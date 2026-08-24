"""Exercise scripts/demo_github.py end to end against the in-memory fake.

The live version of this run needs credentials (see test_github_live.py); this
proves the script's own logic — argument handling, the four tracked calls, the
rendering, the save, and the rollback — without touching the network.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

from controlz import Ledger
from controlz.integrations.github import TOKEN_ENV_VAR, GitHubIntegration
from fakes import FakeGithub

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "demo_github.py"


@pytest.fixture(scope="module")
def demo():
    spec = importlib.util.spec_from_file_location("demo_github", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["demo_github"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_backed(demo, monkeypatch):
    """Point the script's GitHubIntegration at a fake client."""
    client = FakeGithub()
    monkeypatch.setenv(TOKEN_ENV_VAR, "fake-token")
    monkeypatch.setattr(demo, "GitHubIntegration", lambda: GitHubIntegration(client=client))
    return client


class TestArgumentHandling:
    def test_requires_a_repo(self, demo, monkeypatch, capsys):
        monkeypatch.delenv("CONTROLZ_TEST_REPO", raising=False)
        assert demo.main([]) == 2

    def test_requires_a_token(self, demo, monkeypatch):
        monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
        assert demo.main(["--repo", "acme/widgets"]) == 2


class TestDemoRun:
    def test_records_four_actions_and_saves_them(self, demo, fake_backed, tmp_path):
        path = tmp_path / "demo.json"
        assert demo.main(["--repo", "acme/widgets", "--ledger", str(path)]) == 0

        ledger = Ledger.load(path)
        assert [a.api_call for a in ledger.actions] == [
            "create_issue",
            "add_labels",
            "create_comment",
            "close_issue",
        ]

    def test_every_action_is_complete(self, demo, fake_backed, tmp_path):
        path = tmp_path / "demo.json"
        demo.main(["--repo", "acme/widgets", "--ledger", str(path)])

        for action in Ledger.load(path).actions:
            assert action.state_before is not None
            assert action.state_after is not None
            assert action.reversibility.is_undoable
            assert action.rollback_plan is not None and action.rollback_plan.is_executable

    def test_the_comment_depends_on_the_created_issue(self, demo, fake_backed, tmp_path):
        path = tmp_path / "demo.json"
        demo.main(["--repo", "acme/widgets", "--ledger", str(path)])

        session = Ledger.load(path).session
        created, comment = session.actions[0], session.actions[2]
        assert comment.dependencies == [created.operation_id]

    def test_the_real_side_effects_happened(self, demo, fake_backed, tmp_path):
        demo.main(["--repo", "acme/widgets", "--ledger", str(tmp_path / "demo.json")])

        issue = fake_backed.get_repo("acme/widgets").issues[1]
        assert issue.state == "closed"
        assert issue.label_names == ["controlz-demo"]
        assert len(issue.comments) == 1

    def test_rollback_flag_unwinds_the_session(self, demo, fake_backed, tmp_path):
        demo.main(["--repo", "acme/widgets", "--ledger", str(tmp_path / "demo.json"), "--rollback"])

        issue = fake_backed.get_repo("acme/widgets").issues[1]
        assert issue.comments == {}
        assert issue.label_names == []
        # The issue stays closed: closing is the compensation for creating it.
        assert issue.state == "closed"
