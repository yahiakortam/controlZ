"""GitHubIntegration: classification, snapshots, execution, and rollback plans.

Runs against the in-memory fake in ``tests/fakes.py``; see
``test_github_live.py`` for the same behaviour against real GitHub.
"""

import pytest

from controlz import Action, Operation, Reversibility
from controlz.integrations import IntegrationError, UnsupportedOperationError
from controlz.integrations.github import TOKEN_ENV_VAR, GitHubIntegration
from fakes import FakeGithubError


def op(api_call: str, **args) -> Operation:
    return Operation(tool="github", api_call=api_call, args=args)


def run(github, operation):
    """Snapshot, execute, snapshot — the same sequence the tracker follows.

    Returns the resulting Action and the backend result.
    """
    before = github.snapshot(operation)
    result = github.execute(operation)
    after = github.snapshot_after(operation, result)
    action = Action(
        session_id="s1",
        tool=operation.tool,
        api_call=operation.api_call,
        args=operation.args,
        state_before=before,
        state_after=after,
        reversibility=github.classify(operation),
    )
    return action, result


def run_action(github, operation) -> Action:
    return run(github, operation)[0]


def planned(github, operation) -> Action:
    """Run an operation and attach its rollback plan, as the tracker would."""
    action, _ = run(github, operation)
    action.rollback_plan = github.build_rollback_plan(action)
    return action


class TestClassification:
    @pytest.mark.parametrize(
        ("api_call", "expected"),
        [
            ("update_issue", Reversibility.REVERSIBLE),
            ("add_labels", Reversibility.REVERSIBLE),
            ("remove_labels", Reversibility.REVERSIBLE),
            ("close_issue", Reversibility.REVERSIBLE),
            ("reopen_issue", Reversibility.REVERSIBLE),
            ("create_issue", Reversibility.COMPENSATABLE),
            ("create_comment", Reversibility.COMPENSATABLE),
        ],
    )
    def test_hardcoded_map(self, github, api_call, expected):
        assert github.classify(op(api_call)) is expected

    def test_unknown_operation_is_unknown(self, github):
        assert github.classify(op("delete_repository")) is Reversibility.UNKNOWN

    def test_classification_is_a_lookup_not_a_call(self, github, fake_github):
        github.classify(op("create_issue", repo="acme/widgets"))
        assert fake_github.get_repo_calls == []

    def test_supported_operations(self):
        assert GitHubIntegration.supports("create_issue")
        assert not GitHubIntegration.supports("merge_pull_request")


class TestClientConstruction:
    def test_rejects_both_client_and_token(self, fake_github):
        with pytest.raises(ValueError, match="not both"):
            GitHubIntegration(client=fake_github, token="x")

    def test_missing_credentials_error(self, monkeypatch):
        monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
        with pytest.raises(IntegrationError, match=TOKEN_ENV_VAR):
            _ = GitHubIntegration().client


class TestSnapshots:
    def test_issue_snapshot_shape(self, github, issue, repo_name):
        state = github.snapshot(op("close_issue", repo=repo_name, issue_number=issue.number))
        assert state == {
            "repo": repo_name,
            "issue": {
                "issue_number": issue.number,
                "title": "Original title",
                "body": "Original body",
                "state": "open",
                "labels": ["triage"],
                "assignees": [],
                "url": issue.html_url,
            },
        }

    def test_create_issue_snapshot_records_absence(self, github, repo_name):
        assert github.snapshot(op("create_issue", repo=repo_name, title="x")) == {
            "repo": repo_name,
            "issue": None,
        }

    def test_create_comment_snapshot_records_absence(self, github, issue, repo_name):
        state = github.snapshot(
            op("create_comment", repo=repo_name, issue_number=issue.number, body="hi")
        )
        assert state == {"repo": repo_name, "issue_number": issue.number, "comment": None}

    def test_snapshot_does_not_mutate(self, github, issue, repo_name):
        github.snapshot(op("update_issue", repo=repo_name, issue_number=issue.number, title="new"))
        assert issue.title == "Original title"
        assert issue.edits == []

    def test_snapshot_rejects_unsupported_operation(self, github, repo_name):
        with pytest.raises(UnsupportedOperationError):
            github.snapshot(op("delete_repository", repo=repo_name))

    def test_snapshot_requires_repo(self, github):
        with pytest.raises(IntegrationError, match="repo"):
            github.snapshot(op("close_issue", issue_number=1))

    def test_snapshot_after_reads_new_issue_number(self, github, repo_name):
        operation = op("create_issue", repo=repo_name, title="Fresh")
        result = github.execute(operation)
        after = github.snapshot_after(operation, result)
        assert after["issue"]["issue_number"] == result.number
        assert after["issue"]["title"] == "Fresh"

    def test_snapshot_after_reads_new_comment_id(self, github, issue, repo_name):
        operation = op("create_comment", repo=repo_name, issue_number=issue.number, body="hi")
        result = github.execute(operation)
        after = github.snapshot_after(operation, result)
        assert after["comment"] == {"comment_id": result.id, "body": "hi", "url": result.html_url}


