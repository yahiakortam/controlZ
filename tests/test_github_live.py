"""Integration tests against a real GitHub repository.

Skipped unless both are set::

    export CONTROLZ_GITHUB_TOKEN=ghp_...        # needs `repo` / issues:write
    export CONTROLZ_TEST_REPO=you/throwaway     # a REPO YOU DO NOT MIND MUTATING

Every test works inside an issue this module creates, and the issue is closed
during teardown. Run them with ``pytest -m live``.
"""

from __future__ import annotations

import os
import uuid

import pytest

from controlz import Ledger, Operation, Reversibility, Session, Tracker
from controlz.integrations.github import GitHubIntegration

pytestmark = pytest.mark.live

TOKEN = os.environ.get("CONTROLZ_GITHUB_TOKEN")
REPO = os.environ.get("CONTROLZ_TEST_REPO")

pytest.importorskip("github", reason="PyGithub is not installed")

if not TOKEN or not REPO:
    pytest.skip(
        "live GitHub tests need CONTROLZ_GITHUB_TOKEN and CONTROLZ_TEST_REPO",
        allow_module_level=True,
    )

LABEL = "controlz-test"


@pytest.fixture(scope="module")
def integration() -> GitHubIntegration:
    return GitHubIntegration(token=TOKEN)


@pytest.fixture
def tracker(integration) -> Tracker:
    return Tracker(Ledger(Session(agent="controlz-live-tests")), [integration])


@pytest.fixture
def issue_number(integration, tracker):
    """A fresh issue for one test, closed again afterwards."""
    created = integration.execute(
        Operation(
            tool="github",
            api_call="create_issue",
            args={
                "repo": REPO,
                "title": f"[controlz] fixture {uuid.uuid4().hex[:8]}",
                "body": "Created by the ControlZ live test suite. Safe to close.",
                "labels": [LABEL],
            },
        )
    )
    yield created.number
    try:
        integration.execute(
            Operation(
                tool="github",
                api_call="close_issue",
                args={"repo": REPO, "issue_number": created.number},
            )
        )
    except Exception as exc:  # pragma: no cover - cleanup best effort
        print(f"warning: could not close issue #{created.number}: {exc}")


class TestLiveTracking:
    def test_create_issue_records_a_complete_action(self, tracker, integration):
        tracked = tracker.track(
            Operation(
                tool="github",
                api_call="create_issue",
                args={
                    "repo": REPO,
                    "title": f"[controlz] tracked create {uuid.uuid4().hex[:8]}",
                    "body": "Opened by a tracked call.",
                },
                intent="Prove a real create lands in the ledger.",
            )
        )
        action, created = tracked.action, tracked.result

        try:
            assert action.state_before == {"repo": REPO, "issue": None}
            assert action.state_after["issue"]["issue_number"] == created.number
            assert action.state_after["issue"]["state"] == "open"
            assert action.reversibility is Reversibility.COMPENSATABLE
            assert action.rollback_plan.strategy == "close-created-issue"
            assert action.rollback_plan.steps[0].args["issue_number"] == created.number

            tracker.rollback(action)
            assert integration.client.get_repo(REPO).get_issue(created.number).state == "closed"
        finally:
            integration.execute(
                Operation(
                    tool="github",
                    api_call="close_issue",
                    args={"repo": REPO, "issue_number": created.number},
                )
            )

    def test_update_issue_round_trip(self, tracker, integration, issue_number):
        new_title = f"[controlz] retitled {uuid.uuid4().hex[:8]}"
        tracked = tracker.track(
            Operation(
                tool="github",
                api_call="update_issue",
                args={"repo": REPO, "issue_number": issue_number, "title": new_title},
            )
        )
        original = tracked.action.state_before["issue"]["title"]

        assert tracked.action.reversibility is Reversibility.REVERSIBLE
        assert tracked.action.state_after["issue"]["title"] == new_title

        tracker.rollback(tracked.action)
        assert integration.client.get_repo(REPO).get_issue(issue_number).title == original

    def test_comment_created_then_deleted(self, tracker, integration, issue_number):
        body = f"ControlZ live test comment {uuid.uuid4().hex[:8]}"
        tracked = tracker.call(
            "github", "create_comment", repo=REPO, issue_number=issue_number, body=body
        )
        action = tracker.last_action()

        assert action.reversibility is Reversibility.COMPENSATABLE
        assert action.state_before["comment"] is None
        assert action.state_after["comment"]["comment_id"] == tracked.id

        tracker.rollback(action)
        issue = integration.client.get_repo(REPO).get_issue(issue_number)
        assert tracked.id not in [comment.id for comment in issue.get_comments()]

    def test_labels_added_then_removed(self, tracker, integration, issue_number):
        extra = "controlz-temp"
        tracker.call(
            "github", "add_labels", repo=REPO, issue_number=issue_number, labels=[extra, LABEL]
        )
        action = tracker.last_action()

        assert action.reversibility is Reversibility.REVERSIBLE
        assert extra in action.state_after["issue"]["labels"]
        # LABEL was already on the issue, so the plan must not strip it.
        assert action.rollback_plan.steps[0].args["labels"] == [extra]

        tracker.rollback(action)
        issue = integration.client.get_repo(REPO).get_issue(issue_number)
        assert (
            sorted(label.name for label in issue.labels) == action.state_before["issue"]["labels"]
        )

    def test_close_then_reopen(self, tracker, integration, issue_number):
        tracker.call("github", "close_issue", repo=REPO, issue_number=issue_number)
        action = tracker.last_action()

        assert action.state_before["issue"]["state"] == "open"
        assert action.state_after["issue"]["state"] == "closed"
        assert action.rollback_plan.steps[0].api_call == "reopen_issue"

        tracker.rollback(action)
        assert integration.client.get_repo(REPO).get_issue(issue_number).state == "open"

    def test_session_survives_a_ledger_round_trip(self, tracker, tmp_path, issue_number):
        tracker.call(
            "github", "add_labels", repo=REPO, issue_number=issue_number, labels=["controlz-temp"]
        )
        tracker.call("github", "close_issue", repo=REPO, issue_number=issue_number)

        path = tracker.ledger.save(tmp_path / "live.json")
        reloaded = Ledger.load(path)

        assert reloaded.session == tracker.ledger.session
        assert len(reloaded) == 2

        # Roll back from the reloaded ledger, not the in-memory one.
        replay = Tracker(reloaded, [tracker.integration_for("github")])
        undone = replay.rollback_session()
        assert [a.api_call for a in undone] == ["close_issue", "add_labels"]
