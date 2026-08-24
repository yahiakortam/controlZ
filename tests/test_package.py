"""The package imports and exposes its public surface."""

import controlz


def test_version_is_exposed():
    assert isinstance(controlz.__version__, str)
    assert controlz.__version__


def test_public_names_are_importable():
    for name in controlz.__all__:
        assert hasattr(controlz, name), name


def test_end_to_end_smoke(tmp_path):
    """The deliverable in one flow: create, append, save, reload."""
    ledger = controlz.Ledger(controlz.Session(agent="smoke"))
    action = ledger.record(
        tool="fs",
        api_call="write_file",
        args={"path": "notes.md"},
        intent="Save the user's notes.",
        reversibility=controlz.Reversibility.REVERSIBLE,
        rollback_plan=controlz.RollbackPlan(
            strategy="delete-created-file",
            steps=[
                controlz.RollbackStep(tool="fs", api_call="delete_file", args={"path": "notes.md"})
            ],
        ),
    )

    path = ledger.save(tmp_path / "smoke.json")
    reloaded = controlz.Ledger.load(path)

    assert len(reloaded) == 1
    assert reloaded.actions[0] == action
    assert reloaded.actions[0].reversibility.is_undoable
