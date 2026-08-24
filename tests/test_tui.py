"""The watch window: streaming, selection, the diff, and the rewind.

Driven headlessly through Textual's pilot, so the keybinds and the rewind are
exercised the way a person would exercise them.
"""

import pytest

from controlz import Ledger, Reversibility, RollbackOutcome, Session, Tracker
from controlz.agents import DEMO_REPO, run_chaos_agent, seed_repo
from controlz.integrations.github import GitHubIntegration
from controlz.integrations.memory import InMemoryGitHub
from controlz.tui import ControlZApp
from controlz.tui.theme import REVERSIBILITY_COLOR, color_for
from controlz.tui.widgets import ActionFeed, BlastRadiusBar, DiffPane, flatten, state_diff


@pytest.fixture
def backend() -> InMemoryGitHub:
    return InMemoryGitHub()


@pytest.fixture
def tracker(backend) -> Tracker:
    return Tracker(Ledger(Session(agent="chaos-demo")), [GitHubIntegration(client=backend)])


@pytest.fixture
def chaos(tracker, backend):
    """A full 15-action chaos session, already recorded."""
    issues = seed_repo(backend)
    original = {
        name: (issue.title, issue.body, issue.state, sorted(issue.label_names))
        for name, issue in issues.items()
    }
    actions = list(run_chaos_agent(tracker, issues, delay=0))
    return {"issues": issues, "actions": actions, "original": original}


def app_for(tracker, **kwargs) -> ControlZApp:
    kwargs.setdefault("rewind_pace", 0.0)
    kwargs.setdefault("poll_interval", 0.05)
    return ControlZApp(tracker, **kwargs)


class TestChaosAgent:
    def test_records_fifteen_actions(self, chaos, tracker):
        assert len(tracker.ledger) == 15

    def test_mixture_is_worth_watching(self, chaos, tracker):
        classes = {a.reversibility for a in tracker.ledger.actions}
        assert Reversibility.REVERSIBLE in classes
        assert Reversibility.COMPENSATABLE in classes
        assert Reversibility.IRREVERSIBLE in classes

    def test_actions_really_happened(self, chaos, backend):
        issue = backend.get_repo(DEMO_REPO).issues[chaos["issues"]["alpha"].number]
        assert issue.state == "closed"
        assert "wontfix" in issue.label_names

    def test_the_irreversible_step_is_last(self, tracker, chaos):
        assert tracker.ledger.actions[-1].api_call == "wire_transfer"

    def test_can_be_run_without_the_irreversible_step(self, tracker, backend):
        issues = seed_repo(backend)
        actions = list(run_chaos_agent(tracker, issues, include_irreversible=False))
        assert len(actions) == 14


class TestFeed:
    async def test_actions_stream_in(self, tracker, chaos):
        app = app_for(tracker)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.feed.row_count == 15
            assert len(app.feed.actions) == 15

    async def test_new_actions_appear_without_a_restart(self, tracker, backend):
        app = app_for(tracker)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.feed.row_count == 0

            issues = seed_repo(backend)
            list(run_chaos_agent(tracker, issues, delay=0))
            app.sync()
            await pilot.pause()

            assert app.feed.row_count == 15

    async def test_rows_are_coloured_by_reversibility(self, tracker, chaos):
        app = app_for(tracker)
        async with app.run_test() as pilot:
            await pilot.pause()
            feed = app.feed
            for index, action in enumerate(feed.actions):
                dot = feed.get_cell_at((index, 0))
                assert dot.style == color_for(action.reversibility)

    async def test_the_irreversible_row_is_red(self, tracker, chaos):
        app = app_for(tracker)
        async with app.run_test() as pilot:
            await pilot.pause()
            feed = app.feed
            last = feed.row_count - 1
            assert (
                feed.get_cell_at((last, 0)).style
                == (REVERSIBILITY_COLOR[Reversibility.IRREVERSIBLE])
            )
            assert feed.get_cell_at((last, 4)).plain == "irreversible"

    async def test_the_feed_follows_the_newest_action(self, tracker, chaos):
        app = app_for(tracker)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.feed.cursor_row == 14
            assert app.feed.selected_action is tracker.ledger.actions[-1]


