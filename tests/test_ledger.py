"""Ledger recording and disk round-trips."""

import json
from datetime import datetime, timezone

import pytest

from controlz import Action, Ledger, LedgerError, Reversibility, RollbackPlan, RollbackStep, Session
from controlz.ledger import SCHEMA_VERSION


@pytest.fixture
def populated() -> Ledger:
    """A ledger holding one action of each interesting shape."""
    ledger = Ledger(Session(agent="demo-agent", description="Nightly triage", metadata={"run": 3}))
    created = ledger.record(
        tool="github",
        api_call="create_issue",
        args={"repo": "acme/widgets", "title": "Broken build"},
        intent="File the failure the user reported.",
        state_before={"issue_count": 12},
        state_after={"issue_count": 13, "number": 13},
        reversibility=Reversibility.REVERSIBLE,
        rollback_plan=RollbackPlan(
            strategy="close-created-issue",
            steps=[
                RollbackStep(
                    tool="github",
                    api_call="close_issue",
                    args={"repo": "acme/widgets", "number": 13},
                    description="Close the issue we opened.",
                )
            ],
        ),
        timestamp=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
    )
    ledger.record(
        tool="email",
        api_call="send",
        args={"to": "team@acme.test"},
        reversibility=Reversibility.COMPENSATABLE,
        rollback_plan=RollbackPlan(
            strategy="send-retraction",
            steps=[RollbackStep(tool="email", api_call="send", args={"to": "team@acme.test"})],
            requires_confirmation=True,
        ),
        dependencies=[created.operation_id],
    )
    ledger.record(tool="payments", api_call="wire", reversibility=Reversibility.IRREVERSIBLE)
    return ledger


class TestRecording:
    def test_new_ledger_creates_its_own_session(self):
        ledger = Ledger()
        assert ledger.session.session_id
        assert len(ledger) == 0
        assert ledger.actions == []

    def test_record_appends_in_order(self):
        ledger = Ledger()
        first = ledger.record(tool="fs", api_call="write_file")
        second = ledger.record(tool="fs", api_call="delete_file")
        assert len(ledger) == 2
        assert ledger.actions == [first, second]

    def test_append_takes_a_prebuilt_action(self):
        ledger = Ledger()
        action = Action(session_id=ledger.session.session_id, tool="fs", api_call="write_file")
        assert ledger.append(action) is action
        assert ledger.actions == [action]

    def test_append_rejects_foreign_action(self):
        ledger = Ledger()
        with pytest.raises(ValueError, match="belongs to session"):
            ledger.append(Action(session_id="other", tool="fs", api_call="write_file"))

    def test_autosave_without_path_is_rejected(self):
        with pytest.raises(ValueError, match="autosave requires a path"):
            Ledger(autosave=True)


class TestRoundTrip:
    def test_save_then_load_preserves_everything(self, populated, tmp_path):
        path = tmp_path / "session.json"
        populated.save(path)

        reloaded = Ledger.load(path)

        assert reloaded.session == populated.session
        assert reloaded.path == path
        assert len(reloaded) == 3

    def test_reloaded_fields_survive_intact(self, populated, tmp_path):
        path = tmp_path / "session.json"
        populated.save(path)
        original = populated.actions[0]
        restored = Ledger.load(path).actions[0]

        assert restored.operation_id == original.operation_id
        assert restored.timestamp == original.timestamp
        assert restored.timestamp.tzinfo is not None
        assert restored.args == {"repo": "acme/widgets", "title": "Broken build"}
        assert restored.state_after == {"issue_count": 13, "number": 13}
        assert restored.reversibility is Reversibility.REVERSIBLE
        assert restored.rollback_plan.steps[0].args == {"repo": "acme/widgets", "number": 13}

    def test_session_metadata_survives(self, populated, tmp_path):
        path = tmp_path / "session.json"
        populated.save(path)
        session = Ledger.load(path).session
        assert session.agent == "demo-agent"
        assert session.description == "Nightly triage"
        assert session.metadata == {"run": 3}

    def test_dependencies_survive(self, populated, tmp_path):
        path = tmp_path / "session.json"
        populated.save(path)
        reloaded = Ledger.load(path)
        first, second = reloaded.actions[0], reloaded.actions[1]
        assert second.dependencies == [first.operation_id]
        assert reloaded.session.dependents_of(first.operation_id) == [second]

    def test_reloaded_ledger_can_keep_recording(self, populated, tmp_path):
        path = tmp_path / "session.json"
        populated.save(path)

        reloaded = Ledger.load(path)
        reloaded.record(tool="fs", api_call="write_file")
        reloaded.save()

        assert len(Ledger.load(path)) == 4

    def test_empty_session_round_trips(self, tmp_path):
        path = tmp_path / "empty.json"
        ledger = Ledger()
        ledger.save(path)
        assert Ledger.load(path).session == ledger.session


