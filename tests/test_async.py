"""The async core.

Two things must hold. The async path must behave *identically* to the sync one
— same actions, same classifications, same honesty in the report — and it must
not block the event loop while it waits on the network.
"""

import asyncio
import time
from typing import Any, ClassVar

import pytest

from controlz import (
    Decision,
    Ledger,
    Operation,
    Policy,
    PolicyViolation,
    Reversibility,
    RollbackOutcome,
    RollbackPlan,
    RollbackStep,
    Session,
    Tracker,
)
from controlz.integrations import Integration
from controlz.integrations.github import GitHubIntegration
from controlz.integrations.memory import InMemoryGitHub, SandboxError

REPO = "acme/widgets"


@pytest.fixture
def backend() -> InMemoryGitHub:
    return InMemoryGitHub()


@pytest.fixture
def github(backend) -> GitHubIntegration:
    return GitHubIntegration(client=backend)


@pytest.fixture
def tracker(github) -> Tracker:
    return Tracker(Ledger(Session(agent="async-agent")), [github])


@pytest.fixture
def issue(backend):
    return backend.get_repo(REPO).create_issue(
        title="Original title", body="Original body", labels=["triage"]
    )


class TestAsyncCall:
    async def test_acall_executes_and_records(self, tracker, issue):
        await tracker.acall("github", "close_issue", repo=REPO, issue_number=issue.number)

        assert issue.state == "closed"
        assert len(tracker.ledger) == 1
        assert tracker.last_action().api_call == "close_issue"

    async def test_acall_returns_the_backend_result(self, tracker):
        result = await tracker.acall("github", "create_issue", repo=REPO, title="Bug")
        assert result.title == "Bug"

    async def test_atrack_records_a_complete_action(self, tracker, issue):
        tracked = await tracker.atrack(
            Operation(
                tool="github",
                api_call="update_issue",
                args={"repo": REPO, "issue_number": issue.number, "title": "New title"},
                intent="Retitle it.",
            )
        )
        action = tracked.action

        assert action.intent == "Retitle it."
        assert action.reversibility is Reversibility.REVERSIBLE
        assert action.state_before["issue"]["title"] == "Original title"
        assert action.state_after["issue"]["title"] == "New title"
        assert action.rollback_plan.steps[0].args["title"] == "Original title"

    async def test_intent_and_dependencies(self, tracker):
        first = await tracker.acall("github", "create_issue", repo=REPO, title="Parent")
        parent = tracker.last_action().operation_id
        tracked = await tracker.atrack(
            Operation(
                tool="github",
                api_call="create_comment",
                args={"repo": REPO, "issue_number": first.number, "body": "hi"},
            ),
            intent="Because.",
            dependencies=[parent],
        )
        assert tracked.action.intent == "Because."
        assert tracked.action.dependencies == [parent]

    async def test_unsupported_operation_is_refused(self, tracker):
        from controlz.integrations import UnsupportedOperationError

        with pytest.raises(UnsupportedOperationError):
            await tracker.acall("github", "delete_repository", repo=REPO)
        assert tracker.ledger.actions == []


class TestAsyncFailures:
    async def test_failed_call_is_recorded_and_reraised(self, tracker):
        with pytest.raises(SandboxError):
            await tracker.acall("github", "close_issue", repo=REPO, issue_number=999)

        action = tracker.last_action()
        assert action.state_after is None
        assert action.rollback_plan is None
        assert action.reversibility is Reversibility.UNKNOWN

    async def test_snapshot_failure_is_recorded_by_default(self, tracker):
        with pytest.raises(SandboxError):
            await tracker.acall("github", "add_labels", repo=REPO, issue_number=42, labels=["x"])
        assert "snapshot failed" in tracker.last_action().state_before["error"]

    async def test_snapshot_failure_can_abort_the_call(self, github):
        from controlz import TrackingError

        tracker = Tracker(Ledger(), [github], snapshot_errors="raise")
        with pytest.raises(TrackingError, match="could not snapshot"):
            await tracker.acall("github", "close_issue", repo=REPO, issue_number=42)
        assert tracker.ledger.actions == []