class TestSelection:
    async def test_arrow_keys_move_the_selection(self, tracker, chaos):
        app = app_for(tracker)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("up")
            await pilot.pause()
            assert app.feed.cursor_row == 13

    async def test_j_and_k_move_the_selection(self, tracker, chaos):
        app = app_for(tracker)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("k")
            await pilot.pause()
            assert app.feed.cursor_row == 13
            await pilot.press("j")
            await pilot.pause()
            assert app.feed.cursor_row == 14

    async def test_the_diff_follows_the_selection(self, tracker, chaos):
        app = app_for(tracker)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("up", "up", "up")
            await pilot.pause()
            rendered = app.diff.content.plain
            assert app.feed.selected_action.api_call in rendered


class TestDiffPane:
    def test_flatten_nests_into_dotted_paths(self):
        assert flatten({"a": {"b": 1}, "c": 2}) == {"a.b": 1, "c": 2}

    def test_flatten_of_nothing(self):
        assert flatten(None) == {}
        assert flatten({}) == {}

    def test_diff_marks_changed_fields(self, tracker, chaos):
        update = next(a for a in tracker.ledger.actions if a.api_call == "update_issue")
        rendered = state_diff(update).plain
        assert "- title" in rendered
        assert "+ title" in rendered

    def test_diff_leaves_unchanged_fields_unmarked(self, tracker, chaos):
        update = next(a for a in tracker.ledger.actions if a.api_call == "update_issue")
        rendered = state_diff(update).plain
        assert "  state" in rendered

    def test_diff_of_a_creation_shows_only_additions(self, tracker, chaos):
        created = next(a for a in tracker.ledger.actions if a.api_call == "create_issue")
        rendered = state_diff(created).plain
        assert "+ title" in rendered

    def test_diff_pane_shows_intent_and_plan(self, tracker, chaos):
        action = tracker.ledger.actions[0]
        rendered = DiffPane.build(action).plain
        assert action.intent in rendered
        assert action.rollback_plan.strategy in rendered

    def test_diff_pane_with_nothing_selected(self):
        assert "no action selected" in DiffPane.build(None).plain

    def test_diff_pane_handles_an_action_with_no_state(self):
        from controlz import Action

        rendered = DiffPane.build(Action(session_id="s", tool="t", api_call="a")).plain
        assert "no state was captured" in rendered


class TestBlastRadiusFooter:
    async def test_shows_the_score_and_tally(self, tracker, chaos):
        app = app_for(tracker)
        async with app.run_test() as pilot:
            await pilot.pause()
            rendered = app.status.content.plain
            assert "recoverable" in rendered
            assert "%" in rendered
            assert "irreversible" in rendered
            # The pane's border carries the title; the body must not repeat it.
            assert "blast radius" not in rendered

    def test_empty_session_renders(self):
        from controlz.score import reversibility_score

        assert "100.0%" in BlastRadiusBar.build(reversibility_score([])).plain


class TestRewindOneAction:
    async def test_r_rewinds_the_selected_action(self, tracker, chaos, backend):
        app = app_for(tracker)
        async with app.run_test() as pilot:
            await pilot.pause()
            # One up is the deliberate no-op; two up is the created issue.
            await pilot.press("up", "up")
            await pilot.pause()
            selected = app.feed.selected_action
            assert selected.api_call == "create_issue"

            await pilot.press("r")
            await pilot.pause(0.3)

            entry = app._entries[selected.operation_id]
            assert entry.outcome is RollbackOutcome.RESTORED

    async def test_the_row_is_marked_as_reversed(self, tracker, chaos):
        app = app_for(tracker)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("up", "up")
            await pilot.pause()
            row = app.feed.cursor_row

            await pilot.press("r")
            await pilot.pause(0.3)

            marker = app.feed.get_cell_at((row, 5))
            assert "restored" in marker.plain
            assert "↺" in marker.plain
            # and the action itself is struck through
            assert "strike" in str(app.feed.get_cell_at((row, 2)).spans)

    async def test_a_no_op_row_says_so_rather_than_claiming_a_rewind(self, tracker, chaos):
        app = app_for(tracker)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("up")  # the redundant add_labels
            await pilot.pause()
            row = app.feed.cursor_row

            await pilot.press("r")
            await pilot.pause(0.3)

            marker = app.feed.get_cell_at((row, 5))
            assert "no-op" in marker.plain
            assert "↺" not in marker.plain

    async def test_rewinding_the_irreversible_action_is_honest(self, tracker, chaos):
        app = app_for(tracker)
        async with app.run_test() as pilot:
            await pilot.pause()
            row = app.feed.cursor_row  # the wire transfer, last in the feed

            await pilot.press("r")
            await pilot.pause(0.3)

            marker = app.feed.get_cell_at((row, 5))
            assert "skipped" in marker.plain
            assert "↺" not in marker.plain
            entry = app._entries[app.feed.selected_action.operation_id]
            assert "irreversible" in entry.reason


