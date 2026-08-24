"""Validation rules on the core data model."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from controlz import Action, Reversibility, RollbackPlan, RollbackStep, Session


def make_action(session_id: str = "s1", **overrides) -> Action:
    kwargs = {
        "session_id": session_id,
        "tool": "github",
        "api_call": "create_issue",
        "args": {"repo": "acme/widgets", "title": "Broken build"},
        "intent": "File the failure the user reported.",
    }
    kwargs.update(overrides)
    return Action(**kwargs)


class TestReversibility:
    def test_values_are_stable_strings(self):
        assert Reversibility.REVERSIBLE.value == "reversible"
        assert Reversibility.COMPENSATABLE.value == "compensatable"
        assert Reversibility.IRREVERSIBLE.value == "irreversible"
        assert Reversibility.UNKNOWN.value == "unknown"

    def test_parsed_from_string(self):
        action = make_action(reversibility="compensatable")
        assert action.reversibility is Reversibility.COMPENSATABLE

    def test_unknown_value_rejected(self):
        with pytest.raises(ValidationError):
            make_action(reversibility="probably-fine")

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (Reversibility.REVERSIBLE, True),
            (Reversibility.COMPENSATABLE, True),
            (Reversibility.IRREVERSIBLE, False),
            (Reversibility.UNKNOWN, False),
        ],
    )
    def test_is_undoable(self, value, expected):
        assert value.is_undoable is expected


class TestAction:
    def test_defaults(self):
        action = make_action()
        assert action.operation_id
        assert action.reversibility is Reversibility.UNKNOWN
        assert action.rollback_plan is None
        assert action.dependencies == []
        assert action.state_before is None
        assert action.timestamp.tzinfo is not None

    def test_operation_ids_are_unique(self):
        assert make_action().operation_id != make_action().operation_id

    @pytest.mark.parametrize("field", ["session_id", "tool", "api_call"])
    def test_required_fields_reject_empty(self, field):
        with pytest.raises(ValidationError):
            make_action(**{field: ""})

    def test_missing_required_field(self):
        with pytest.raises(ValidationError):
            Action(session_id="s1", tool="github")

    def test_extra_fields_rejected(self):
        with pytest.raises(ValidationError):
            make_action(oops="typo")

    def test_naive_timestamp_is_treated_as_utc(self):
        action = make_action(timestamp=datetime(2026, 1, 2, 3, 4, 5))
        assert action.timestamp.tzinfo is timezone.utc

    def test_aware_timestamp_preserved(self):
        stamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        assert make_action(timestamp=stamp).timestamp == stamp

    def test_dependencies_deduplicated_preserving_order(self):
        action = make_action(dependencies=["a", "b", "a", "c"])
        assert action.dependencies == ["a", "b", "c"]

    def test_empty_dependency_rejected(self):
        with pytest.raises(ValidationError):
            make_action(dependencies=[""])

    def test_self_dependency_rejected(self):
        with pytest.raises(ValidationError, match="cannot depend on itself"):
            make_action(operation_id="op1", dependencies=["op1"])

    def test_irreversible_cannot_carry_executable_plan(self):
        plan = RollbackPlan(steps=[RollbackStep(tool="github", api_call="close_issue")])
        with pytest.raises(ValidationError, match="IRREVERSIBLE"):
            make_action(reversibility=Reversibility.IRREVERSIBLE, rollback_plan=plan)

    def test_irreversible_may_carry_an_empty_plan_as_documentation(self):
        plan = RollbackPlan(strategy="none", notes="Funds have settled.")
        action = make_action(reversibility=Reversibility.IRREVERSIBLE, rollback_plan=plan)
        assert action.rollback_plan.is_executable is False

    def test_assignment_is_validated(self):
        action = make_action()
        with pytest.raises(ValidationError):
            action.tool = ""


class TestRollbackPlan:
    def test_empty_plan_is_not_executable(self):
        assert RollbackPlan().is_executable is False

    def test_plan_with_steps_is_executable(self):
        plan = RollbackPlan(
            strategy="close-created-issue",
            steps=[RollbackStep(tool="github", api_call="close_issue", args={"number": 7})],
        )
        assert plan.is_executable is True
        assert plan.requires_confirmation is False

    def test_step_requires_tool_and_call(self):
        with pytest.raises(ValidationError):
            RollbackStep(tool="github", api_call="")


class TestSession:
    def test_new_session_is_empty(self):
        session = Session()
        assert session.session_id
        assert session.actions == []
        assert len(session) == 0

    def test_append_returns_action_and_preserves_order(self):
        session = Session()
        first = session.record(tool="fs", api_call="write_file")
        second = session.record(tool="fs", api_call="delete_file")
        assert [a.operation_id for a in session.actions] == [
            first.operation_id,
            second.operation_id,
        ]

    def test_record_stamps_the_session_id(self):
        session = Session()
        assert session.record(tool="fs", api_call="write_file").session_id == session.session_id

    def test_append_rejects_foreign_action(self):
        session = Session()
        with pytest.raises(ValueError, match="belongs to session"):
            session.append(make_action(session_id="somewhere-else"))

    def test_append_rejects_duplicate_operation_id(self):
        session = Session()
        action = make_action(session_id=session.session_id, operation_id="op1")
        session.append(action)
        with pytest.raises(ValueError, match="duplicate operation_id"):
            session.append(action.model_copy())

    def test_constructor_rejects_foreign_action(self):
        with pytest.raises(ValidationError, match="belongs to session"):
            Session(session_id="s1", actions=[make_action(session_id="s2")])

    def test_get_finds_and_misses(self):
        session = Session()
        action = session.record(tool="fs", api_call="write_file")
        assert session.get(action.operation_id) is action
        assert session.get("nope") is None

    def test_dependents_of(self):
        session = Session()
        first = session.record(tool="fs", api_call="write_file")
        second = session.record(tool="fs", api_call="chmod", dependencies=[first.operation_id])
        assert session.dependents_of(first.operation_id) == [second]
        assert session.dependents_of(second.operation_id) == []

    def test_undo_order_is_newest_first(self):
        session = Session()
        first = session.record(tool="fs", api_call="write_file")
        second = session.record(tool="fs", api_call="delete_file")
        assert [a.operation_id for a in session.undo_order()] == [
            second.operation_id,
            first.operation_id,
        ]
