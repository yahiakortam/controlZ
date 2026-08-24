"""The three panes: the feed, the diff, and the blast-radius readout."""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual.widgets import DataTable, Static

from controlz.models import Action, Reversibility
from controlz.rollback import RollbackEntry, RollbackOutcome, RollbackReport
from controlz.score import ReversibilityScore
from controlz.tui.theme import INK, color_for, outcome_color

__all__ = ["ActionFeed", "BlastRadiusBar", "DiffPane", "flatten", "state_diff"]

MARK = "●"  # ●  the reversibility dot: the signature of the whole interface
REWIND = "↺"  # ↺


class ActionFeed(DataTable):
    """The live feed. One row per action, coloured by how reversible it is."""

    #: (header, width). Fixed and deliberately tight: the feed must never need a
    #: horizontal scrollbar, because the rollback column is the one that matters.
    COLUMNS = (
        ("", 1),
        ("#", 2),
        ("action", 21),
        ("on", 4),
        ("class", 13),
        ("rollback", 11),
    )

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.cursor_type = "row"
        self.zebra_stripes = False
        # The cell's own colour must survive the cursor: reversibility is the
        # one thing in this interface that colour is allowed to mean.
        self.cursor_foreground_priority = "renderable"
        self._actions: list[Action] = []
        self._outcomes: dict[str, RollbackEntry] = {}

    def on_mount(self) -> None:
        for index, (name, width) in enumerate(self.COLUMNS):
            self.add_column(name, key=f"col{index}", width=width)

    # -- content ------------------------------------------------------------

    @property
    def actions(self) -> list[Action]:
        return list(self._actions)

    @property
    def selected_action(self) -> Action | None:
        if not self._actions:
            return None
        row = self.cursor_row
        if row is None or not (0 <= row < len(self._actions)):
            return None
        return self._actions[row]

    def add_action(self, action: Action) -> None:
        """Append one action and move the cursor to it, so the feed follows along."""
        self._actions.append(action)
        self.add_row(*self._cells(action), key=action.operation_id)
        self.move_cursor(row=len(self._actions) - 1)

    def _cells(self, action: Action) -> list[Text]:
        color = color_for(action.reversibility)
        entry = self._outcomes.get(action.operation_id)
        reversed_ = entry is not None and entry.outcome is RollbackOutcome.RESTORED

        label = Text(f"{action.tool}.{action.api_call}")
        if reversed_:
            label.stylize(f"strike {INK['dim']}")

        target = Text(self._target(action), style=INK["dim"])
        if reversed_:
            target.stylize("strike")

        return [
            Text(MARK, style=color),
            Text(str(len(self._actions)), style=INK["dim"]),
            label,
            target,
            Text(action.reversibility.value, style=color),
            self._rollback_cell(entry),
        ]

    #: Kept short so a verdict never truncates. The diff pane carries the full reason.
    OUTCOME_LABEL: ClassVar[dict[RollbackOutcome, str]] = {
        RollbackOutcome.RESTORED: "restored",
        RollbackOutcome.NOTHING_TO_DO: "no-op",
        RollbackOutcome.SKIPPED: "skipped",
        RollbackOutcome.CONFLICT: "conflict",
        RollbackOutcome.BLOCKED: "blocked",
        RollbackOutcome.FAILED: "failed",
        RollbackOutcome.PLANNED: "planned",
        RollbackOutcome.NOT_ATTEMPTED: "untried",
    }

    @classmethod
    def _rollback_cell(cls, entry: RollbackEntry | None) -> Text:
        if entry is None:
            return Text("", style=INK["dim"])
        label = cls.OUTCOME_LABEL.get(entry.outcome, entry.outcome.value)
        symbol = REWIND if entry.outcome is RollbackOutcome.RESTORED else "·"
        return Text(f"{symbol} {label}", style=outcome_color(entry.outcome))

    @staticmethod
    def _target(action: Action) -> str:
        """Just the issue number — the repository is already in the banner."""
        number = action.args.get("issue_number")
        if number:
            return f"#{number}"
        return "—" if action.args.get("repo") else ""

    # -- the rewind ---------------------------------------------------------

    def mark(self, entry: RollbackEntry) -> None:
        """Mark one row with its rollback outcome. This is the rewind, one row at a time."""
        self._outcomes[entry.operation_id] = entry
        for index, action in enumerate(self._actions):
            if action.operation_id != entry.operation_id:
                continue
            for column, cell in enumerate(self._cells(action)):
                if column == 1:
                    cell = Text(str(index + 1), style=INK["dim"])
                self.update_cell_at((index, column), cell)
            self.move_cursor(row=index)
            return

    def clear_marks(self) -> None:
        self._outcomes.clear()

    def reset(self) -> None:
        self.clear()
        self._actions.clear()
        self._outcomes.clear()


def flatten(state: dict[str, Any] | None, prefix: str = "") -> dict[str, Any]:
    """Flatten a nested snapshot into dotted paths, so two can be compared field by field."""
    if not state:
        return {}
    flat: dict[str, Any] = {}
    for key, value in state.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            nested = flatten(value, f"{path}.")
            flat.update(nested or {path: {}})
        else:
            flat[path] = value
    return flat


