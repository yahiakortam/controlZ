"""Rollback: ordering, conflict handling, and honesty about what came back.

The four requirements under test:

1. reverse dependency order
2. drift is detected and never overwritten without explicit confirmation
3. irreversible actions are reported as un-restored, never silently dropped
4. the report accounts for every action exactly once
"""

from typing import ClassVar

import pytest

from controlz import (
    Action,
    Ledger,
    Reversibility,
    RollbackEngine,
    RollbackOutcome,
    RollbackPlan,
    RollbackStep,
    Session,
    Tracker,
    dependency_order,
)
from controlz.integrations import Integration
from controlz.integrations.memory import SandboxError


class TestDependencyOrder:
    def test_no_dependencies_is_reverse_chronological(self):
        session = Session()
        first = session.record(tool="t", api_call="a")
        second = session.record(tool="t", api_call="b")
        third = session.record(tool="t", api_call="c")

        ordered, cycles = dependency_order(session)
        assert [a.operation_id for a in ordered] == [
            third.operation_id,
            second.operation_id,
            first.operation_id,
        ]
        assert cycles == set()

    def test_dependents_are_undone_before_their_dependencies(self):
        session = Session()
        base = session.record(tool="t", api_call="base")
        # Recorded before its dependency in wall-clock terms would be unusual,
        # but the graph, not the clock, decides the order.
        session.record(tool="t", api_call="leaf", dependencies=[base.operation_id])

        ordered, _ = dependency_order(session)
        assert [a.api_call for a in ordered] == ["leaf", "base"]

    def test_chronology_does_not_override_the_graph(self):
        """An action recorded late that others depend on still goes last."""
        session = Session()
        a = Action(session_id=session.session_id, tool="t", api_call="a")
        b = Action(session_id=session.session_id, tool="t", api_call="b")
        # b was recorded second but a depends on it, so b must be undone after a.
        a = a.model_copy(update={"dependencies": [b.operation_id]})
        session.append(b)
        session.append(a)

        ordered, _ = dependency_order(session)
        assert [x.api_call for x in ordered] == ["a", "b"]

    def test_diamond_dependencies(self):
        session = Session()
        root = session.record(tool="t", api_call="root")
        left = session.record(tool="t", api_call="left", dependencies=[root.operation_id])
        right = session.record(tool="t", api_call="right", dependencies=[root.operation_id])
        session.record(
            tool="t", api_call="top", dependencies=[left.operation_id, right.operation_id]
        )

        ordered, _ = dependency_order(session)
        names = [a.api_call for a in ordered]
        assert names[0] == "top"
        assert names[-1] == "root"
        assert set(names[1:3]) == {"left", "right"}

    def test_cycles_are_reported_not_reordered(self):
        session = Session()
        first = Action(session_id=session.session_id, tool="t", api_call="a")
        second = Action(
            session_id=session.session_id,
            tool="t",
            api_call="b",
            dependencies=[first.operation_id],
        )
        first = first.model_copy(update={"dependencies": [second.operation_id]})
        session.append(first)
        session.append(second)

        ordered, cycles = dependency_order(session)
        assert cycles == {first.operation_id, second.operation_id}
        assert len(ordered) == 2

    def test_unknown_dependency_ids_are_ignored(self):
        session = Session()
        session.record(tool="t", api_call="a", dependencies=["not-in-this-session"])
        ordered, cycles = dependency_order(session)
        assert len(ordered) == 1
        assert cycles == set()

    def test_empty_session(self):
        assert dependency_order(Session()) == ([], set())


