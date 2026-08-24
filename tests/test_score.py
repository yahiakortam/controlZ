"""Reversibility scoring: the math, and the blast-radius summary."""

import pytest

from controlz import (
    Action,
    BlastRadius,
    Operation,
    Reversibility,
    ReversibilityScore,
    reversibility_score,
)
from controlz.score import DEFAULT_WEIGHTS


def op(api_call: str, **args) -> Operation:
    return Operation(tool="github", api_call=api_call, args={"repo": "acme/widgets", **args})


class TestWeights:
    def test_default_weights(self):
        assert DEFAULT_WEIGHTS[Reversibility.REVERSIBLE] == 1.0
        # Half credit: a retraction mitigates, it does not restore.
        assert DEFAULT_WEIGHTS[Reversibility.COMPENSATABLE] == 0.5
        assert DEFAULT_WEIGHTS[Reversibility.IRREVERSIBLE] == 0.0
        # Unclassified earns nothing, for the same reason as irreversible.
        assert DEFAULT_WEIGHTS[Reversibility.UNKNOWN] == 0.0


class TestCoverageMath:
    def test_empty_plan_scores_full(self):
        score = reversibility_score([])
        assert score.total == 0
        assert score.coverage == 100.0
        assert score.recoverable_share == 100.0

    def test_all_reversible_is_one_hundred(self, github):
        score = reversibility_score([op("close_issue"), op("reopen_issue")], github)
        assert score.coverage == 100.0
        assert score.reversible == 2

    def test_all_compensatable_is_fifty(self, github):
        score = reversibility_score([op("create_issue", title="x"), op("create_comment")], github)
        assert score.coverage == 50.0
        assert score.compensatable == 2

    def test_all_irreversible_is_zero(self):
        session_actions = [
            Action(
                session_id="s",
                tool="bank",
                api_call="wire",
                reversibility=Reversibility.IRREVERSIBLE,
            )
        ]
        assert reversibility_score(session_actions).coverage == 0.0

    def test_mixed_plan_is_weighted(self, github):
        # 2 reversible (1.0 each) + 1 compensatable (0.5) = 2.5 / 3 = 83.3%
        score = reversibility_score(
            [op("close_issue"), op("add_labels", labels=["x"]), op("create_comment")], github
        )
        assert score.coverage == 83.3
        assert score.recoverable == 3
        assert score.unrecoverable == 0

    def test_unknown_drags_the_score_down(self, github):
        # 1 reversible + 1 unknown = 1.0 / 2 = 50%
        score = reversibility_score([op("close_issue"), op("delete_repository")], github)
        assert score.coverage == 50.0
        assert score.unknown == 1

    def test_ninety_two_percent(self, github):
        """The worked example: 12 reversible and 1 compensatable."""
        plan = [op("close_issue") for _ in range(12)] + [op("create_comment")]
        score = reversibility_score(plan, github)
        assert score.total == 13
        assert score.coverage == 96.2  # (12 + 0.5) / 13

    def test_rounding_is_to_one_decimal(self, github):
        score = reversibility_score(
            [op("close_issue"), op("close_issue"), op("create_issue")], github
        )
        assert score.coverage == 83.3  # 2.5 / 3

    def test_custom_weights(self, github):
        """A caller who refuses to credit compensation at all."""
        strict = {**DEFAULT_WEIGHTS, Reversibility.COMPENSATABLE: 0.0}
        score = reversibility_score(
            [op("close_issue"), op("create_comment")], github, weights=strict
        )
        assert score.coverage == 50.0
        assert score.weights[Reversibility.COMPENSATABLE] == 0.0

    def test_recoverable_share_is_unweighted(self, github):
        score = reversibility_score([op("close_issue"), op("create_comment")], github)
        assert score.coverage == 75.0  # weighted
        assert score.recoverable_share == 100.0  # both have *a* way back
        assert score.fully_reversible_share == 50.0  # only one restores exactly


class TestClassification:
    def test_operations_are_classified_by_the_integration(self, github):
        score = reversibility_score([op("create_issue", title="x")], github)
        assert score.items[0].reversibility is Reversibility.COMPENSATABLE

    def test_recorded_actions_use_their_own_classification(self):
        action = Action(
            session_id="s",
            tool="github",
            api_call="create_issue",
            reversibility=Reversibility.REVERSIBLE,  # whatever the ledger says
        )
        assert reversibility_score([action]).items[0].reversibility is Reversibility.REVERSIBLE

    def test_unregistered_tool_is_unknown_not_an_error(self):
        score = reversibility_score([Operation(tool="stripe", api_call="refund")])
        assert score.unknown == 1
        assert score.coverage == 0.0

    def test_a_single_integration_need_not_be_a_list(self, github):
        assert reversibility_score([op("close_issue")], github).coverage == 100.0

    def test_counts_cover_every_item(self, github):
        plan = [op("close_issue"), op("create_issue", title="x"), op("delete_repository")]
        score = reversibility_score(plan, github)
        assert sum(score.counts.values()) == score.total == 3