class TestAsyncPolicy:
    async def test_blocked_call_does_not_execute(self, github, issue):
        tracker = Tracker(Ledger(), [github], policy=Policy(on_reversible=Decision.BLOCK))
        with pytest.raises(PolicyViolation):
            await tracker.acall("github", "close_issue", repo=REPO, issue_number=issue.number)

        assert issue.state == "open"
        assert tracker.ledger.actions == []

    async def test_approval_required_without_an_approver(self, github):
        policy = Policy(minimum_score=0, on_compensatable=Decision.REQUIRE_APPROVAL)
        tracker = Tracker(Ledger(), [github], policy=policy)
        with pytest.raises(PolicyViolation):
            await tracker.acall("github", "create_issue", repo=REPO, title="x")

    async def test_a_sync_approver_still_works(self, github):
        policy = Policy(minimum_score=0, on_compensatable=Decision.REQUIRE_APPROVAL)
        tracker = Tracker(Ledger(), [github], policy=policy, approve=lambda d: True)
        await tracker.acall("github", "create_issue", repo=REPO, title="x")
        assert len(tracker.ledger) == 1

    async def test_an_async_approver_is_awaited(self, github):
        """Asking a human usually means a network round trip."""
        asked = []

        async def approve(decision):
            await asyncio.sleep(0)
            asked.append(decision)
            return True

        policy = Policy(minimum_score=0, on_compensatable=Decision.REQUIRE_APPROVAL)
        tracker = Tracker(Ledger(), [github], policy=policy, approve=approve)
        await tracker.acall("github", "create_issue", repo=REPO, title="x")

        assert len(asked) == 1
        assert len(tracker.ledger) == 1

    async def test_an_async_approver_can_refuse(self, github):
        async def refuse(decision):
            return False

        policy = Policy(minimum_score=0, on_compensatable=Decision.REQUIRE_APPROVAL)
        tracker = Tracker(Ledger(), [github], policy=policy, approve=refuse)
        with pytest.raises(PolicyViolation):
            await tracker.acall("github", "create_issue", repo=REPO, title="x")


class TestAsyncRollback:
    async def test_arollback_restores_the_session(self, tracker, issue):
        original = (issue.title, issue.state, list(issue.label_names))

        await tracker.acall(
            "github", "update_issue", repo=REPO, issue_number=issue.number, title="WRONG"
        )
        await tracker.acall(
            "github", "add_labels", repo=REPO, issue_number=issue.number, labels=["bug"]
        )
        await tracker.acall("github", "close_issue", repo=REPO, issue_number=issue.number)

        report = await tracker.arollback()

        assert report.fully_restored, report.summary()
        assert len(report.restored) == 3
        assert (issue.title, issue.state, list(issue.label_names)) == original

    async def test_arollback_action_undoes_one(self, tracker, issue):
        await tracker.acall("github", "close_issue", repo=REPO, issue_number=issue.number)
        entry = await tracker.arollback_action(tracker.last_action())

        assert entry.outcome is RollbackOutcome.RESTORED
        assert issue.state == "open"

    async def test_conflicts_are_refused(self, tracker, issue):
        await tracker.acall(
            "github", "update_issue", repo=REPO, issue_number=issue.number, title="Agent"
        )
        issue.edit(title="A human was here")

        report = await tracker.arollback()

        assert len(report.conflicts) == 1
        assert issue.title == "A human was here"
        assert not report.complete

    async def test_an_async_on_conflict_is_awaited(self, tracker, issue):
        await tracker.acall(
            "github", "update_issue", repo=REPO, issue_number=issue.number, title="Agent"
        )
        issue.edit(title="A human was here")

        async def confirm(action, conflicts):
            await asyncio.sleep(0)
            return True

        report = await tracker.arollback(on_conflict=confirm)

        assert len(report.restored) == 1
        assert issue.title == "Original title"
        assert "overridden by explicit confirmation" in report.restored[0].reason

    async def test_irreversible_is_reported_not_restored(self, tracker, issue):
        await tracker.acall("github", "close_issue", repo=REPO, issue_number=issue.number)
        tracker.ledger.record(
            tool="github",
            api_call="wire_transfer",
            args={"amount": 5000},
            reversibility=Reversibility.IRREVERSIBLE,
            state_after={"sent": True},
        )

        report = await tracker.arollback()

        assert len(report.restored) == 1
        assert len(report.skipped_irreversible) == 1
        assert report.complete
        assert not report.fully_restored

    async def test_dry_run_changes_nothing(self, tracker, issue):
        await tracker.acall("github", "close_issue", repo=REPO, issue_number=issue.number)
        report = await tracker.arollback(dry_run=True)

        assert len(report.planned) == 1
        assert report.restored == []
        assert issue.state == "closed"

    async def test_session_arollback(self, tracker, github, issue):
        await tracker.acall("github", "close_issue", repo=REPO, issue_number=issue.number)
        report = await tracker.ledger.session.arollback(github)

        assert len(report.restored) == 1
        assert issue.state == "open"

    async def test_order_is_newest_first(self, tracker, issue):
        await tracker.acall(
            "github", "add_labels", repo=REPO, issue_number=issue.number, labels=["bug"]
        )
        await tracker.acall("github", "close_issue", repo=REPO, issue_number=issue.number)

        report = await tracker.arollback()
        assert [e.api_call for e in report.entries] == ["close_issue", "add_labels"]


