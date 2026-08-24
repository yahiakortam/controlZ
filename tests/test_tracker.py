"""The tracker: snapshot → execute → snapshot → classify → plan → record."""

from typing import ClassVar

import pytest

from controlz import Ledger, Operation, Reversibility, Session, Tracker, TrackingError
from controlz.integrations import Integration, UnsupportedOperationError
from controlz.integrations.github import GitHubIntegration
from fakes import FakeGithubError


class TestRegistry:
    def test_creates_its_own_ledger(self):
        assert isinstance(Tracker().ledger, Ledger)

    def test_register_and_list_tools(self, github):
        tracker = Tracker()
        tracker.register(github)
        assert tracker.tools == ["github"]
        assert tracker.integration_for("github") is github

    def test_unregistered_tool(self, tracker):
        with pytest.raises(TrackingError, match="no integration registered"):
            tracker.call("gitlab", "create_issue")

    def test_unnamed_integration_is_rejected(self):
        class Nameless(GitHubIntegration):
            name = ""

        with pytest.raises(ValueError, match="declares no name"):
            Tracker().register(Nameless(token="x"))

    def test_bad_snapshot_error_policy(self):
        with pytest.raises(ValueError, match="snapshot_errors"):
            Tracker(snapshot_errors="explode")


class TestTrackedCall:
    def test_records_a_complete_action(self, tracker, issue, repo_name):
        tracked = tracker.track(
            Operation(
                tool="github",
                api_call="update_issue",
                args={"repo": repo_name, "issue_number": issue.number, "title": "New title"},
                intent="Retitle to match the user's report.",
            )
        )
        action = tracked.action

        assert action.session_id == tracker.ledger.session.session_id
        assert action.tool == "github"
        assert action.api_call == "update_issue"
        assert action.args["title"] == "New title"
        assert action.intent == "Retitle to match the user's report."
        assert action.reversibility is Reversibility.REVERSIBLE
        assert action.state_before["issue"]["title"] == "Original title"
        assert action.state_after["issue"]["title"] == "New title"
        assert action.rollback_plan.steps[0].args["title"] == "Original title"
        assert action.timestamp is not None

    def test_action_lands_in_the_ledger(self, tracker, issue, repo_name):
        tracked = tracker.track(
            Operation(
                tool="github",
                api_call="close_issue",
                args={"repo": repo_name, "issue_number": issue.number},
            )
        )
        assert tracker.ledger.actions == [tracked.action]
        assert tracker.last_action() is tracked.action

    def test_the_call_actually_happens(self, tracker, issue, repo_name):
        tracker.call("github", "close_issue", repo=repo_name, issue_number=issue.number)
        assert issue.state == "closed"

    def test_call_returns_the_backend_result(self, tracker, repo_name):
        result = tracker.call("github", "create_issue", repo=repo_name, title="Bug")
        assert result.number == 1
        assert result.title == "Bug"

    def test_snapshot_is_taken_before_execution(self, tracker, issue, repo_name):
        tracker.call(
            "github", "add_labels", repo=repo_name, issue_number=issue.number, labels=["bug"]
        )
        action = tracker.last_action()
        assert action.state_before["issue"]["labels"] == ["triage"]
        assert action.state_after["issue"]["labels"] == ["bug", "triage"]

    def test_create_issue_before_and_after(self, tracker, repo_name):
        tracker.call("github", "create_issue", repo=repo_name, title="Fresh", body="Body")
        action = tracker.last_action()
        assert action.state_before == {"repo": repo_name, "issue": None}
        assert action.state_after["issue"]["title"] == "Fresh"
        assert action.reversibility is Reversibility.COMPENSATABLE
        assert action.rollback_plan.strategy == "close-created-issue"

    def test_intent_can_be_passed_to_track(self, tracker, repo_name):
        tracked = tracker.track(
            Operation(
                tool="github", api_call="create_issue", args={"repo": repo_name, "title": "x"}
            ),
            intent="Because the user asked.",
        )
        assert tracked.action.intent == "Because the user asked."

    def test_dependencies_are_recorded(self, tracker, repo_name):
        first = tracker.call("github", "create_issue", repo=repo_name, title="Parent")
        parent_id = tracker.last_action().operation_id
        tracker.track(
            Operation(
                tool="github",
                api_call="create_comment",
                args={"repo": repo_name, "issue_number": first.number, "body": "hi"},
            ),
            dependencies=[parent_id],
        )
        assert tracker.last_action().dependencies == [parent_id]

    def test_unsupported_operation_is_refused_before_anything_runs(self, tracker, repo_name):
        with pytest.raises(UnsupportedOperationError):
            tracker.call("github", "delete_repository", repo=repo_name)
        assert tracker.ledger.actions == []

    def test_no_op_plans_are_still_recorded(self, tracker, issue, repo_name):
        issue.edit(state="closed")
        tracker.call("github", "close_issue", repo=repo_name, issue_number=issue.number)
        plan = tracker.last_action().rollback_plan
        assert plan.strategy == "no-op"
        assert plan.is_executable is False