def state_diff(action: Action) -> Text:
    """Render before → after for one action, field by field.

    Changed fields are shown as a red ``-`` line and a green ``+`` line;
    unchanged fields stay dim, so the eye lands on what moved.
    """
    before = flatten(action.state_before)
    after = flatten(action.state_after)

    body = Text()
    if not before and not after:
        return Text("no state was captured for this action", style=INK["dim"])

    for path in sorted(set(before) | set(after)):
        old, new = before.get(path, None), after.get(path, None)
        label = path.split(".", 1)[-1] if path.count(".") else path

        # A permalink that did not change is noise in a pane about what did.
        if old == new and label.endswith("url"):
            continue

        if old == new:
            body.append(f"  {label:<14}", style=INK["dim"])
            body.append(f"{_render(old)}\n", style=INK["dim"])
            continue

        if path in before:
            body.append(f"- {label:<14}", style="#f85149")
            body.append(f"{_render(old)}\n", style="#f85149")
        if path in after:
            body.append(f"+ {label:<14}", style="#3fb950")
            body.append(f"{_render(new)}\n", style="#3fb950")
    return body


def _render(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, list):
        return "[" + ", ".join(str(item) for item in value) + "]" if value else "[]"
    text = str(value)
    return text if len(text) <= 42 else text[:41] + "…"


class DiffPane(Static):
    """Before and after for the selected action, plus its intent and rollback plan."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.content: Text = Text()

    def show(self, action: Action | None, entry: RollbackEntry | None = None) -> None:
        """Render the pane and remember what it says, so it can be asserted on."""
        self.content = self.build(action, entry)
        self.update(self.content)

    @staticmethod
    def build(action: Action | None, entry: RollbackEntry | None = None) -> Text:
        if action is None:
            return Text("no action selected", style=INK["dim"])

        color = color_for(action.reversibility)
        body = Text()
        body.append(f"{action.tool}.{action.api_call}\n", style=f"bold {INK['bright']}")
        body.append(f"{MARK} {action.reversibility.value}\n", style=color)
        if action.intent:
            body.append(f'"{action.intent}"\n', style=f"italic {INK['dim']}")
        body.append("\n")
        body.append(state_diff(action))

        plan = action.rollback_plan
        body.append("\nrollback  ", style=INK["dim"])
        if plan is None:
            body.append("none recorded\n", style=INK["dim"])
        elif not plan.is_executable:
            body.append(f"{plan.strategy} (nothing to run)\n", style=INK["dim"])
        else:
            body.append(f"{plan.strategy}\n", style=INK["text"])
            for step in plan.steps:
                body.append(f"          {step.tool}.{step.api_call}\n", style=INK["dim"])

        if entry is not None:
            body.append("\n")
            body.append(
                f"{entry.outcome.value.replace('_', ' ')}: {entry.reason}\n",
                style=outcome_color(entry.outcome),
            )
        return body


class BlastRadiusBar(Static):
    """The footer: a proportional bar, the tally, and — after a rewind — the verdict."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.content: Text = Text()

    def show(self, score: ReversibilityScore, report: RollbackReport | None = None) -> None:
        self.content = self.build(score, report)
        self.update(self.content)

    @staticmethod
    def build(score: ReversibilityScore, report: RollbackReport | None = None) -> Text:
        body = Text()
        body.append(f"{score.coverage}%", style=f"bold {_score_color(score.coverage)}")
        body.append(f"  recoverable  ({score.total} actions)\n", style=INK["dim"])

        body.append(_bar(score))
        body.append("  ")
        for count, reversibility in (
            (score.reversible, Reversibility.REVERSIBLE),
            (score.compensatable, Reversibility.COMPENSATABLE),
            (score.irreversible, Reversibility.IRREVERSIBLE),
            (score.unknown, Reversibility.UNKNOWN),
        ):
            if count:
                body.append(f"{count} {reversibility.value}  ", style=color_for(reversibility))

        if report is not None:
            body.append("\n")
            body.append(
                f"rewound  {len(report.restored)} restored",
                style=color_for(Reversibility.REVERSIBLE),
            )
            for label, entries, reversibility in (
                ("conflicts", report.conflicts, Reversibility.COMPENSATABLE),
                ("blocked", report.blocked, Reversibility.COMPENSATABLE),
                ("failed", report.failures, Reversibility.IRREVERSIBLE),
                ("not undoable", report.skipped_irreversible, Reversibility.IRREVERSIBLE),
                ("nothing to undo", report.nothing_to_do, Reversibility.UNKNOWN),
            ):
                if entries:
                    body.append(f"  ·  {len(entries)} {label}", style=color_for(reversibility))
            if not report.fully_restored:
                body.append("   not everything came back", style=f"italic {INK['dim']}")
        return body


def _score_color(coverage: float) -> str:
    if coverage >= 90:
        return color_for(Reversibility.REVERSIBLE)
    if coverage >= 50:
        return color_for(Reversibility.COMPENSATABLE)
    return color_for(Reversibility.IRREVERSIBLE)


def _bar(score: ReversibilityScore, width: int = 32) -> Text:
    """A proportional bar: one block per action, coloured by class."""
    bar = Text()
    if not score.total:
        return Text("─" * width, style=INK["line"])
    for reversibility in (
        Reversibility.REVERSIBLE,
        Reversibility.COMPENSATABLE,
        Reversibility.IRREVERSIBLE,
        Reversibility.UNKNOWN,
    ):
        count = score.count(reversibility)
        if not count:
            continue
        blocks = max(1, round(count / score.total * width))
        bar.append("█" * blocks, style=color_for(reversibility))
    return bar
