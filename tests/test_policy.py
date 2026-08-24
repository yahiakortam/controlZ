"""The policy gate: every branch, and the enforcement that follows from it."""

import pytest
import yaml

from controlz import (
    Action,
    Decision,
    Ledger,
    Operation,
    Policy,
    PolicyDecision,
    PolicyGate,
    PolicyViolation,
    Reversibility,
    Tracker,
    reversibility_score,
)


def op(api_call: str, **args) -> Operation:
    return Operation(tool="github", api_call=api_call, args={"repo": "acme/widgets", **args})


def irreversible() -> Action:
    return Action(
        session_id="s", tool="bank", api_call="wire", reversibility=Reversibility.IRREVERSIBLE
    )


def decide(policy: Policy, plan, github=None) -> PolicyDecision:
    return policy.evaluate(reversibility_score(plan, github))


class TestDecisionOrdering:
    def test_strictest_wins(self):
        assert Decision.strictest([Decision.ALLOW, Decision.BLOCK]) is Decision.BLOCK
        assert (
            Decision.strictest([Decision.ALLOW, Decision.REQUIRE_APPROVAL])
            is Decision.REQUIRE_APPROVAL
        )
        assert Decision.strictest([Decision.ALLOW]) is Decision.ALLOW

    def test_empty_defaults_to_allow(self):
        assert Decision.strictest([]) is Decision.ALLOW

    def test_ranks(self):
        assert Decision.ALLOW.rank < Decision.REQUIRE_APPROVAL.rank < Decision.BLOCK.rank


class TestDefaults:
    def test_defaults_are_cautious(self):
        policy = Policy()
        assert policy.on_reversible is Decision.ALLOW
        assert policy.on_irreversible is Decision.REQUIRE_APPROVAL
        assert policy.on_unknown is Decision.REQUIRE_APPROVAL
        assert policy.below_minimum_score is Decision.BLOCK
        assert policy.minimum_score == 50.0

    def test_all_reversible_is_allowed(self, github):
        decision = decide(Policy(), [op("close_issue"), op("reopen_issue")], github)
        assert decision.decision is Decision.ALLOW
        assert decision.allowed

    def test_empty_plan_is_allowed(self):
        decision = decide(Policy(), [])
        assert decision.allowed
        assert decision.findings == []

    def test_empty_plan_does_not_trip_the_minimum_score(self):
        """Nothing proposed cannot be below the bar."""
        decision = decide(Policy(minimum_score=99.0), [])
        assert decision.allowed


class TestReversibleBranch:
    def test_auto_allow(self, github):
        decision = decide(Policy(), [op("close_issue")], github)
        assert decision.allowed
        assert decision.findings[0].rule == "on_reversible"
        assert decision.findings[0].decision is Decision.ALLOW

    def test_can_be_tightened(self, github):
        policy = Policy(on_reversible=Decision.REQUIRE_APPROVAL)
        assert decide(policy, [op("close_issue")], github).needs_approval


class TestCompensatableBranch:
    def test_within_limit_is_allowed(self, github):
        policy = Policy(max_compensatable=3)
        decision = decide(policy, [op("create_issue", title="x")] * 2, github)
        assert decision.allowed
        assert decision.findings[0].rule == "on_compensatable"

    def test_at_the_limit_is_still_allowed(self, github):
        policy = Policy(max_compensatable=2, minimum_score=0)
        decision = decide(policy, [op("create_issue", title="x")] * 2, github)
        assert decision.allowed

    def test_over_the_limit_escalates(self, github):
        policy = Policy(max_compensatable=2, minimum_score=0)
        decision = decide(policy, [op("create_issue", title="x")] * 3, github)
        assert decision.needs_approval
        finding = decision.findings[0]
        assert finding.rule == "max_compensatable"
        assert "over the limit of 2" in finding.detail

    def test_over_the_limit_can_block(self, github):
        policy = Policy(
            max_compensatable=1, over_compensatable_limit=Decision.BLOCK, minimum_score=0
        )
        assert decide(policy, [op("create_issue", title="x")] * 2, github).blocked

    def test_no_limit_means_no_limit(self, github):
        policy = Policy(minimum_score=0)
        assert decide(policy, [op("create_issue", title="x")] * 50, github).allowed

    def test_zero_limit_escalates_on_any(self, github):
        policy = Policy(max_compensatable=0, minimum_score=0)
        assert decide(policy, [op("create_issue", title="x")], github).needs_approval