class TestFailures:
    def test_failed_call_is_recorded_and_reraised(self, tracker, repo_name):
        with pytest.raises(FakeGithubError):
            tracker.call("github", "close_issue", repo=repo_name, issue_number=999)

        action = tracker.last_action()
        assert action.api_call == "close_issue"
        assert action.state_after is None
        assert action.rollback_plan is None
        # A call that raised may still have landed: needs a human, not an auto-undo.
        assert action.reversibility is Reversibility.UNKNOWN

    def test_snapshot_failure_is_recorded_by_default(self, tracker, repo_name):
        with pytest.raises(FakeGithubError):
            tracker.call("github", "add_labels", repo=repo_name, issue_number=42, labels=["bug"])
        assert "snapshot failed" in tracker.last_action().state_before["error"]

    def test_snapshot_failure_can_abort_the_call(self, github, repo_name):
        tracker = Tracker(Ledger(), [github], snapshot_errors="raise")
        with pytest.raises(TrackingError, match="could not snapshot"):
            tracker.call("github", "close_issue", repo=repo_name, issue_number=42)
        assert tracker.ledger.actions == []


class TestToolProxy:
    def test_attribute_call_is_tracked(self, tracker, issue, repo_name):
        tracker.tool("github").close_issue(repo=repo_name, issue_number=issue.number)
        assert issue.state == "closed"
        assert tracker.last_action().api_call == "close_issue"

    def test_unknown_operation_is_an_attribute_error(self, tracker):
        proxy = tracker.tool("github")
        with pytest.raises(AttributeError, match="has no operation"):
            _ = proxy.delete_repository

    def test_proxy_for_unknown_tool(self, tracker):
        with pytest.raises(TrackingError):
            tracker.tool("gitlab")

    def test_dir_lists_operations(self, tracker):
        assert "create_issue" in dir(tracker.tool("github"))


class TestPersistenceAndRollback:
    def test_tracked_actions_survive_a_ledger_round_trip(self, tracker, issue, repo_name, tmp_path):
        tracker.call(
            "github", "add_labels", repo=repo_name, issue_number=issue.number, labels=["bug"]
        )
        path = tracker.ledger.save(tmp_path / "run.json")

        reloaded = Ledger.load(path)
        assert reloaded.session == tracker.ledger.session
        assert reloaded.actions[0].rollback_plan.steps[0].args["labels"] == ["bug"]

    def test_rollback_undoes_a_tracked_call(self, tracker, issue, repo_name):
        tracker.call(
            "github", "update_issue", repo=repo_name, issue_number=issue.number, title="New"
        )
        assert issue.title == "New"

        tracker.rollback(tracker.last_action())
        assert issue.title == "Original title"

    def test_rollback_refuses_unknown_classification(self, tracker, repo_name):
        with pytest.raises(FakeGithubError):
            tracker.call("github", "close_issue", repo=repo_name, issue_number=999)
        with pytest.raises(TrackingError, match="refusing to roll it back"):
            tracker.rollback(tracker.last_action())

    def test_rollback_session_unwinds_newest_first(self, tracker, issue, repo_name):
        tracker.call(
            "github", "add_labels", repo=repo_name, issue_number=issue.number, labels=["bug"]
        )
        tracker.call("github", "close_issue", repo=repo_name, issue_number=issue.number)
        comment = tracker.call(
            "github",
            "create_comment",
            repo=repo_name,
            issue_number=issue.number,
            body="Closing this.",
        )

        undone = tracker.rollback_session()

        assert [a.api_call for a in undone] == ["create_comment", "close_issue", "add_labels"]
        assert issue.state == "open"
        assert issue.label_names == ["triage"]
        assert comment.id not in issue.comments

    def test_rollback_session_skips_no_ops(self, tracker, issue, repo_name):
        tracker.call(
            "github", "add_labels", repo=repo_name, issue_number=issue.number, labels=["triage"]
        )  # already present
        assert tracker.rollback_session() == []

    def test_rollback_session_can_continue_past_errors(self, tracker, issue, repo_name):
        tracker.call("github", "close_issue", repo=repo_name, issue_number=issue.number)
        tracker.call(
            "github", "create_comment", repo=repo_name, issue_number=issue.number, body="hi"
        )
        # Someone else deleted the comment in the meantime.
        issue.comments.clear()

        undone = tracker.rollback_session(stop_on_error=False)
        assert [a.api_call for a in undone] == ["close_issue"]
        assert issue.state == "open"


class TestWithAnotherIntegration:
    """The tracker is not GitHub-specific."""

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
            from controlz import RollbackPlan, RollbackStep

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
            for step in action.rollback_plan.steps:
                self.execute(Operation(tool=step.tool, api_call=step.api_call, args=step.args))

    def test_tracks_and_rolls_back(self):
        memory = self.Memory()
        memory.store["greeting"] = "hello"
        tracker = Tracker(Ledger(Session(agent="demo")), [memory])

        tracker.call("memory", "set", key="greeting", value="goodbye")
        assert memory.store["greeting"] == "goodbye"

        tracker.rollback(tracker.last_action())
        assert memory.store["greeting"] == "hello"


class TestIntent:
    def test_call_records_intent(self, tracker, repo_name):
        tracker.call("github", "create_issue", _intent="Because.", repo=repo_name, title="x")
        assert tracker.last_action().intent == "Because."

    def test_proxy_records_intent(self, tracker, issue, repo_name):
        tracker.tool("github").close_issue(
            repo=repo_name, issue_number=issue.number, _intent="Done with it."
        )
        assert tracker.last_action().intent == "Done with it."

    def test_intent_is_not_passed_to_the_backend(self, tracker, repo_name):
        result = tracker.call("github", "create_issue", _intent="x", repo=repo_name, title="Bug")
        assert result.title == "Bug"
        assert "_intent" not in tracker.last_action().args
