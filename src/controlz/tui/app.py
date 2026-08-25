"""``controlz watch`` — the live feed, the diff, and the rewind.

The app watches a session and streams each action into the feed as it lands.
Selecting a row shows what that action changed; ``r`` rewinds that one action,
``R`` rewinds the whole session. The rewind is deliberately paced so it reads as
an animation rather than a repaint — you watch the session come undone.

Two sources of actions:

* a ledger file on disk, polled for changes (``controlz watch run.json``)
* the built-in chaos agent, run in-process against an in-memory GitHub
  (``controlz watch --demo``), which needs no credentials
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.widgets import Footer, Static

from controlz.ledger import Ledger
from controlz.models import Action, Session
from controlz.rollback import RollbackEngine, RollbackEntry, RollbackReport
from controlz.score import reversibility_score
from controlz.tracker import Tracker
from controlz.tui.theme import INK
from controlz.tui.widgets import ActionFeed, BlastRadiusBar, DiffPane

__all__ = ["ControlZApp"]


class ControlZApp(App[None]):
    """The ControlZ watch window."""

    TITLE = "ControlZ"
    SUB_TITLE = "every action, and the way back"

    CSS = f"""
    Screen {{
        background: {INK["ground"]};
        color: {INK["text"]};
    }}
    #body {{
        height: 1fr;
    }}
    #feed-pane {{
        width: 5fr;
        border: round {INK["line"]};
        border-title-color: {INK["dim"]};
        padding: 0 1;
    }}
    #diff-pane {{
        width: 4fr;
        border: round {INK["line"]};
        border-title-color: {INK["dim"]};
        padding: 1 2;
        overflow-y: auto;
    }}
    ActionFeed {{
        background: {INK["ground"]};
        height: 1fr;
        scrollbar-size-horizontal: 0;
    }}
    ActionFeed > .datatable--cursor {{
        background: {INK["panel"]};
        text-style: bold;
    }}
    ActionFeed > .datatable--header {{
        background: {INK["ground"]};
        color: {INK["dim"]};
    }}
    #status {{
        height: 5;
        border: round {INK["line"]};
        border-title-color: {INK["dim"]};
        padding: 0 2;
    }}
    #banner {{
        height: 1;
        padding: 0 1;
        color: {INK["dim"]};
    }}
    Footer {{
        background: {INK["panel"]};
    }}
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("up,k", "cursor_up", "select", show=False),
        Binding("down,j", "cursor_down", "select", show=False),
        Binding("r", "rollback_selected", "rewind action"),
        Binding("R,shift+r", "rollback_session", "rewind session"),
        Binding("q", "quit", "quit"),
    ]

    #: Seconds between rows during the rewind. The point is to be watchable.
    rewind_pace: float = 0.18

    def __init__(
        self,
        tracker: Tracker | None = None,
        *,
        ledger_path: str | Path | None = None,
        demo: bool = False,
        demo_delay: float = 0.35,
        poll_interval: float = 0.25,
        rewind_pace: float | None = None,
    ) -> None:
        super().__init__()
        self.tracker = tracker if tracker is not None else Tracker()
        self.ledger_path = Path(ledger_path) if ledger_path else None
        self.demo = demo
        self.demo_delay = demo_delay
        self.poll_interval = poll_interval
        if rewind_pace is not None:
            self.rewind_pace = rewind_pace
        self._shown: set[str] = set()
        self._entries: dict[str, RollbackEntry] = {}
        self._report: RollbackReport | None = None
        self._mtime: float | None = None
        self._rewinding = False
        self._panes_ready = False

    # -- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(self._banner(), id="banner")
        with Horizontal(id="body"):
            with Vertical(id="feed-pane") as feed_pane:
                feed_pane.border_title = "actions"
                yield ActionFeed(id="feed")
            with Vertical(id="diff-pane") as diff_pane:
                diff_pane.border_title = "before / after"
                yield DiffPane(id="diff")
        yield BlastRadiusBar(id="status")
        yield Footer()

    def _banner(self) -> Text:
        session = self.session
        line = Text()
        line.append("ControlZ  ", style=f"bold {INK['bright']}")
        line.append(f"session {session.session_id[:8]}", style=INK["dim"])
        if session.agent:
            line.append(f"  ·  {session.agent}", style=INK["dim"])
        if self.ledger_path:
            line.append(f"  ·  {self.ledger_path}", style=INK["dim"])
        return line

    # -- state --------------------------------------------------------------

    @property
    def _live(self) -> bool:
        """True only while the panes actually exist.

        False before compose finishes and again during shutdown, when queued
        messages are still being dispatched against a screen that is already
        being torn down.
        """
        if not self._panes_ready:
            return False
        try:
            self.query_one("#feed", ActionFeed)
        except NoMatches:
            return False
        return True

    @property
    def session(self) -> Session:
        return self.tracker.ledger.session

    @property
    def feed(self) -> ActionFeed:
        return self.query_one("#feed", ActionFeed)

    @property
    def diff(self) -> DiffPane:
        return self.query_one("#diff", DiffPane)

    @property
    def status(self) -> BlastRadiusBar:
        return self.query_one("#status", BlastRadiusBar)

    def on_ready(self) -> None:
        """Start polling only once the panes exist.

        Everything here touches children, so it cannot run at mount time: the
        poll timer would otherwise race compose and query a feed that is not
        there yet.
        """
        self.query_one("#status", BlastRadiusBar).border_title = "blast radius"
        self.diff.show(None)
        self._panes_ready = True
        self.refresh_status()
        self.sync()
        self.set_interval(self.poll_interval, self.sync)
        if self.demo:
            self.run_demo_agent()

    # -- streaming ----------------------------------------------------------

    def sync(self) -> None:
        """Pull in anything new: from the ledger file, or from the running agent."""
        if not self._live:
            return
        if self.ledger_path is not None:
            self._reload_file()
        new = [a for a in self.session.actions if a.operation_id not in self._shown]
        if not new:
            return
        for action in new:
            self._shown.add(action.operation_id)
            self.feed.add_action(action)
        self.show_selected()
        self.refresh_status()

    def _reload_file(self) -> None:
        try:
            mtime = self.ledger_path.stat().st_mtime
        except OSError:
            return
        if self._mtime is not None and mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            ledger = Ledger.load(self.ledger_path)
        except Exception:
            return  # a half-written file; the next poll will catch it
        self.tracker.ledger = ledger

    @work(thread=True, exclusive=True)
    def run_demo_agent(self) -> None:
        """Run the chaos agent in the background so its actions stream in."""
        from controlz.agents import run_chaos_agent, seed_repo

        integration = self.tracker.integration_for("github")
        issues = seed_repo(integration.client)
        for _ in run_chaos_agent(self.tracker, issues, delay=self.demo_delay):
            if not self.is_running:
                return

    # -- selection ----------------------------------------------------------

    def show_selected(self) -> None:
        if not self._live:
            return
        action = self.feed.selected_action
        entry = self._entries.get(action.operation_id) if action else None
        self.diff.show(action, entry)

    def action_cursor_up(self) -> None:
        """Delegate to the feed, so j/k work wherever focus happens to be."""
        self.feed.action_cursor_up()

    def action_cursor_down(self) -> None:
        self.feed.action_cursor_down()

    def on_data_table_row_highlighted(self, _event: Any) -> None:
        self.show_selected()

    def refresh_status(self) -> None:
        if not self._live:
            return
        score = reversibility_score(self.session.actions, list(self.tracker._integrations.values()))
        self.status.show(score, self._report)

    # -- the rewind ---------------------------------------------------------

    @property
    def engine(self) -> RollbackEngine:
        return RollbackEngine(self.session, list(self.tracker._integrations.values()))

    def action_rollback_selected(self) -> None:
        action = self.feed.selected_action
        if action is None or self._rewinding:
            return
        self.rewind([action], whole_session=False)

    def action_rollback_session(self) -> None:
        if self._rewinding or not self.session.actions:
            return
        from controlz.rollback import dependency_order

        ordered, _ = dependency_order(self.session)
        self.rewind(ordered, whole_session=True)

    @work(exclusive=True)
    async def rewind(self, actions: list[Action], *, whole_session: bool) -> None:
        """Undo actions one at a time, marking each row as it goes.

        Paced on purpose: the sequence of rows striking through in reverse is
        the whole point of watching a rollback rather than reading about one.
        """
        self._rewinding = True
        engine = self.engine
        entries: list[RollbackEntry] = []
        try:
            for action in actions:
                entry = await engine.arollback_action(action)
                entries.append(entry)
                self._entries[entry.operation_id] = entry
                self.feed.mark(entry)
                self.show_selected()
                await asyncio.sleep(self.rewind_pace)
        finally:
            self._rewinding = False

        if whole_session:
            report = RollbackReport(session_id=self.session.session_id, entries=entries)
            self._report = report
            self.refresh_status()
            self.notify(
                report.summary(),
                title="rewind complete" if report.fully_restored else "rewind incomplete",
                severity="information" if report.complete else "warning",
                timeout=10,
            )
        else:
            self.refresh_status()
            entry = entries[0]
            self.notify(
                entry.reason,
                title=f"{entry.api_call}: {entry.outcome.value.replace('_', ' ')}",
                severity="information" if entry.restored else "warning",
                timeout=6,
            )