class TestCleanRollback:
    """The happy path: everything comes back."""

    def test_restores_the_repo_to_the_recorded_state(self, tracker, issue, repo_name):
        original = {
            "title": issue.title,
            "body": issue.body,
            "state": issue.state,
            "labels": list(issue.label_names),
        }

        tracker.call(
            "github",
            "update_issue",
            repo=repo_name,
            issue_number=issue.number,
            title="Wrong title",
            body="Wrong body",
        )
        tracker.call(
            "github",
            "add_labels",
            repo=repo_name,
            issue_number=issue.number,
            labels=["wontfix", "duplicate"],
        )
        tracker.call("github", "close_issue", repo=repo_name, issue_number=issue.number)

        report = tracker.rollback()

        assert report.complete
        assert len(report.restored) == 3
        assert report.unrestored == []
        assert {
            "title": issue.title,
            "body": issue.body,
            "state": issue.state,
            "labels": list(issue.label_names),
        } == original

    def test_rolls_back_in_reverse_dependency_order(self, tracker, repo_name):
        created = tracker.call("github", "create_issue", repo=repo_name, title="Parent")
        parent_id = tracker.last_action().operation_id
        tracker.engine  # noqa: B018 - the engine is rebuilt per call; touching it is harmless

        from controlz import Operation

        tracker.track(
            Operation(
                tool="github",
                api_call="create_comment",
                args={"repo": repo_name, "issue_number": created.number, "body": "child"},
            ),
            dependencies=[parent_id],
        )

        report = tracker.rollback()
        assert [e.api_call for e in report.entries] == ["create_comment", "create_issue"]
        assert report.complete

    def test_session_rollback_is_the_same_engine(self, tracker, github, issue, repo_name):
        tracker.call("github", "close_issue", repo=repo_name, issue_number=issue.number)

        report = tracker.ledger.session.rollback(github)

        assert len(report.restored) == 1
        assert issue.state == "open"

    def test_report_survives_serialization(self, tracker, issue, repo_name):
        tracker.call("github", "close_issue", repo=repo_name, issue_number=issue.number)
        report = tracker.rollback()

        from controlz import RollbackReport

        restored = RollbackReport.model_validate(report.model_dump(mode="json"))
        assert restored.entries[0].outcome is RollbackOutcome.RESTORED
        assert restored.complete

    def test_rollback_from_a_reloaded_ledger(self, tracker, github, issue, repo_name, tmp_path):
        tracker.call(
            "github", "add_labels", repo=repo_name, issue_number=issue.number, labels=["bug"]
        )
        path = tracker.ledger.save(tmp_path / "run.json")

        replay = Tracker(Ledger.load(path), [github])
        report = replay.rollback()

        assert report.complete
        assert issue.label_names == ["triage"]