class TestAsyncLedger:
    async def test_asave_and_aload_round_trip(self, tracker, issue, tmp_path):
        await tracker.acall("github", "close_issue", repo=REPO, issue_number=issue.number)
        path = await tracker.ledger.asave(tmp_path / "run.json")

        reloaded = await Ledger.aload(path)
        assert reloaded.session == tracker.ledger.session

    async def test_autosave_persists_each_call(self, github, issue, tmp_path):
        path = tmp_path / "run.json"
        tracker = Tracker(Ledger(path=path, autosave=True), [github])

        await tracker.acall("github", "close_issue", repo=REPO, issue_number=issue.number)
        assert len(Ledger.load(path)) == 1

        await tracker.acall("github", "reopen_issue", repo=REPO, issue_number=issue.number)
        assert len(Ledger.load(path)) == 2

    async def test_aappend_and_arecord(self, tmp_path):
        ledger = Ledger(path=tmp_path / "run.json", autosave=True)
        await ledger.arecord(tool="t", api_call="a")
        assert len(Ledger.load(tmp_path / "run.json")) == 1


class BlockingIntegration(Integration):
    """An integration whose calls block, like most real SDKs do."""

    name: ClassVar[str] = "blocking"
    classification: ClassVar[dict[str, Reversibility]] = {"work": Reversibility.REVERSIBLE}
    DELAY = 0.15

    def __init__(self):
        self.calls = 0

    def snapshot(self, operation):
        return {"value": None}

    def classify(self, operation):
        return self.classification.get(operation.api_call, Reversibility.UNKNOWN)

    def execute(self, operation):
        time.sleep(self.DELAY)  # a blocking network call
        self.calls += 1
        return self.calls

    def build_rollback_plan(self, action):
        return RollbackPlan(strategy="undo", steps=[RollbackStep(tool=self.name, api_call="work")])

    def execute_rollback(self, action):
        self.execute_rollback_plan(action)


class TestDoesNotBlockTheLoop:
    """The whole point: a blocking SDK must not stall the event loop."""

    async def test_other_coroutines_keep_running_during_a_call(self):
        tracker = Tracker(Ledger(), [BlockingIntegration()])
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        await tracker.acall("blocking", "work")
        beat.cancel()

        # A blocked loop would have ticked zero times during the 0.15s call.
        assert ticks >= 5, f"the loop only ticked {ticks} times — it was blocked"

    async def test_concurrent_calls_overlap(self):
        tracker = Tracker(Ledger(), [BlockingIntegration()])

        started = time.perf_counter()
        await asyncio.gather(*(tracker.acall("blocking", "work") for _ in range(4)))
        elapsed = time.perf_counter() - started

        # Serialized would be ~0.6s; overlapped should be nearer 0.15s.
        assert elapsed < 0.45, f"calls did not overlap ({elapsed:.2f}s)"

    async def test_concurrent_calls_all_land_in_the_ledger(self):
        """Appends happen on the loop, so none can be lost to a race."""
        tracker = Tracker(Ledger(), [BlockingIntegration()])

        await asyncio.gather(*(tracker.acall("blocking", "work") for _ in range(12)))

        assert len(tracker.ledger) == 12
        ids = [a.operation_id for a in tracker.ledger.actions]
        assert len(set(ids)) == 12

    async def test_the_sync_path_still_blocks_and_that_is_fine(self):
        """Sanity check on the test itself: sync really is the slow one."""
        tracker = Tracker(Ledger(), [BlockingIntegration()])
        started = time.perf_counter()
        for _ in range(2):
            tracker.call("blocking", "work")
        assert time.perf_counter() - started >= BlockingIntegration.DELAY * 2 * 0.9