class TestExecute:
    def test_create_issue(self, github, fake_github, repo_name):
        result = github.execute(op("create_issue", repo=repo_name, title="Bug", body="Details"))
        assert fake_github.get_repo(repo_name).issues[result.number].title == "Bug"
        assert result.body == "Details"

    def test_update_issue(self, github, issue, repo_name):
        github.execute(op("update_issue", repo=repo_name, issue_number=issue.number, title="New"))
        assert issue.title == "New"

    def test_update_issue_requires_a_field(self, github, issue, repo_name):
        with pytest.raises(IntegrationError, match="at least one of"):
            github.execute(op("update_issue", repo=repo_name, issue_number=issue.number))

    def test_close_and_reopen(self, github, issue, repo_name):
        github.execute(op("close_issue", repo=repo_name, issue_number=issue.number))
        assert issue.state == "closed"
        github.execute(op("reopen_issue", repo=repo_name, issue_number=issue.number))
        assert issue.state == "open"

    def test_add_and_remove_labels(self, github, issue, repo_name):
        github.execute(op("add_labels", repo=repo_name, issue_number=issue.number, labels=["bug"]))
        assert issue.label_names == ["triage", "bug"]
        github.execute(
            op("remove_labels", repo=repo_name, issue_number=issue.number, labels=["bug"])
        )
        assert issue.label_names == ["triage"]

    def test_labels_must_be_a_list(self, github, issue, repo_name):
        with pytest.raises(IntegrationError, match="list of label names"):
            github.execute(
                op("add_labels", repo=repo_name, issue_number=issue.number, labels="bug")
            )

    def test_create_and_delete_comment(self, github, issue, repo_name):
        comment = github.execute(
            op("create_comment", repo=repo_name, issue_number=issue.number, body="hello")
        )
        assert issue.comments[comment.id].body == "hello"
        github.execute(
            op(
                "delete_comment",
                repo=repo_name,
                issue_number=issue.number,
                comment_id=comment.id,
            )
        )
        assert issue.comments == {}

    def test_unsupported_operation(self, github, repo_name):
        with pytest.raises(UnsupportedOperationError, match="does not support"):
            github.execute(op("delete_repository", repo=repo_name))

    def test_backend_errors_propagate(self, github, repo_name):
        with pytest.raises(FakeGithubError):
            github.execute(op("close_issue", repo=repo_name, issue_number=999))