class TestConflicts:
    """Rule two: never overwrite a surprise."""

    def test_external_edit_refuses_and_flags(self, tracker, issue, repo_name):
        tracker.call(
            "github",
            "update_issue",
            repo=repo_name,
            issue_number=issue.number,
            title="Agent title",
        )
        # Someone else edits the same field before the rollback runs.
        issue.edit(title="A human was here")

        report = tracker.rollback()

        assert report.restored == []
        assert len(report.conflicts) == 1
        entry = report.conflicts[0]
        assert entry.outcome is RollbackOutcome.CONFLICT
        assert entry.conflicts[0].field == "issue.title"
        assert entry.conflicts[0].expected == "Agent title"
        assert entry.conflicts[0].actual == "A human was here"
        # The human's edit is untouched.
        assert issue.title == "A human was here"
        assert not report.complete

    def test_external_state_change_conflicts(self, tracker, issue, repo_name):
        tracker.call("github", "close_issue", repo=repo_name, issue_number=issue.number)
        issue.edit(state="open")  # someone reopened it themselves

        report = tracker.rollback()
        assert len(report.conflicts) == 1
        assert report.conflicts[0].conflicts[0].field == "issue.state"

    def test_removed_label_conflicts(self, tracker, issue, repo_name):
        tracker.call(
            "github", "add_labels", repo=repo_name, issue_number=issue.number, labels=["bug"]
        )
        issue.remove_from_labels("bug")  # someone beat us to it

        report = tracker.rollback()
        assert len(report.conflicts) == 1
        assert "already removed" in report.conflicts[0].conflicts[0].detail

    def test_edited_comment_is_not_deleted(self, tracker, issue, repo_name):
        comment = tracker.call(
            "github", "create_comment", repo=repo_name, issue_number=issue.number, body="original"
        )
        issue.comments[comment.id].body = "someone edited this"

        report = tracker.rollback()

        assert len(report.conflicts) == 1
        assert comment.id in issue.comments  # not deleted

    def test_vanished_target_is_a_conflict_not_a_failure(self, tracker, issue, repo_name):
        tracker.call("github", "close_issue", repo=repo_name, issue_number=issue.number)
        del tracker.integration_for("github").client.get_repo(repo_name).issues[issue.number]

        report = tracker.rollback()
        assert len(report.conflicts) == 1
        assert "gone or unreadable" in report.conflicts[0].conflicts[0].detail

    def test_unrelated_edit_does_not_block_the_rollback(self, tracker, issue, repo_name):
        """A rollback overwrites only what the action wrote."""
        tracker.call(
            "github", "add_labels", repo=repo_name, issue_number=issue.number, labels=["bug"]
        )
        # Someone edits the title — a field this action never touched.
        issue.edit(title="Meanwhile, a human renamed it")

        report = tracker.rollback()

        assert len(report.restored) == 1
        assert issue.label_names == ["triage"]
        assert issue.title == "Meanwhile, a human renamed it"  # left alone

    def test_explicit_confirmation_overrides_a_conflict(self, tracker, issue, repo_name):
        tracker.call(
            "github", "update_issue", repo=repo_name, issue_number=issue.number, title="Agent"
        )
        issue.edit(title="Human")

        report = tracker.rollback(on_conflict=lambda action, conflicts: True)

        assert len(report.restored) == 1
        assert issue.title == "Original title"
        # The override is recorded, not hidden.
        assert "overridden by explicit confirmation" in report.restored[0].reason

    def test_confirmation_callback_can_decline(self, tracker, issue, repo_name):
        tracker.call(
            "github", "update_issue", repo=repo_name, issue_number=issue.number, title="Agent"
        )
        issue.edit(title="Human")

        report = tracker.rollback(on_conflict=lambda action, conflicts: False)
        assert len(report.conflicts) == 1
        assert issue.title == "Human"

    def test_callback_receives_the_action_and_details(self, tracker, issue, repo_name):
        tracker.call(
            "github", "update_issue", repo=repo_name, issue_number=issue.number, title="Agent"
        )
        issue.edit(title="Human")
        seen = []

        tracker.rollback(on_conflict=lambda action, conflicts: seen.append((action, conflicts)))

        action, conflicts = seen[0]
        assert action.api_call == "update_issue"
        assert conflicts[0].field == "issue.title"

    def test_force_by_operation_id(self, tracker, issue, repo_name):
        tracker.call(
            "github", "update_issue", repo=repo_name, issue_number=issue.number, title="Agent"
        )
        issue.edit(title="Human")
        operation_id = tracker.last_action().operation_id

        report = tracker.rollback(force=[operation_id])
        assert len(report.restored) == 1

    def test_per_action_force(self, tracker, issue, repo_name):
        tracker.call(
            "github", "update_issue", repo=repo_name, issue_number=issue.number, title="Agent"
        )
        issue.edit(title="Human")
        action = tracker.last_action()

        assert tracker.rollback_action(action).outcome is RollbackOutcome.CONFLICT
        assert issue.title == "Human"
        assert tracker.rollback_action(action, force=True).outcome is RollbackOutcome.RESTORED
        assert issue.title == "Original title"