class TestRewindSession:
    async def test_capital_r_rewinds_everything(self, tracker, chaos, backend):
        app = app_for(tracker)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("R")
            await pilot.pause(0.5)

            report = app._report
            assert report is not None
            assert len(report.restored) == 13
            assert len(report.skipped_irreversible) == 1
            assert len(report.nothing_to_do) == 1

    async def test_the_repo_is_restored_on_screen_and_in_fact(self, tracker, chaos, backend):
        alpha = chaos["issues"]["alpha"]
        original = chaos["original"]["alpha"]
        assert (alpha.title, alpha.body, alpha.state, sorted(alpha.label_names)) != original

        app = app_for(tracker)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("R")
            await pilot.pause(0.5)

        assert (alpha.title, alpha.body, alpha.state, sorted(alpha.label_names)) == original
        assert alpha.comments == {}

    async def test_every_row_gets_a_verdict(self, tracker, chaos):
        app = app_for(tracker)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("R")
            await pilot.pause(0.5)

            for index in range(app.feed.row_count):
                assert app.feed.get_cell_at((index, 5)).plain != ""

    async def test_the_footer_reports_honestly(self, tracker, chaos):
        app = app_for(tracker)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("R")
            await pilot.pause(0.5)

            rendered = app.status.content.plain
            assert "13 restored" in rendered
            assert "1 not undoable" in rendered
            assert "not everything came back" in rendered

    async def test_rewind_is_ordered_newest_first(self, tracker, chaos):
        app = app_for(tracker)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("R")
            await pilot.pause(0.5)

            order = [e.operation_id for e in app._report.entries]
            recorded = [a.operation_id for a in tracker.ledger.actions]
            assert order == list(reversed(recorded))


class TestConflictOnScreen:
    async def test_a_conflicted_action_is_flagged_not_overwritten(self, tracker, chaos, backend):
        alpha = chaos["issues"]["alpha"]
        # A human edits the title before the rewind.
        alpha.edit(title="A human was here")

        app = app_for(tracker)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("R")
            await pilot.pause(0.6)

            report = app._report
            assert len(report.conflicts) >= 1
            assert alpha.title == "A human was here"
            conflicted = report.conflicts[0]
            row = [a.operation_id for a in app.feed.actions].index(conflicted.operation_id)
            assert "conflict" in app.feed.get_cell_at((row, 5)).plain


class TestQuit:
    async def test_q_quits(self, tracker, chaos):
        app = app_for(tracker)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
        assert app.is_running is False


class TestDemoMode:
    async def test_the_demo_agent_streams_into_the_feed(self):
        backend = InMemoryGitHub()
        tracker = Tracker(Ledger(Session(agent="chaos-demo")), [GitHubIntegration(client=backend)])
        app = app_for(tracker, demo=True, demo_delay=0.0)
        async with app.run_test() as pilot:
            for _ in range(40):
                await pilot.pause(0.05)
                if app.feed.row_count == 15:
                    break
            assert app.feed.row_count == 15


class TestFileWatching:
    async def test_follows_a_ledger_file(self, tmp_path, backend):
        path = tmp_path / "run.json"
        writer = Tracker(
            Ledger(Session(agent="writer"), path=path, autosave=True),
            [GitHubIntegration(client=backend)],
        )
        issues = seed_repo(backend)

        viewer = Tracker(Ledger(path=path), [GitHubIntegration(client=backend)])
        app = app_for(viewer, ledger_path=path)
        async with app.run_test() as pilot:
            await pilot.pause()
            list(run_chaos_agent(writer, issues, delay=0))
            for _ in range(40):
                await pilot.pause(0.05)
                if app.feed.row_count == 15:
                    break
            assert app.feed.row_count == 15

    async def test_a_missing_file_is_not_fatal(self, tmp_path, tracker):
        app = app_for(tracker, ledger_path=tmp_path / "absent.json")
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.feed.row_count == 0


class TestFeedWidget:
    def test_reset_clears_everything(self):
        feed = ActionFeed()
        feed._actions.append(object())
        feed.reset()
        assert feed.actions == []

    def test_selected_action_of_an_empty_feed(self):
        assert ActionFeed().selected_action is None