class NativeAsyncIntegration(Integration):
    """An integration built on an async client, overriding the async hooks."""

    name: ClassVar[str] = "native"
    classification: ClassVar[dict[str, Reversibility]] = {"set": Reversibility.REVERSIBLE}

    def __init__(self):
        self.store: dict[str, str] = {}
        self.used_async: list[str] = []
        self.used_sync: list[str] = []

    # -- the sync half exists, but should not be reached on the async path ---

    def snapshot(self, operation):
        self.used_sync.append("snapshot")
        return {"key": operation.args["key"], "value": self.store.get(operation.args["key"])}

    def classify(self, operation):
        return self.classification.get(operation.api_call, Reversibility.UNKNOWN)

    def execute(self, operation):
        self.used_sync.append("execute")
        self.store[operation.args["key"]] = operation.args["value"]
        return self.store[operation.args["key"]]

    def build_rollback_plan(self, action):
        before = action.state_before or {}
        return RollbackPlan(
            strategy="restore",
            steps=[
                RollbackStep(
                    tool=self.name,
                    api_call="set",
                    args={"key": before["key"], "value": before["value"]},
                )
            ],
        )

    def execute_rollback(self, action):
        self.execute_rollback_plan(action)

    # -- the async half, natively awaited -----------------------------------

    async def asnapshot(self, operation: Operation) -> dict[str, Any] | None:
        self.used_async.append("asnapshot")
        await asyncio.sleep(0)
        return {"key": operation.args["key"], "value": self.store.get(operation.args["key"])}

    async def aexecute(self, operation: Operation) -> Any:
        self.used_async.append("aexecute")
        await asyncio.sleep(0)
        self.store[operation.args["key"]] = operation.args["value"]
        return self.store[operation.args["key"]]

    async def asnapshot_after(self, operation: Operation, result: Any) -> dict[str, Any] | None:
        # Overridden alongside asnapshot: the default would offload the
        # blocking snapshot_after, which is right for a sync SDK and wrong here.
        self.used_async.append("asnapshot_after")
        return await self.asnapshot(operation)

    async def acheck_conflict(self, action):
        self.used_async.append("acheck_conflict")
        current = self.store.get((action.state_after or {})["key"])
        recorded = (action.state_after or {})["value"]
        if current != recorded:
            from controlz import ConflictDetail

            return [ConflictDetail(field="value", expected=recorded, actual=current)]
        return []

    async def aexecute_rollback(self, action) -> None:
        self.used_async.append("aexecute_rollback")
        await self.aexecute_rollback_plan(action)


class TestNativeAsyncIntegration:
    async def test_the_async_hooks_are_used_not_the_threads(self):
        native = NativeAsyncIntegration()
        native.store["greeting"] = "hello"
        tracker = Tracker(Ledger(), [native])

        await tracker.acall("native", "set", key="greeting", value="goodbye")

        assert "aexecute" in native.used_async
        assert "asnapshot" in native.used_async
        # The blocking twins were never touched.
        assert native.used_sync == []

    async def test_native_rollback(self):
        native = NativeAsyncIntegration()
        native.store["greeting"] = "hello"
        tracker = Tracker(Ledger(), [native])
        await tracker.acall("native", "set", key="greeting", value="goodbye")

        report = await tracker.arollback()

        assert report.fully_restored
        assert native.store["greeting"] == "hello"
        assert "aexecute_rollback" in native.used_async

    async def test_native_conflict_detection(self):
        native = NativeAsyncIntegration()
        native.store["greeting"] = "hello"
        tracker = Tracker(Ledger(), [native])
        await tracker.acall("native", "set", key="greeting", value="goodbye")

        native.store["greeting"] = "someone else wrote this"
        report = await tracker.arollback()

        assert len(report.conflicts) == 1
        assert native.store["greeting"] == "someone else wrote this"