class TestRollbackPlans:
    def test_create_issue_plan_closes_it(self, github, repo_name):
        operation = op("create_issue", repo=repo_name, title="Bug")
        action = run_action(github, operation)
        plan = github.build_rollback_plan(action)

        assert plan.strategy == "close-created-issue"
        assert len(plan.steps) == 1
        step = plan.steps[0]
        assert step.tool == "github"
        assert step.api_call == "close_issue"
        assert step.args == {
            "repo": repo_name,
            "issue_number": action.state_after["issue"]["issue_number"],
        }

    def test_create_comment_plan_deletes_it(self, github, issue, repo_name):
        operation = op("create_comment", repo=repo_name, issue_number=issue.number, body="hi")
        action, result = run(github, operation)
        plan = github.build_rollback_plan(action)

        assert plan.strategy == "delete-created-comment"
        assert plan.steps[0].api_call == "delete_comment"
        assert plan.steps[0].args["comment_id"] == result.id

    def test_update_issue_plan_restores_changed_fields_only(self, github, issue, repo_name):
        operation = op("update_issue", repo=repo_name, issue_number=issue.number, title="New title")
        plan = github.build_rollback_plan(run_action(github, operation))

        assert plan.strategy == "restore-previous-fields"
        assert plan.steps[0].api_call == "update_issue"
        assert plan.steps[0].args == {
            "repo": repo_name,
            "issue_number": issue.number,
            "title": "Original title",
        }

    def test_update_issue_plan_restores_several_fields(self, github, issue, repo_name):
        operation = op(
            "update_issue", repo=repo_name, issue_number=issue.number, title="T", body="B"
        )
        plan = github.build_rollback_plan(run_action(github, operation))
        assert plan.steps[0].args["title"] == "Original title"
        assert plan.steps[0].args["body"] == "Original body"

    def test_update_that_changed_nothing_is_a_no_op(self, github, issue, repo_name):
        operation = op(
            "update_issue", repo=repo_name, issue_number=issue.number, title="Original title"
        )
        plan = github.build_rollback_plan(run_action(github, operation))
        assert plan.strategy == "no-op"
        assert plan.is_executable is False

    def test_add_labels_plan_removes_only_new_labels(self, github, issue, repo_name):
        operation = op(
            "add_labels", repo=repo_name, issue_number=issue.number, labels=["bug", "triage"]
        )
        plan = github.build_rollback_plan(run_action(github, operation))

        assert plan.steps[0].api_call == "remove_labels"
        # "triage" was already there — removing it would lose pre-existing state.
        assert plan.steps[0].args["labels"] == ["bug"]

    def test_add_labels_plan_is_a_no_op_when_nothing_new(self, github, issue, repo_name):
        operation = op("add_labels", repo=repo_name, issue_number=issue.number, labels=["triage"])
        plan = github.build_rollback_plan(run_action(github, operation))
        assert plan.strategy == "no-op"

    def test_remove_labels_plan_re_adds_only_present_labels(self, github, issue, repo_name):
        operation = op(
            "remove_labels", repo=repo_name, issue_number=issue.number, labels=["triage"]
        )
        plan = github.build_rollback_plan(run_action(github, operation))
        assert plan.steps[0].api_call == "add_labels"
        assert plan.steps[0].args["labels"] == ["triage"]

    def test_close_plan_reopens(self, github, issue, repo_name):
        operation = op("close_issue", repo=repo_name, issue_number=issue.number)
        plan = github.build_rollback_plan(run_action(github, operation))
        assert plan.steps[0].api_call == "reopen_issue"
        assert plan.steps[0].args == {"repo": repo_name, "issue_number": issue.number}

    def test_reopen_plan_closes(self, github, issue, repo_name):
        issue.edit(state="closed")
        operation = op("reopen_issue", repo=repo_name, issue_number=issue.number)
        plan = github.build_rollback_plan(run_action(github, operation))
        assert plan.steps[0].api_call == "close_issue"

    def test_closing_an_already_closed_issue_is_a_no_op(self, github, issue, repo_name):
        issue.edit(state="closed")
        operation = op("close_issue", repo=repo_name, issue_number=issue.number)
        plan = github.build_rollback_plan(run_action(github, operation))
        assert plan.strategy == "no-op"

    def test_no_plan_for_another_tool(self, github):
        action = Action(session_id="s1", tool="email", api_call="send", state_after={})
        assert github.build_rollback_plan(action) is None

    def test_no_plan_when_the_action_never_completed(self, github, repo_name):
        action = Action(
            session_id="s1",
            tool="github",
            api_call="close_issue",
            args={"repo": repo_name, "issue_number": 1},
            state_after=None,
        )
        assert github.build_rollback_plan(action) is None


class TestExecuteRollback:
    def test_create_issue_rollback_closes_the_issue(self, github, repo_name):
        operation = op("create_issue", repo=repo_name, title="Oops")
        action, result = run(github, operation)
        action.rollback_plan = github.build_rollback_plan(action)

        github.execute_rollback(action)
        assert result.state == "closed"

    def test_label_rollback_restores_exact_prior_labels(self, github, issue, repo_name):
        before = list(issue.label_names)
        operation = op(
            "add_labels", repo=repo_name, issue_number=issue.number, labels=["bug", "triage"]
        )
        action = planned(github, operation)

        github.execute_rollback(action)
        assert issue.label_names == before

    def test_update_rollback_restores_the_title(self, github, issue, repo_name):
        operation = op("update_issue", repo=repo_name, issue_number=issue.number, title="New")
        action = planned(github, operation)

        github.execute_rollback(action)
        assert issue.title == "Original title"

    def test_comment_rollback_deletes_the_comment(self, github, issue, repo_name):
        operation = op("create_comment", repo=repo_name, issue_number=issue.number, body="oops")
        action = planned(github, operation)

        github.execute_rollback(action)
        assert issue.comments == {}

    def test_close_rollback_reopens(self, github, issue, repo_name):
        operation = op("close_issue", repo=repo_name, issue_number=issue.number)
        action = planned(github, operation)

        github.execute_rollback(action)
        assert issue.state == "open"

    def test_builds_the_plan_if_the_action_lacks_one(self, github, issue, repo_name):
        operation = op("close_issue", repo=repo_name, issue_number=issue.number)
        action = run_action(github, operation)
        assert action.rollback_plan is None

        github.execute_rollback(action)
        assert issue.state == "open"

    def test_refuses_an_action_from_another_tool(self, github):
        action = Action(session_id="s1", tool="email", api_call="send")
        with pytest.raises(IntegrationError, match="belongs to tool"):
            github.execute_rollback(action)

    def test_refuses_a_no_op_plan(self, github, issue, repo_name):
        issue.edit(state="closed")
        operation = op("close_issue", repo=repo_name, issue_number=issue.number)
        action = planned(github, operation)

        with pytest.raises(IntegrationError, match="no steps"):
            github.execute_rollback(action)

    def test_refuses_when_no_plan_can_be_built(self, github):
        action = Action(session_id="s1", tool="github", api_call="close_issue", state_after=None)
        with pytest.raises(IntegrationError, match="no rollback plan"):
            github.execute_rollback(action)