class TestIrreversibleBranch:
    def test_requires_approval_by_default(self):
        decision = decide(Policy(minimum_score=0), [irreversible()])
        assert decision.needs_approval
        finding = decision.findings[0]
        assert finding.rule == "on_irreversible"
        assert "bank.wire" in finding.detail

    def test_can_be_blocked_outright(self):
        policy = Policy(minimum_score=0, on_irreversible=Decision.BLOCK)
        assert decide(policy, [irreversible()]).blocked

    def test_can_be_allowed_by_a_reckless_policy(self):
        policy = Policy(minimum_score=0, on_irreversible=Decision.ALLOW)
        assert decide(policy, [irreversible()]).allowed

    def test_irreversible_is_always_named(self):
        decision = decide(Policy(minimum_score=0), [irreversible()])
        assert "wire" in decision.summary()


class TestUnknownBranch:
    def test_unclassified_requires_approval(self, github):
        decision = decide(Policy(minimum_score=0), [op("delete_repository")], github)
        assert decision.needs_approval
        assert decision.findings[0].rule == "on_unknown"
        assert "potentially irreversible" in decision.findings[0].detail

    def test_unregistered_tool_is_unknown(self):
        decision = decide(Policy(minimum_score=0), [Operation(tool="stripe", api_call="refund")])
        assert decision.needs_approval

    def test_can_be_blocked(self, github):
        policy = Policy(minimum_score=0, on_unknown=Decision.BLOCK)
        assert decide(policy, [op("delete_repository")], github).blocked


class TestMinimumScore:
    def test_below_the_bar_blocks(self, github):
        policy = Policy(minimum_score=90.0)
        decision = decide(policy, [op("close_issue"), op("create_issue", title="x")], github)
        assert decision.blocked
        assert decision.findings[0].rule == "minimum_score"
        assert "75.0% is below the 90.0% required" in decision.findings[0].detail

    def test_at_the_bar_passes(self, github):
        policy = Policy(minimum_score=75.0)
        decision = decide(policy, [op("close_issue"), op("create_issue", title="x")], github)
        assert not decision.blocked

    def test_above_the_bar_passes(self, github):
        assert not decide(Policy(minimum_score=70.0), [op("close_issue")], github).blocked

    def test_can_be_downgraded_to_approval(self, github):
        policy = Policy(minimum_score=90.0, below_minimum_score=Decision.REQUIRE_APPROVAL)
        assert decide(policy, [op("create_issue", title="x")], github).needs_approval

    def test_score_is_out_of_range(self):
        with pytest.raises(ValueError):
            Policy(minimum_score=101)


class TestMaxTargets:
    def test_within_limit(self, github):
        policy = Policy(max_targets=2)
        plan = [op("close_issue", issue_number=1), op("close_issue", issue_number=2)]
        assert decide(policy, plan, github).allowed

    def test_over_limit_escalates(self, github):
        policy = Policy(max_targets=1)
        plan = [op("close_issue", issue_number=1), op("close_issue", issue_number=2)]
        decision = decide(policy, plan, github)
        assert decision.needs_approval
        assert decision.findings[-1].rule == "max_targets"

    def test_over_limit_can_block(self, github):
        policy = Policy(max_targets=1, over_target_limit=Decision.BLOCK)
        plan = [op("close_issue", issue_number=1), op("close_issue", issue_number=2)]
        assert decide(policy, plan, github).blocked


class TestStrictestWins:
    def test_block_beats_approval_and_allow(self, github):
        policy = Policy(minimum_score=99.0)
        plan = [op("close_issue"), op("create_issue", title="x"), irreversible()]
        decision = decide(policy, plan, github)

        assert decision.blocked
        # Every rule still reports, even the ones that would have allowed it.
        rules = {f.rule for f in decision.findings}
        assert {"minimum_score", "on_irreversible", "on_compensatable", "on_reversible"} <= rules
        assert len(decision.blocking_findings) == 1
        assert len(decision.approval_findings) == 1

    def test_approval_beats_allow(self, github):
        policy = Policy(minimum_score=0)
        decision = decide(policy, [op("close_issue"), op("delete_repository")], github)
        assert decision.needs_approval