class TestHonesty:
    """Rule three: nothing is quietly dropped, and nothing is over-claimed."""

    @staticmethod
    def _irreversible(session, **overrides):
        kwargs = dict(
            tool="github",
            api_call="wire_transfer",
            args={"amount": 5000},
            reversibility=Reversibility.IRREVERSIBLE,
            state_after={"sent": True},
        )
        kwargs.update(overrides)
        return session.record(**kwargs)

    def test_irreversible_is_reported_never_claimed_restored(self, github):
        session = Session()
        action = self._irreversible(session)

        report = session.rollback(github)

        assert report.restored == []
        assert len(report.skipped_irreversible) == 1
        entry = report.skipped_irreversible[0]
        assert entry.operation_id == action.operation_id
        assert entry.outcome is RollbackOutcome.SKIPPED
        assert "irreversible" in entry.reason
        assert report.unrestored == [entry]
        # Nothing is left for a human to retry, but the session did NOT fully
        # come back — and the report must not blur those two things together.
        assert report.complete
        assert not report.fully_restored

    def test_unknown_classification_is_reported(self, github):
        session = Session()
        session.record(tool="github", api_call="mystery", state_after={})

        report = session.rollback(github)
        assert len(report.skipped_irreversible) == 1
        assert "unknown" in report.skipped_irreversible[0].reason

    def test_action_without_a_plan_is_reported(self, github):
        session = Session()
        session.record(
            tool="github",
            api_call="close_issue",
            reversibility=Reversibility.REVERSIBLE,
            state_after={},
        )

        report = session.rollback(github)
        assert len(report.skipped_irreversible) == 1
        assert "no rollback plan" in report.skipped_irreversible[0].reason

    def test_action_for_an_unregistered_tool_is_reported(self):
        session = Session()
        session.record(
            tool="stripe",
            api_call="refund",
            reversibility=Reversibility.REVERSIBLE,
            rollback_plan=RollbackPlan(
                strategy="x", steps=[RollbackStep(tool="stripe", api_call="y")]
            ),
        )

        report = session.rollback([])
        assert len(report.skipped_irreversible) == 1
        assert "no integration registered" in report.skipped_irreversible[0].reason

    def test_a_no_op_is_not_claimed_as_restored(self, tracker, issue, repo_name):
        tracker.call(
            "github", "add_labels", repo=repo_name, issue_number=issue.number, labels=["triage"]
        )
        report = tracker.rollback()

        assert report.restored == []
        assert len(report.nothing_to_do) == 1
        assert report.complete  # nothing needed doing, so nothing is outstanding

    def test_failure_is_reported_with_the_error(self, tracker, issue, repo_name, monkeypatch):
        tracker.call("github", "close_issue", repo=repo_name, issue_number=issue.number)

        def explode(action):
            raise SandboxError("502 upstream is unhappy")

        monkeypatch.setattr(tracker.integration_for("github"), "execute_rollback", explode)
        report = tracker.rollback()

        assert report.restored == []
        assert len(report.failures) == 1
        assert "502 upstream is unhappy" in report.failures[0].error
        assert not report.complete

    def test_every_action_appears_exactly_once(self, tracker, github, issue, repo_name):
        tracker.call("github", "close_issue", repo=repo_name, issue_number=issue.number)
        tracker.call(
            "github", "add_labels", repo=repo_name, issue_number=issue.number, labels=["bug"]
        )
        self._irreversible(tracker.ledger.session)

        report = tracker.rollback()

        recorded = [a.operation_id for a in tracker.ledger.actions]
        reported = [e.operation_id for e in report.entries]
        assert sorted(reported) == sorted(recorded)
        assert len(reported) == len(set(reported))

    def test_summary_names_everything_that_did_not_come_back(self, tracker, issue, repo_name):
        tracker.call("github", "close_issue", repo=repo_name, issue_number=issue.number)
        self._irreversible(tracker.ledger.session)

        summary = tracker.rollback().summary()
        assert "1 of 2 actions restored" in summary
        assert "not undoable: wire_transfer" in summary

    def test_counts_cover_every_entry(self, tracker, issue, repo_name):
        tracker.call("github", "close_issue", repo=repo_name, issue_number=issue.number)
        self._irreversible(tracker.ledger.session)

        report = tracker.rollback()
        assert sum(report.counts().values()) == len(report.entries)


