"""Exercise scripts/demo_policy.py: readout, verdict, and exit code."""

import importlib.util
import sys
from pathlib import Path

import pytest

from controlz import Decision, Policy

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "demo_policy.py"
EXAMPLE_POLICY = ROOT / "controlz-policy.yaml"


@pytest.fixture(scope="module")
def demo():
    spec = importlib.util.spec_from_file_location("demo_policy", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["demo_policy"] = module
    spec.loader.exec_module(module)
    return module


class TestPlans:
    def test_the_three_plans_escalate(self, demo):
        assert len(demo.plan("safe")) == 3
        assert len(demo.plan("risky")) == 7
        assert len(demo.plan("reckless")) == 9

    def test_unknown_plan_is_refused(self, demo):
        with pytest.raises(SystemExit):
            demo.plan("nonsense")


class TestExitCodes:
    def test_safe_plan_is_cleared(self, demo, capsys):
        assert demo.main(["--plan", "safe", "--policy", str(EXAMPLE_POLICY)]) == 0
        assert "cleared to run 3 actions" in capsys.readouterr().out

    def test_risky_plan_is_held_for_approval(self, demo, capsys):
        assert demo.main(["--plan", "risky", "--policy", str(EXAMPLE_POLICY)]) == 1
        assert "held for approval" in capsys.readouterr().out

    def test_risky_plan_proceeds_once_approved(self, demo, capsys):
        code = demo.main(["--plan", "risky", "--policy", str(EXAMPLE_POLICY), "--approve"])
        assert code == 0
        assert "with explicit human approval" in capsys.readouterr().out

    def test_reckless_plan_is_refused(self, demo, capsys):
        assert demo.main(["--plan", "reckless", "--policy", str(EXAMPLE_POLICY)]) == 1
        assert "refused to run this task" in capsys.readouterr().out

    def test_a_block_is_not_approvable(self, demo, capsys):
        """--approve must not rescue a blocked plan."""
        code = demo.main(["--plan", "reckless", "--policy", str(EXAMPLE_POLICY), "--approve"])
        assert code == 1
        assert "refused to run this task" in capsys.readouterr().out


class TestReadout:
    def test_prints_the_blast_radius(self, demo, capsys):
        demo.main(["--plan", "risky", "--policy", str(EXAMPLE_POLICY)])
        out = capsys.readouterr().out
        assert "Blast radius" in out
        assert "reversibility score" in out
        assert "Planned actions" in out

    def test_names_what_cannot_be_undone(self, demo, capsys):
        demo.main(["--plan", "reckless", "--policy", str(EXAMPLE_POLICY)])
        out = capsys.readouterr().out
        assert "cannot be undone" in out
        assert "delete_repository" in out

    def test_shows_every_rule_that_fired(self, demo, capsys):
        demo.main(["--plan", "reckless", "--policy", str(EXAMPLE_POLICY)])
        out = capsys.readouterr().out
        assert "minimum_score" in out
        assert "on_unknown" in out

    def test_defaults_to_the_builtin_policy(self, demo, capsys):
        assert demo.main(["--plan", "safe"]) == 0
        assert "policy 'default'" in capsys.readouterr().out


class TestExamplePolicyFile:
    def test_the_shipped_example_parses(self):
        policy = Policy.from_yaml(EXAMPLE_POLICY)
        assert policy.name == "example"
        assert policy.minimum_score == 60
        assert policy.max_compensatable == 3
        assert policy.on_irreversible is Decision.REQUIRE_APPROVAL