class TestConfigLoading:
    def test_from_dict(self):
        policy = Policy.from_dict({"minimum_score": 80, "on_irreversible": "block"})
        assert policy.minimum_score == 80
        assert policy.on_irreversible is Decision.BLOCK

    def test_hyphenated_values_are_accepted(self):
        policy = Policy.from_dict({"on_irreversible": "require-approval"})
        assert policy.on_irreversible is Decision.REQUIRE_APPROVAL

    def test_uppercase_values_are_accepted(self):
        assert Policy.from_dict({"on_unknown": "BLOCK"}).on_unknown is Decision.BLOCK

    def test_from_yaml(self, tmp_path):
        path = tmp_path / "policy.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "name": "production",
                    "minimum_score": 85,
                    "max_compensatable": 3,
                    "on_irreversible": "block",
                    "on_unknown": "require_approval",
                }
            ),
            encoding="utf-8",
        )
        policy = Policy.from_yaml(path)
        assert policy.name == "production"
        assert policy.minimum_score == 85
        assert policy.max_compensatable == 3
        assert policy.on_irreversible is Decision.BLOCK

    def test_empty_yaml_is_the_default_policy(self, tmp_path):
        path = tmp_path / "empty.yaml"
        path.write_text("", encoding="utf-8")
        assert Policy.from_yaml(path) == Policy()

    def test_yaml_must_be_a_mapping(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("- just\n- a list\n", encoding="utf-8")
        with pytest.raises(ValueError, match="must contain a YAML mapping"):
            Policy.from_yaml(path)

    def test_unknown_key_is_rejected(self):
        with pytest.raises(ValueError):
            Policy.from_dict({"maximum_score": 90})

    def test_invalid_decision_is_rejected(self):
        with pytest.raises(ValueError):
            Policy.from_dict({"on_irreversible": "maybe"})

    def test_round_trips_through_yaml(self, tmp_path):
        policy = Policy(name="strict", minimum_score=95, on_unknown=Decision.BLOCK)
        path = tmp_path / "policy.yaml"
        path.write_text(policy.to_yaml(), encoding="utf-8")
        assert Policy.from_yaml(path) == policy


class TestPolicyGate:
    def test_check_changes_nothing(self, github, issue, repo_name):
        gate = PolicyGate(Policy(), github)
        before = (issue.title, issue.state)
        gate.check([op("close_issue", issue_number=issue.number)])
        assert (issue.title, issue.state) == before

    def test_enforce_allows(self, github):
        gate = PolicyGate(Policy(), github)
        assert gate.enforce([op("close_issue")]).allowed

    def test_enforce_raises_when_blocked(self, github):
        gate = PolicyGate(Policy(minimum_score=99.0), github)
        with pytest.raises(PolicyViolation) as caught:
            gate.enforce([op("create_issue", title="x")])
        assert caught.value.decision.blocked
        assert "below the 99.0% required" in str(caught.value)

    def test_enforce_raises_when_approval_is_missing(self, github):
        gate = PolicyGate(Policy(minimum_score=0), github)
        with pytest.raises(PolicyViolation):
            gate.enforce([op("delete_repository")])

    def test_enforce_proceeds_when_approved(self, github):
        gate = PolicyGate(Policy(minimum_score=0), github)
        decision = gate.enforce([op("delete_repository")], approve=lambda d: True)
        assert decision.needs_approval

    def test_enforce_raises_when_approval_is_refused(self, github):
        gate = PolicyGate(Policy(minimum_score=0), github)
        with pytest.raises(PolicyViolation):
            gate.enforce([op("delete_repository")], approve=lambda d: False)

    def test_approver_sees_the_decision(self, github):
        gate = PolicyGate(Policy(minimum_score=0), github)
        seen = []
        gate.enforce([op("delete_repository")], approve=lambda d: seen.append(d) or True)
        assert seen[0].findings[0].rule == "on_unknown"

    def test_a_block_cannot_be_approved_away(self, github):
        """Approval is for judgement calls; a block is not one."""
        gate = PolicyGate(Policy(minimum_score=99.0), github)
        with pytest.raises(PolicyViolation):
            gate.enforce([op("create_issue", title="x")], approve=lambda d: True)

    def test_default_policy_when_none_given(self):
        assert PolicyGate().policy == Policy()


class TestTrackerEnforcement:
    """The gate actually prevents execution, not just advises."""

    def test_no_policy_allows_everything(self, tracker, issue, repo_name):
        tracker.call("github", "close_issue", repo=repo_name, issue_number=issue.number)
        assert issue.state == "closed"

    def test_blocked_call_does_not_execute(self, github, issue, repo_name):
        policy = Policy(on_compensatable=Decision.BLOCK)
        tracker = Tracker(Ledger(), [github], policy=policy)
        with pytest.raises(PolicyViolation):
            tracker.call("github", "create_issue", repo=repo_name, title="Nope")

        assert issue.state == "open"
        # Nothing happened, so nothing is recorded.
        assert tracker.ledger.actions == []

    def test_blocked_call_leaves_the_target_untouched(self, github, issue, repo_name):
        tracker = Tracker(Ledger(), [github], policy=Policy(on_reversible=Decision.BLOCK))
        with pytest.raises(PolicyViolation):
            tracker.call("github", "close_issue", repo=repo_name, issue_number=issue.number)
        assert issue.state == "open"

    def test_allowed_call_proceeds_and_records(self, github, issue, repo_name):
        tracker = Tracker(Ledger(), [github], policy=Policy())
        tracker.call("github", "close_issue", repo=repo_name, issue_number=issue.number)
        assert issue.state == "closed"
        assert len(tracker.ledger) == 1

    def test_approval_gate_blocks_without_an_approver(self, github, issue, repo_name):
        policy = Policy(minimum_score=0, on_compensatable=Decision.REQUIRE_APPROVAL)
        tracker = Tracker(Ledger(), [github], policy=policy)
        with pytest.raises(PolicyViolation):
            tracker.call("github", "create_issue", repo=repo_name, title="x")
        assert tracker.ledger.actions == []

    def test_approval_gate_proceeds_with_an_approver(self, github, issue, repo_name):
        policy = Policy(minimum_score=0, on_compensatable=Decision.REQUIRE_APPROVAL)
        tracker = Tracker(Ledger(), [github], policy=policy, approve=lambda d: True)
        tracker.call("github", "create_issue", repo=repo_name, title="x")
        assert len(tracker.ledger) == 1

    def test_approver_can_refuse(self, github, repo_name):
        policy = Policy(minimum_score=0, on_compensatable=Decision.REQUIRE_APPROVAL)
        tracker = Tracker(Ledger(), [github], policy=policy, approve=lambda d: False)
        with pytest.raises(PolicyViolation):
            tracker.call("github", "create_issue", repo=repo_name, title="x")

    def test_check_policy_does_not_execute(self, github, issue, repo_name):
        tracker = Tracker(Ledger(), [github], policy=Policy())
        decision = tracker.check_policy([op("close_issue", issue_number=issue.number)])
        assert decision.allowed
        assert issue.state == "open"

    def test_aggregate_rules_do_not_fire_per_call(self, github, issue, repo_name):
        """A lone compensatable call scores 50%, but that is not a verdict on it.

        minimum_score describes a plan. If the tracker applied it one action at
        a time, every single compensatable call would be blocked inside a plan
        the same policy would happily allow.
        """
        tracker = Tracker(Ledger(), [github], policy=Policy(minimum_score=60))
        tracker.call("github", "create_issue", repo=repo_name, title="x")
        assert len(tracker.ledger) == 1

    def test_the_same_policy_still_judges_the_whole_plan(self, github, repo_name):
        tracker = Tracker(Ledger(), [github], policy=Policy(minimum_score=60))
        decision = tracker.check_policy([op("create_issue", title="x")])
        assert decision.blocked
        assert decision.findings[0].rule == "minimum_score"

    def test_class_rules_still_apply_per_call(self, github, repo_name):
        """Scope narrows the aggregate rules; it does not disarm the gate.

        (An UNKNOWN operation cannot reach the gate through a tracker: an
        integration only supports what is in its classification map, so an
        unsupported call is refused before the policy is consulted.)
        """
        policy = Policy(minimum_score=0, on_compensatable=Decision.REQUIRE_APPROVAL)
        tracker = Tracker(Ledger(), [github], policy=policy)  # no approver
        with pytest.raises(PolicyViolation):
            tracker.call("github", "create_issue", repo=repo_name, title="x")
        assert tracker.ledger.actions == []


class TestPolicyScope:
    def test_for_single_call_drops_only_the_aggregate_rules(self):
        policy = Policy(
            minimum_score=90,
            max_compensatable=2,
            max_targets=5,
            on_irreversible=Decision.BLOCK,
            on_unknown=Decision.BLOCK,
        )
        narrowed = policy.for_single_call()

        assert narrowed.minimum_score == 0.0
        assert narrowed.max_compensatable is None
        assert narrowed.max_targets is None
        # The per-class rules — the ones that mean something for one action — stay.
        assert narrowed.on_irreversible is Decision.BLOCK
        assert narrowed.on_unknown is Decision.BLOCK

    def test_the_original_policy_is_untouched(self):
        policy = Policy(minimum_score=90)
        policy.for_single_call()
        assert policy.minimum_score == 90

    def test_aggregate_rule_names_are_declared(self):
        assert set(Policy.AGGREGATE_RULES) == {
            "minimum_score",
            "max_compensatable",
            "max_targets",
        }


class TestDecisionReporting:
    def test_summary_lists_every_finding(self, github):
        policy = Policy(minimum_score=99.0)
        decision = decide(policy, [op("close_issue"), irreversible()], github)
        summary = decision.summary()
        assert "block" in summary
        assert "minimum_score" in summary
        assert "on_irreversible" in summary
        assert "blast radius" in summary

    def test_decision_survives_serialization(self, github):
        decision = decide(Policy(), [op("close_issue"), op("create_issue", title="x")], github)
        restored = PolicyDecision.model_validate(decision.model_dump(mode="json"))
        assert restored.decision is decision.decision
        assert len(restored.findings) == len(decision.findings)
        assert restored.score.coverage == decision.score.coverage

    def test_finding_describes_itself(self, github):
        decision = decide(Policy(), [op("close_issue")], github)
        assert decision.findings[0].describe().startswith("[allow] on_reversible:")