class TestBlocking:
    """An action whose dependent could not be undone is held back, and said so."""

    def test_blocked_when_a_dependent_conflicts(self, tracker, repo_name):
        from controlz import Operation

        created = tracker.call("github", "create_issue", repo=repo_name, title="Parent")
        parent_id = tracker.last_action().operation_id
        comment = tracker.track(
            Operation(
                tool="github",
                api_call="create_comment",
                args={"repo": repo_name, "issue_number": created.number, "body": "child"},
            ),
            dependencies=[parent_id],
        )
        # Someone edits the comment, so it must not be deleted.
        created.comments[comment.result.id].body = "edited by a human"

        report = tracker.rollback()

        assert len(report.conflicts) == 1
        blocked = report.blocked
        assert len(blocked) == 1
        assert blocked[0].operation_id == parent_id
        assert "depends on this action" in blocked[0].reason
        assert created.state == "open"  # the parent was not closed
        assert not report.complete

    def test_blocking_can_be_disabled(self, tracker, repo_name):
        from controlz import Operation

        created = tracker.call("github", "create_issue", repo=repo_name, title="Parent")
        parent_id = tracker.last_action().operation_id
        comment = tracker.track(
            Operation(
                tool="github",
                api_call="create_comment",
                args={"repo": repo_name, "issue_number": created.number, "body": "child"},
            ),
            dependencies=[parent_id],
        )
        created.comments[comment.result.id].body = "edited by a human"

        report = tracker.ledger.session.rollback(
            tracker.integration_for("github"), block_dependencies=False
        )

        assert len(report.conflicts) == 1
        assert len(report.restored) == 1
        assert created.state == "closed"

    def test_cycles_are_failed_not_hidden(self, github):
        session = Session()
        first = Action(session_id=session.session_id, tool="github", api_call="close_issue")
        second = Action(
            session_id=session.session_id,
            tool="github",
            api_call="close_issue",
            dependencies=[first.operation_id],
        )
        session.append(first.model_copy(update={"dependencies": [second.operation_id]}))
        session.append(second)

        report = session.rollback(github)
        assert len(report.failures) == 2
        assert all("cycle" in e.reason for e in report.failures)


class TestDryRun:
    def test_changes_nothing_and_claims_nothing(self, tracker, issue, repo_name):
        tracker.call("github", "close_issue", repo=repo_name, issue_number=issue.number)

        report = tracker.rollback(dry_run=True)

        assert report.dry_run
        assert report.restored == []
        assert len(report.planned) == 1
        assert issue.state == "closed"  # untouched
        assert "dry run" in report.summary()

    def test_dry_run_still_reports_conflicts(self, tracker, issue, repo_name):
        tracker.call(
            "github", "update_issue", repo=repo_name, issue_number=issue.number, title="Agent"
        )
        issue.edit(title="Human")

        report = tracker.rollback(dry_run=True)
        assert len(report.conflicts) == 1
        assert report.planned == []

    def test_dry_run_then_commit(self, tracker, issue, repo_name):
        tracker.call("github", "close_issue", repo=repo_name, issue_number=issue.number)
        assert tracker.rollback(dry_run=True).planned
        assert issue.state == "closed"

        assert tracker.rollback().restored
        assert issue.state == "open"


class TestStopOnError:
    def test_remaining_actions_are_reported_not_omitted(self, tracker, issue, repo_name):
        tracker.call(
            "github", "add_labels", repo=repo_name, issue_number=issue.number, labels=["bug"]
        )
        tracker.call("github", "close_issue", repo=repo_name, issue_number=issue.number)

        integration = tracker.integration_for("github")
        original = integration.execute_rollback

        def explode(action):
            if action.api_call == "close_issue":
                raise SandboxError("boom")
            return original(action)

        integration.execute_rollback = explode
        report = tracker.rollback(stop_on_error=True)

        assert len(report.failures) == 1
        assert len(report.not_attempted) == 1
        assert report.not_attempted[0].api_call == "add_labels"
        assert len(report.entries) == 2
        assert not report.complete