class TestPersistence:
    def test_save_uses_ledger_path_when_none_given(self, tmp_path):
        path = tmp_path / "session.json"
        ledger = Ledger(path=path)
        ledger.record(tool="fs", api_call="write_file")
        assert ledger.save() == path
        assert path.exists()

    def test_save_without_any_path_is_rejected(self):
        with pytest.raises(ValueError, match="no path"):
            Ledger().save()

    def test_save_adopts_the_explicit_path(self, tmp_path):
        ledger = Ledger()
        path = tmp_path / "session.json"
        ledger.save(path)
        assert ledger.path == path

    def test_save_creates_missing_parent_directories(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "session.json"
        Ledger().save(path)
        assert path.exists()

    def test_save_leaves_no_temporary_files(self, tmp_path):
        path = tmp_path / "session.json"
        Ledger().save(path)
        assert [p.name for p in tmp_path.iterdir()] == ["session.json"]

    def test_file_is_readable_json_with_a_schema_version(self, populated, tmp_path):
        path = tmp_path / "session.json"
        populated.save(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["schema_version"] == SCHEMA_VERSION
        assert data["session"]["session_id"] == populated.session.session_id
        assert len(data["session"]["actions"]) == 3
        assert data["session"]["actions"][2]["reversibility"] == "irreversible"

    def test_save_overwrites_previous_content(self, tmp_path):
        path = tmp_path / "session.json"
        first = Ledger()
        first.record(tool="fs", api_call="write_file")
        first.save(path)

        second = Ledger()
        second.save(path)

        assert len(Ledger.load(path)) == 0

    def test_autosave_persists_each_record(self, tmp_path):
        path = tmp_path / "session.json"
        ledger = Ledger(path=path, autosave=True)
        ledger.record(tool="fs", api_call="write_file")
        assert len(Ledger.load(path)) == 1
        ledger.record(tool="fs", api_call="delete_file")
        assert len(Ledger.load(path)) == 2


class TestLoadErrors:
    def test_missing_file(self, tmp_path):
        with pytest.raises(LedgerError, match="no ledger at"):
            Ledger.load(tmp_path / "absent.json")

    def test_invalid_json(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(LedgerError, match="not valid JSON"):
            Ledger.load(path)

    def test_missing_session_key(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"schema_version": SCHEMA_VERSION}), encoding="utf-8")
        with pytest.raises(LedgerError, match="missing a 'session' object"):
            Ledger.load(path)

    def test_unsupported_schema_version(self, tmp_path):
        path = tmp_path / "future.json"
        path.write_text(
            json.dumps({"schema_version": SCHEMA_VERSION + 1, "session": {}}), encoding="utf-8"
        )
        with pytest.raises(LedgerError, match="schema_version"):
            Ledger.load(path)

    def test_malformed_session_surfaces_validation_error(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "session": {"session_id": "s1", "actions": [{"tool": "fs"}]},
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
            Ledger.load(path)