class TestBlastRadius:
    def test_counts_calls_per_tool_and_operation(self, github):
        plan = [op("close_issue"), op("close_issue"), op("create_comment")]
        radius = reversibility_score(plan, github).blast_radius
        assert radius.tools == {"github": 3}
        assert radius.operations == {"close_issue": 2, "create_comment": 1}

    def test_distinct_targets(self, github):
        plan = [
            op("close_issue", issue_number=1),
            op("close_issue", issue_number=1),
            op("close_issue", issue_number=2),
        ]
        radius = reversibility_score(plan, github).blast_radius
        assert radius.targets == ["acme/widgets#1", "acme/widgets#2"]
        assert radius.target_count == 2

    def test_target_falls_back_to_the_repo(self, github):
        radius = reversibility_score([op("create_issue", title="x")], github).blast_radius
        assert radius.targets == ["acme/widgets"]

    def test_unrecoverable_items_are_named(self, github):
        plan = [op("close_issue"), op("delete_repository")]
        radius = reversibility_score(plan, github).blast_radius
        assert len(radius.unknown) == 1
        assert radius.unknown[0].api_call == "delete_repository"
        assert len(radius.unrecoverable) == 1

    def test_irreversible_and_unknown_are_listed_separately(self):
        actions = [
            Action(
                session_id="s",
                tool="bank",
                api_call="wire",
                reversibility=Reversibility.IRREVERSIBLE,
            ),
            Action(session_id="s", tool="bank", api_call="mystery"),
        ]
        radius = reversibility_score(actions).blast_radius
        assert [i.api_call for i in radius.irreversible] == ["wire"]
        assert [i.api_call for i in radius.unknown] == ["mystery"]
        assert len(radius.unrecoverable) == 2

    def test_describe_mentions_what_cannot_be_undone(self, github):
        plan = [op("close_issue"), op("delete_repository")]
        described = reversibility_score(plan, github).blast_radius.describe()
        assert "github x2" in described
        assert "1 target" in described
        assert "1 cannot be undone" in described

    def test_describe_of_an_empty_plan(self):
        assert "nothing" in BlastRadius().describe()

    def test_intent_is_carried_through(self, github):
        planned = Operation(
            tool="github",
            api_call="close_issue",
            args={"repo": "acme/widgets", "issue_number": 1},
            intent="Tidy up the backlog.",
        )
        assert reversibility_score([planned], github).items[0].intent == "Tidy up the backlog."


class TestSummary:
    def test_summary_reports_the_score_and_the_tally(self, github):
        score = reversibility_score([op("close_issue"), op("create_comment")], github)
        summary = score.summary()
        assert "75.0%" in summary
        assert "1 reversible, 1 compensatable" in summary

    def test_summary_names_unrecoverable_actions(self, github):
        score = reversibility_score([op("close_issue"), op("delete_repository")], github)
        assert "cannot be undone: github.delete_repository" in score.summary()

    def test_score_survives_serialization(self, github):
        score = reversibility_score([op("close_issue"), op("create_comment")], github)
        restored = ReversibilityScore.model_validate(score.model_dump(mode="json"))
        assert restored.coverage == score.coverage
        assert restored.counts == score.counts
        assert restored.blast_radius.targets == score.blast_radius.targets

    def test_singular_plural_in_summary(self, github):
        assert "over 1 action\n" in reversibility_score([op("close_issue")], github).summary()
        one = reversibility_score([op("close_issue")], github)
        assert "1 target" in one.blast_radius.describe()


class TestScoreIsPreExecution:
    def test_scoring_touches_nothing(self, github, fake_github, issue, repo_name):
        before = (issue.title, issue.state, list(issue.label_names))
        # The fixture already fetched the repo; scoring must add no calls of its own.
        calls_before = len(fake_github.get_repo_calls)
        reversibility_score(
            [
                op("close_issue", issue_number=issue.number),
                op("update_issue", issue_number=issue.number, title="nope"),
            ],
            github,
        )
        assert (issue.title, issue.state, list(issue.label_names)) == before
        assert len(fake_github.get_repo_calls) == calls_before

    def test_a_tracker_can_score_a_plan(self, tracker, repo_name):
        plan = [op("close_issue", issue_number=1), op("create_issue", title="x")]
        assert tracker.score(plan).coverage == 75.0


@pytest.mark.parametrize(
    ("calls", "expected"),
    [
        ([], 100.0),
        (["close_issue"], 100.0),
        (["create_issue"], 50.0),
        (["delete_repository"], 0.0),
        (["close_issue", "create_issue"], 75.0),
        (["close_issue", "close_issue", "create_issue", "delete_repository"], 62.5),
    ],
)
def test_coverage_table(github, calls, expected):
    plan = [op(call, title="x") for call in calls]
    assert reversibility_score(plan, github).coverage == expected