class TestChaos:
    """Fifteen wrong changes, one rollback, an honest account of the result."""

    @pytest.fixture
    def chaos(self, tracker, fake_github, repo_name):
        """An agent makes a mess across three issues, then reality drifts."""
        repo = fake_github.get_repo(repo_name)
        alpha = repo.create_issue(title="Alpha", body="Alpha body", labels=["triage"])
        beta = repo.create_issue(title="Beta", body="Beta body", labels=["bug"])
        gamma = repo.create_issue(title="Gamma", body="Gamma body", labels=[])

        original = {
            issue.number: {
                "title": issue.title,
                "body": issue.body,
                "state": issue.state,
                "labels": sorted(issue.label_names),
            }
            for issue in (alpha, beta, gamma)
        }

        gh = tracker.tool("github")
        # 1-3: mangle alpha
        gh.update_issue(repo=repo_name, issue_number=alpha.number, title="WRONG alpha")
        gh.add_labels(repo=repo_name, issue_number=alpha.number, labels=["wontfix"])
        gh.close_issue(repo=repo_name, issue_number=alpha.number)
        # 4-6: mangle beta
        gh.update_issue(repo=repo_name, issue_number=beta.number, body="WRONG beta body")
        gh.remove_labels(repo=repo_name, issue_number=beta.number, labels=["bug"])
        gh.add_labels(repo=repo_name, issue_number=beta.number, labels=["invalid", "duplicate"])
        # 7-9: mangle gamma
        gh.update_issue(repo=repo_name, issue_number=gamma.number, title="WRONG gamma")
        gh.close_issue(repo=repo_name, issue_number=gamma.number)
        gh.reopen_issue(repo=repo_name, issue_number=gamma.number)
        # 10-12: noise on all three
        comment_a = gh.create_comment(repo=repo_name, issue_number=alpha.number, body="spam A")
        comment_b = gh.create_comment(repo=repo_name, issue_number=beta.number, body="spam B")
        gh.create_comment(repo=repo_name, issue_number=gamma.number, body="spam C")
        # 13: a brand-new issue that should not exist
        created = gh.create_issue(repo=repo_name, title="WRONG new issue")
        # 14: a no-op — the label was already there
        gh.add_labels(repo=repo_name, issue_number=alpha.number, labels=["wontfix"])
        # 15: something nothing can undo
        tracker.ledger.session.record(
            tool="github",
            api_call="wire_transfer",
            args={"amount": 5000},
            intent="Pay the invoice the user mentioned.",
            reversibility=Reversibility.IRREVERSIBLE,
            state_after={"sent": True},
        )
        return {
            "issues": {"alpha": alpha, "beta": beta, "gamma": gamma},
            "original": original,
            "created": created,
            "comments": {"alpha": comment_a, "beta": comment_b},
        }

    def test_fifteen_actions_were_recorded(self, tracker, chaos):
        assert len(tracker.ledger) == 15

    def test_clean_chaos_rolls_back_everything_recoverable(self, tracker, chaos):
        report = tracker.rollback()

        assert len(report.entries) == 15
        assert len(report.restored) == 13
        assert len(report.nothing_to_do) == 1  # the redundant label
        assert len(report.skipped_irreversible) == 1  # the wire transfer
        assert report.conflicts == []
        assert report.failures == []
        assert report.complete  # nothing left to retry
        assert not report.fully_restored  # but the wire transfer never came back

        for issue in chaos["issues"].values():
            assert {
                "title": issue.title,
                "body": issue.body,
                "state": issue.state,
                "labels": sorted(issue.label_names),
            } == chaos["original"][issue.number]
            assert issue.comments == {}

        # The issue that should never have existed is closed, not deleted.
        assert chaos["created"].state == "closed"

    def test_the_irreversible_action_is_never_claimed_restored(self, tracker, chaos):
        report = tracker.rollback()

        wire = [e for e in report.entries if e.api_call == "wire_transfer"]
        assert len(wire) == 1
        assert wire[0].outcome is RollbackOutcome.SKIPPED
        assert "irreversible" in wire[0].reason
        assert wire[0].operation_id not in {e.operation_id for e in report.restored}
        assert "not undoable: wire_transfer" in report.summary()

    def test_chaos_with_drift_restores_the_rest_and_flags_the_conflicts(self, tracker, chaos):
        alpha = chaos["issues"]["alpha"]
        beta = chaos["issues"]["beta"]

        # Three independent external changes land before the rollback.
        alpha.edit(title="A human retitled alpha")
        beta.remove_from_labels("invalid")
        alpha.comments[chaos["comments"]["alpha"].id].body = "a human edited this comment"

        report = tracker.rollback()

        assert len(report.entries) == 15
        assert len(report.conflicts) == 3
        conflicted = {e.api_call for e in report.conflicts}
        assert conflicted == {"update_issue", "add_labels", "create_comment"}

        # Nothing a human touched was overwritten.
        assert alpha.title == "A human retitled alpha"
        assert alpha.comments[chaos["comments"]["alpha"].id].body == "a human edited this comment"

        # Everything else still came back.
        gamma = chaos["issues"]["gamma"]
        assert beta.body == chaos["original"][beta.number]["body"]
        assert gamma.title == chaos["original"][gamma.number]["title"]
        assert gamma.comments == {}
        assert not report.complete

    def test_every_action_is_accounted_for_under_drift(self, tracker, chaos):
        chaos["issues"]["alpha"].edit(title="A human retitled alpha")
        chaos["issues"]["beta"].remove_from_labels("invalid")

        report = tracker.rollback()

        recorded = {a.operation_id for a in tracker.ledger.actions}
        assert {e.operation_id for e in report.entries} == recorded
        assert len(report.restored) + len(report.unrestored) == 15

    def test_dry_run_over_chaos_changes_nothing(self, tracker, chaos):
        alpha = chaos["issues"]["alpha"]
        before = (alpha.title, alpha.state, sorted(alpha.label_names))

        report = tracker.rollback(dry_run=True)

        assert len(report.entries) == 15
        assert report.restored == []
        assert (alpha.title, alpha.state, sorted(alpha.label_names)) == before


