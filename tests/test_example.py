"""The launch example must actually run — a broken example is worse than none."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "examples" / "rogue_agent.py"


@pytest.fixture(scope="module")
def example():
    spec = importlib.util.spec_from_file_location("rogue_agent", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["rogue_agent"] = module
    spec.loader.exec_module(module)
    return module


class TestRunsCleanly:
    def test_exits_zero_with_the_world_restored(self, example, tmp_path, capsys):
        code = example.main(["--pace", "0", "--ledger", str(tmp_path / "run.json")])
        assert code == 0
        out = capsys.readouterr().out
        assert "13 of 15 actions restored" in out
        assert "the issue tracker is exactly as it was" in out

    def test_needs_no_credentials(self, example, tmp_path, monkeypatch):
        monkeypatch.delenv("CONTROLZ_GITHUB_TOKEN", raising=False)
        assert example.main(["--pace", "0", "--ledger", str(tmp_path / "run.json")]) == 0

    def test_writes_a_reloadable_ledger(self, example, tmp_path):
        from controlz import Ledger

        path = tmp_path / "run.json"
        example.main(["--pace", "0", "--ledger", str(path)])

        ledger = Ledger.load(path)
        assert len(ledger) == 15
        assert json.loads(path.read_text())["session"]["agent"] == "rogue-agent"

    def test_shows_the_plan_before_it_runs(self, example, tmp_path, capsys):
        example.main(["--pace", "0", "--ledger", str(tmp_path / "run.json")])
        out = capsys.readouterr().out
        assert "What the agent intends to do" in out
        assert "Blast radius" in out
        # The policy verdict is shown before any action is taken.
        assert out.index("Whether it is allowed to") < out.index("The agent goes to work")

    def test_is_honest_about_the_irreversible_action(self, example, tmp_path, capsys):
        example.main(["--pace", "0", "--ledger", str(tmp_path / "run.json")])
        out = capsys.readouterr().out
        assert "wire_transfer" in out
        assert "not coming back" in out
        assert "refused to pretend otherwise" in out

    def test_no_rollback_leaves_the_mess(self, example, tmp_path, capsys):
        code = example.main(
            ["--pace", "0", "--ledger", str(tmp_path / "run.json"), "--no-rollback"]
        )
        assert code == 0
        assert "leaving it as-is" in capsys.readouterr().out


class TestRealRepoGuard:
    def test_refuses_a_real_repo_without_a_token(self, example, tmp_path, monkeypatch):
        monkeypatch.delenv("CONTROLZ_GITHUB_TOKEN", raising=False)
        with pytest.raises(SystemExit, match="CONTROLZ_GITHUB_TOKEN"):
            example.main(["--repo", "someone/real", "--ledger", str(tmp_path / "r.json")])