class TestSyncIntegrationsOnTheAsyncPath:
    """A blocking SDK keeps its snapshot_after semantics when awaited."""

    async def test_create_still_captures_the_new_identifier(self, tracker):
        """GitHubIntegration overrides snapshot_after, not asnapshot_after."""
        result = await tracker.acall("github", "create_issue", repo=REPO, title="Fresh")
        action = tracker.last_action()

        assert action.state_before == {"repo": REPO, "issue": None}
        assert action.state_after["issue"]["issue_number"] == result.number
        assert action.state_after["issue"]["title"] == "Fresh"
        assert action.rollback_plan.strategy == "close-created-issue"

    async def test_comment_create_captures_the_new_id(self, tracker, issue):
        comment = await tracker.acall(
            "github", "create_comment", repo=REPO, issue_number=issue.number, body="hi"
        )
        action = tracker.last_action()

        assert action.state_before["comment"] is None
        assert action.state_after["comment"]["comment_id"] == comment.id


class TestParity:
    """The two paths must not drift apart."""

    @staticmethod
    def _mess(tracker_call, issue):
        return [
            ("update_issue", {"repo": REPO, "issue_number": issue.number, "title": "WRONG"}),
            ("add_labels", {"repo": REPO, "issue_number": issue.number, "labels": ["bug"]}),
            ("close_issue", {"repo": REPO, "issue_number": issue.number}),
            ("create_comment", {"repo": REPO, "issue_number": issue.number, "body": "hi"}),
        ]

    async def test_same_actions_from_both_paths(self):
        def build():
            backend = InMemoryGitHub()
            issue = backend.get_repo(REPO).create_issue(
                title="Original title", body="Body", labels=["triage"]
            )
            return Tracker(Ledger(), [GitHubIntegration(client=backend)]), issue

        sync_tracker, sync_issue = build()
        async_tracker, async_issue = build()

        for api_call, args in self._mess(None, sync_issue):
            sync_tracker.track(Operation(tool="github", api_call=api_call, args=args))
        for api_call, args in self._mess(None, async_issue):
            await async_tracker.atrack(Operation(tool="github", api_call=api_call, args=args))

        def shape(tracker):
            return [
                (
                    a.api_call,
                    a.reversibility,
                    a.state_before,
                    a.state_after,
                    a.rollback_plan.strategy if a.rollback_plan else None,
                )
                for a in tracker.ledger.actions
            ]

        assert shape(sync_tracker) == shape(async_tracker)

    async def test_same_report_from_both_paths(self):
        def build():
            backend = InMemoryGitHub()
            issue = backend.get_repo(REPO).create_issue(title="T", body="B", labels=["triage"])
            return Tracker(Ledger(), [GitHubIntegration(client=backend)]), issue

        sync_tracker, sync_issue = build()
        async_tracker, async_issue = build()

        for tracker, issue in ((sync_tracker, sync_issue), (async_tracker, async_issue)):
            for api_call, args in self._mess(None, issue):
                tracker.track(Operation(tool="github", api_call=api_call, args=args))
            tracker.ledger.record(
                tool="github",
                api_call="wire_transfer",
                args={},
                reversibility=Reversibility.IRREVERSIBLE,
                state_after={"sent": True},
            )

        sync_report = sync_tracker.rollback()
        async_report = await async_tracker.arollback()

        def shape(report):
            return [(e.api_call, e.outcome, e.reason) for e in report.entries]

        assert shape(sync_report) == shape(async_report)
        assert sync_report.complete == async_report.complete
        assert sync_report.fully_restored == async_report.fully_restored