class TestEngineConstruction:
    def test_accepts_a_single_integration(self, github, issue, repo_name):
        session = Session()
        engine = RollbackEngine(session, github)
        assert engine.integrations == {"github": github}

    def test_accepts_an_iterable(self, github):
        assert RollbackEngine(Session(), [github]).integrations == {"github": github}

    def test_empty_session_produces_an_empty_report(self, github):
        report = RollbackEngine(Session(), [github]).run()
        assert report.entries == []
        assert report.complete
        assert report.restored == []


class TestDefaultConflictCheck:
    """Integrations that do not override check_conflict get whole-state comparison."""

    class Memory(Integration):
        name = "memory"
        classification: ClassVar[dict] = {"set": Reversibility.REVERSIBLE}

        def __init__(self):
            self.store: dict[str, str] = {}

        def snapshot(self, operation):
            key = operation.args["key"]
            return {"key": key, "value": self.store.get(key)}

        def classify(self, operation):
            return self.classification.get(operation.api_call, Reversibility.UNKNOWN)

        def execute(self, operation):
            self.store[operation.args["key"]] = operation.args["value"]
            return self.store[operation.args["key"]]

        def build_rollback_plan(self, action):
            before = action.state_before or {}
            return RollbackPlan(
                strategy="restore-previous-value",
                steps=[
                    RollbackStep(
                        tool="memory",
                        api_call="set",
                        args={"key": before["key"], "value": before["value"]},
                    )
                ],
            )

        def execute_rollback(self, action):
            self.execute_rollback_plan(action)

    def test_clean_rollback(self):
        memory = self.Memory()
        memory.store["greeting"] = "hello"
        tracker = Tracker(Ledger(), [memory])
        tracker.call("memory", "set", key="greeting", value="goodbye")

        report = tracker.rollback()
        assert len(report.restored) == 1
        assert memory.store["greeting"] == "hello"

    def test_drift_is_caught_by_the_default_check(self):
        memory = self.Memory()
        memory.store["greeting"] = "hello"
        tracker = Tracker(Ledger(), [memory])
        tracker.call("memory", "set", key="greeting", value="goodbye")

        memory.store["greeting"] = "someone else wrote this"
        report = tracker.rollback()

        assert len(report.conflicts) == 1
        assert memory.store["greeting"] == "someone else wrote this"
