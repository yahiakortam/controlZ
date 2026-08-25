"""Rollback: ordered, conflict-aware, and honest about what it could not undo.

Three rules govern this module.

**Order.** Actions are undone in reverse dependency order — every action that
builds on another is undone before the one it builds on. Reverse chronological
order usually satisfies that, but the dependency graph is what is actually
walked, so an out-of-order or hand-built session still unwinds correctly.

**Never overwrite a surprise.** Before restoring anything, the current state is
re-read and compared against what the ledger recorded. If they differ, the
action is marked ``CONFLICT`` and left alone until a human confirms. If the
current state cannot be read at all, that is also a conflict — an unreadable
target is not a green light.

**Say what happened.** Every action in the session appears in the report exactly
once, with an outcome it earned. An irreversible action is never quietly
dropped: it is reported as un-restored, with the reason. ``RESTORED`` means the
rollback plan ran without error — nothing else claims it.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from controlz.models import Action, Reversibility, Session, utcnow

if TYPE_CHECKING:  # pragma: no cover - typing only
    from controlz.integrations import Integration

__all__ = [
    "ConflictDetail",
    "RollbackEngine",
    "RollbackEntry",
    "RollbackOutcome",
    "RollbackReport",
    "dependency_order",
]


class RollbackOutcome(str, Enum):
    """What became of one action during a rollback.

    RESTORED
        The rollback plan ran without error.
    NOTHING_TO_DO
        The action changed nothing, so its plan has no steps.
    SKIPPED
        Not undoable: irreversible, unclassified, or carrying no plan.
    CONFLICT
        The live state no longer matches what was recorded. Left untouched.
    BLOCKED
        Something that depends on this action could not be rolled back, so
        undoing this one would be unsafe.
    FAILED
        The rollback was attempted and raised.
    PLANNED
        Dry run only: this is what would have been attempted.
    NOT_ATTEMPTED
        The run stopped early, before reaching this action.
    """

    RESTORED = "restored"
    NOTHING_TO_DO = "nothing_to_do"
    SKIPPED = "skipped"
    CONFLICT = "conflict"
    BLOCKED = "blocked"
    FAILED = "failed"
    PLANNED = "planned"
    NOT_ATTEMPTED = "not_attempted"

    @property
    def is_restored(self) -> bool:
        return self is RollbackOutcome.RESTORED


class ConflictDetail(BaseModel):
    """One field whose live value no longer matches the ledger."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(..., description="Dotted path of the field that drifted.")
    expected: Any = Field(default=None, description="What the ledger recorded.")
    actual: Any = Field(default=None, description="What the live system says now.")
    detail: str | None = Field(default=None, description="Why this matters, in words.")

    def describe(self) -> str:
        if self.detail:
            return f"{self.field}: {self.detail}"
        return f"{self.field}: recorded {self.expected!r}, found {self.actual!r}"


class RollbackEntry(BaseModel):
    """The fate of a single action, and why."""

    model_config = ConfigDict(extra="forbid")

    operation_id: str
    tool: str
    api_call: str
    outcome: RollbackOutcome
    reason: str = Field(default="", description="Plain-language explanation of the outcome.")
    reversibility: Reversibility = Reversibility.UNKNOWN
    strategy: str | None = Field(default=None, description="The rollback plan's strategy, if any.")
    conflicts: list[ConflictDetail] = Field(default_factory=list)
    error: str | None = Field(default=None, description="Exception text, when one was raised.")
    state_after_rollback: dict[str, Any] | None = Field(
        default=None, description="State re-read once the rollback ran."
    )

    @property
    def restored(self) -> bool:
        return self.outcome.is_restored

    def describe(self) -> str:
        line = f"{self.api_call} [{self.outcome.value}]"
        return f"{line}: {self.reason}" if self.reason else line


class RollbackReport(BaseModel):
    """The structured result of a rollback run.

    Every action in the session appears in :attr:`entries` exactly once.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    dry_run: bool = False
    entries: list[RollbackEntry] = Field(default_factory=list)

    # -- the four headline categories ---------------------------------------

    @property
    def restored(self) -> list[RollbackEntry]:
        """Actions whose rollback plan ran without error."""
        return self._with(RollbackOutcome.RESTORED)

    @property
    def skipped_irreversible(self) -> list[RollbackEntry]:
        """Actions that could not be undone: irreversible, unknown, or planless."""
        return self._with(RollbackOutcome.SKIPPED)

    @property
    def conflicts(self) -> list[RollbackEntry]:
        """Actions left untouched because the live state had drifted."""
        return self._with(RollbackOutcome.CONFLICT)

    @property
    def failures(self) -> list[RollbackEntry]:
        """Actions whose rollback was attempted and raised."""
        return self._with(RollbackOutcome.FAILED)

    # -- the rest -----------------------------------------------------------

    @property
    def blocked(self) -> list[RollbackEntry]:
        """Actions held back because a dependent of theirs was not rolled back."""
        return self._with(RollbackOutcome.BLOCKED)

    @property
    def nothing_to_do(self) -> list[RollbackEntry]:
        """Actions that changed nothing, so there was nothing to undo."""
        return self._with(RollbackOutcome.NOTHING_TO_DO)

    @property
    def not_attempted(self) -> list[RollbackEntry]:
        return self._with(RollbackOutcome.NOT_ATTEMPTED)

    @property
    def planned(self) -> list[RollbackEntry]:
        return self._with(RollbackOutcome.PLANNED)

    @property
    def unrestored(self) -> list[RollbackEntry]:
        """Everything that did not come back. The honest complement of :attr:`restored`."""
        return [entry for entry in self.entries if not entry.restored]

    @property
    def complete(self) -> bool:
        """True when nothing is left that a human needs to act on.

        Conflicts, failures, blocked actions, and actions the run never reached
        all make a report incomplete. Actions that were *skipped* do not: an
        irreversible action is a fact to report, not a task to retry. So a
        report can be ``complete`` while some actions were never restored —
        use :attr:`fully_restored` when that distinction matters.
        """
        return not (self.conflicts or self.failures or self.blocked or self.not_attempted)

    @property
    def fully_restored(self) -> bool:
        """True only when every action was restored or had nothing to undo.

        The strict reading: no skips, no conflicts, no failures. This is the
        one to assert on when "did everything come back?" is the question.
        """
        return all(
            entry.outcome in (RollbackOutcome.RESTORED, RollbackOutcome.NOTHING_TO_DO)
            for entry in self.entries
        )

    def _with(self, outcome: RollbackOutcome) -> list[RollbackEntry]:
        return [entry for entry in self.entries if entry.outcome is outcome]

    def entry_for(self, operation_id: str) -> RollbackEntry | None:
        for entry in self.entries:
            if entry.operation_id == operation_id:
                return entry
        return None

    def counts(self) -> dict[str, int]:
        """How many actions landed in each outcome, for logging."""
        counts: dict[str, int] = {}
        for entry in self.entries:
            counts[entry.outcome.value] = counts.get(entry.outcome.value, 0) + 1
        return counts

    def summary(self) -> str:
        """A one-paragraph account, listing everything that did not come back."""
        total = len(self.entries)
        head = f"{len(self.restored)} of {total} actions restored"
        if self.dry_run:
            head = f"dry run: {len(self.planned)} of {total} actions would be attempted"

        lines = [head]
        for label, entries in (
            ("conflicts", self.conflicts),
            ("failed", self.failures),
            ("blocked", self.blocked),
            ("not undoable", self.skipped_irreversible),
            ("not attempted", self.not_attempted),
            ("nothing to undo", self.nothing_to_do),
        ):
            for entry in entries:
                lines.append(f"  {label}: {entry.api_call} — {entry.reason}")
        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self.entries)


def dependency_order(session: Session) -> tuple[list[Action], set[str]]:
    """Order a session's actions for rollback: dependents before dependencies.

    Returns the ordered actions and the set of ``operation_id``s caught in a
    dependency cycle. Actions in a cycle are placed last, newest first, and
    reported rather than silently reordered.

    Within a layer, newest first — so an ordinary session with no declared
    dependencies unwinds in exactly reverse chronological order.
    """
    actions = list(session.actions)
    by_id = {action.operation_id: action for action in actions}
    position = {action.operation_id: index for index, action in enumerate(actions)}

    # dependents[x] = actions that declare x as a dependency, and so must go first.
    dependents: dict[str, set[str]] = {action.operation_id: set() for action in actions}
    for action in actions:
        for dependency in action.dependencies:
            if dependency in dependents:
                dependents[dependency].add(action.operation_id)

    ordered: list[Action] = []
    remaining = set(by_id)
    cycles: set[str] = set()

    while remaining:
        ready = sorted(
            (oid for oid in remaining if not dependents[oid] & remaining),
            key=lambda oid: position[oid],
            reverse=True,
        )
        if not ready:
            # Nothing can go next: everything left is in a cycle.
            cycles = set(remaining)
            ordered.extend(
                by_id[oid] for oid in sorted(remaining, key=lambda o: position[o], reverse=True)
            )
            break
        ordered.extend(by_id[oid] for oid in ready)
        remaining -= set(ready)

    return ordered, cycles


class RollbackEngine:
    """Walks a session backwards, undoing what it safely can.

    >>> engine = RollbackEngine(session, [github])        # doctest: +SKIP
    >>> report = engine.run()                             # doctest: +SKIP
    >>> print(report.summary())                           # doctest: +SKIP
    """

    def __init__(
        self,
        session: Session,
        integrations: Integration | Iterable[Integration],
        *,
        block_dependencies: bool = True,
    ) -> None:
        """
        Args:
            session: The recorded session to unwind.
            integrations: One integration, or several, keyed by their names.
            block_dependencies: When an action cannot be rolled back, hold back
                the actions it depends on too (default). Set ``False`` to undo
                each action independently.
        """
        from controlz.integrations import Integration as _Integration

        if isinstance(integrations, _Integration):
            integrations = [integrations]
        self.session = session
        self.integrations = {i.name: i for i in integrations}
        self.block_dependencies = block_dependencies

    # -- one action ---------------------------------------------------------

    def _precheck(self, action: Action) -> tuple[RollbackEntry, Integration | None]:
        """Build the entry and decide whether a rollback is even possible.

        Returns the entry, plus the integration to use — or ``None`` when the
        entry is already final and nothing should be attempted. Shared by the
        sync and async paths so their honesty rules cannot drift apart.
        """
        entry = RollbackEntry(
            operation_id=action.operation_id,
            tool=action.tool,
            api_call=action.api_call,
            outcome=RollbackOutcome.SKIPPED,
            reversibility=action.reversibility,
            strategy=action.rollback_plan.strategy if action.rollback_plan else None,
        )

        integration = self.integrations.get(action.tool)
        if integration is None:
            entry.outcome = RollbackOutcome.SKIPPED
            entry.reason = f"no integration registered for {action.tool!r}"
            return entry, None

        # 1. Is it undoable at all? Irreversible actions are reported, never dropped.
        if action.reversibility is Reversibility.IRREVERSIBLE:
            entry.reason = "classified irreversible — nothing can undo it"
            return entry, None
        if action.reversibility is Reversibility.UNKNOWN:
            entry.reason = (
                "classified unknown — treated as potentially irreversible until classified"
            )
            return entry, None

        plan = action.rollback_plan
        if plan is None:
            entry.reason = "no rollback plan was recorded for this action"
            return entry, None
        if not plan.is_executable:
            entry.outcome = RollbackOutcome.NOTHING_TO_DO
            entry.reason = f"the action changed nothing (strategy {plan.strategy!r})"
            return entry, None

        return entry, integration

    def _apply_conflicts(
        self,
        entry: RollbackEntry,
        conflicts: list[ConflictDetail],
        *,
        force: bool,
    ) -> bool:
        """Record any drift on the entry. Returns True if the rollback may proceed."""
        if conflicts:
            entry.conflicts = conflicts
            if not force:
                entry.outcome = RollbackOutcome.CONFLICT
                entry.reason = "live state no longer matches the ledger: " + "; ".join(
                    detail.describe() for detail in conflicts
                )
                return False
            entry.reason = "conflicts overridden by explicit confirmation: " + "; ".join(
                detail.describe() for detail in conflicts
            )
        return True

    @staticmethod
    def _planned(entry: RollbackEntry, action: Action) -> RollbackEntry:
        entry.outcome = RollbackOutcome.PLANNED
        strategy = action.rollback_plan.strategy if action.rollback_plan else ""
        entry.reason = entry.reason or f"would run {strategy!r}"
        return entry

    @staticmethod
    def _failed(entry: RollbackEntry, exc: BaseException) -> RollbackEntry:
        entry.outcome = RollbackOutcome.FAILED
        entry.error = f"{type(exc).__name__}: {exc}"
        entry.reason = f"rollback raised {type(exc).__name__}: {exc}"
        return entry

    @staticmethod
    def _restored(entry: RollbackEntry, action: Action) -> RollbackEntry:
        entry.outcome = RollbackOutcome.RESTORED
        strategy = action.rollback_plan.strategy if action.rollback_plan else ""
        entry.reason = entry.reason or f"ran {strategy!r}"
        return entry

    def rollback_action(
        self,
        action: Action,
        *,
        force: bool = False,
        dry_run: bool = False,
    ) -> RollbackEntry:
        """Roll back a single action and report what happened.

        Args:
            force: Proceed even if the live state has drifted. This is the
                explicit confirmation a conflict demands; without it a
                conflicted action is never overwritten.
            dry_run: Check everything, change nothing.
        """
        entry, integration = self._precheck(action)
        if integration is None:
            return entry

        # 2. Has the world moved on? Never overwrite a surprise.
        if not self._apply_conflicts(entry, integration.check_conflict(action), force=force):
            return entry

        if dry_run:
            return self._planned(entry, action)

        # 3. Do it.
        try:
            integration.execute_rollback(action)
        except Exception as exc:
            return self._failed(entry, exc)

        self._restored(entry, action)
        try:
            entry.state_after_rollback = integration.current_state(action)
        except Exception:  # pragma: no cover - the rollback itself already succeeded
            entry.state_after_rollback = None
        return entry

    async def arollback_action(
        self,
        action: Action,
        *,
        force: bool = False,
        dry_run: bool = False,
    ) -> RollbackEntry:
        """Async :meth:`rollback_action`.

        The conflict check and the undo itself are awaited; the decisions
        between them are pure computation and stay on the loop.
        """
        entry, integration = self._precheck(action)
        if integration is None:
            return entry

        conflicts = await integration.acheck_conflict(action)
        if not self._apply_conflicts(entry, conflicts, force=force):
            return entry

        if dry_run:
            return self._planned(entry, action)

        try:
            await integration.aexecute_rollback(action)
        except Exception as exc:
            return self._failed(entry, exc)

        self._restored(entry, action)
        try:
            entry.state_after_rollback = await integration.acurrent_state(action)
        except Exception:  # pragma: no cover - the rollback itself already succeeded
            entry.state_after_rollback = None
        return entry

    # -- the whole session --------------------------------------------------

    def run(
        self,
        *,
        on_conflict: Callable[[Action, list[ConflictDetail]], bool] | None = None,
        force: Sequence[str] | bool = (),
        dry_run: bool = False,
        stop_on_error: bool = False,
    ) -> RollbackReport:
        """Roll the whole session back, newest first, and report on every action.

        Args:
            on_conflict: Called with ``(action, conflicts)`` when the live state
                has drifted. Return ``True`` to proceed anyway. This is where an
                interactive confirmation prompt belongs. Without it, conflicted
                actions are left alone.
            force: ``operation_id``s to roll back despite conflicts, or ``True``
                to override every conflict. Prefer ``on_conflict``.
            dry_run: Check everything, change nothing.
            stop_on_error: Halt at the first failure. Remaining actions are
                reported as ``NOT_ATTEMPTED`` rather than omitted.
        """
        report = RollbackReport(session_id=self.session.session_id, dry_run=dry_run)
        ordered, cycles = dependency_order(self.session)
        force_all = force is True
        force_ids = set() if isinstance(force, bool) else set(force)

        # Actions that did not come back, so anything depending on them is unsafe.
        unresolved: set[str] = set()
        halted = False

        for action in ordered:
            if halted:
                report.entries.append(
                    self._trivial_entry(
                        action,
                        RollbackOutcome.NOT_ATTEMPTED,
                        "the run stopped at an earlier failure",
                    )
                )
                continue

            if action.operation_id in cycles:
                report.entries.append(
                    self._trivial_entry(
                        action,
                        RollbackOutcome.FAILED,
                        "caught in a dependency cycle; cannot determine a safe order",
                    )
                )
                unresolved.add(action.operation_id)
                continue

            blocker = self._blocking_dependent(action, unresolved)
            if blocker is not None:
                report.entries.append(
                    self._trivial_entry(
                        action,
                        RollbackOutcome.BLOCKED,
                        f"{blocker.api_call} depends on this action and was not rolled back",
                    )
                )
                unresolved.add(action.operation_id)
                continue

            confirmed = force_all or action.operation_id in force_ids
            entry = self.rollback_action(action, force=confirmed, dry_run=dry_run)

            # A conflict the caller is willing to accept gets one more try.
            if (
                entry.outcome is RollbackOutcome.CONFLICT
                and on_conflict is not None
                and on_conflict(action, entry.conflicts)
            ):
                entry = self.rollback_action(action, force=True, dry_run=dry_run)

            report.entries.append(entry)
            if entry.outcome is not RollbackOutcome.RESTORED and entry.outcome not in (
                RollbackOutcome.NOTHING_TO_DO,
                RollbackOutcome.PLANNED,
            ):
                unresolved.add(action.operation_id)
            if entry.outcome is RollbackOutcome.FAILED and stop_on_error:
                halted = True

        report.finished_at = utcnow()
        return report

    async def arun(
        self,
        *,
        on_conflict: Callable[[Action, list[ConflictDetail]], Any] | None = None,
        force: Sequence[str] | bool = (),
        dry_run: bool = False,
        stop_on_error: bool = False,
    ) -> RollbackReport:
        """Async :meth:`run`.

        Actions are unwound one at a time, in order — deliberately not
        concurrently. A rollback is a sequence of causally related undos, and
        the ordering guarantees are the point; the async version exists to keep
        the event loop free while waiting on the network, not to parallelise.

        ``on_conflict`` may return an awaitable, so an interactive confirmation
        can go and ask someone.
        """
        report = RollbackReport(session_id=self.session.session_id, dry_run=dry_run)
        ordered, cycles = dependency_order(self.session)
        force_all = force is True
        force_ids = set() if isinstance(force, bool) else set(force)

        unresolved: set[str] = set()
        halted = False

        for action in ordered:
            if halted:
                report.entries.append(
                    self._trivial_entry(
                        action,
                        RollbackOutcome.NOT_ATTEMPTED,
                        "the run stopped at an earlier failure",
                    )
                )
                continue

            if action.operation_id in cycles:
                report.entries.append(
                    self._trivial_entry(
                        action,
                        RollbackOutcome.FAILED,
                        "caught in a dependency cycle; cannot determine a safe order",
                    )
                )
                unresolved.add(action.operation_id)
                continue

            blocker = self._blocking_dependent(action, unresolved)
            if blocker is not None:
                report.entries.append(
                    self._trivial_entry(
                        action,
                        RollbackOutcome.BLOCKED,
                        f"{blocker.api_call} depends on this action and was not rolled back",
                    )
                )
                unresolved.add(action.operation_id)
                continue

            confirmed = force_all or action.operation_id in force_ids
            entry = await self.arollback_action(action, force=confirmed, dry_run=dry_run)

            if entry.outcome is RollbackOutcome.CONFLICT and on_conflict is not None:
                allowed = on_conflict(action, entry.conflicts)
                if inspect.isawaitable(allowed):
                    allowed = await allowed
                if allowed:
                    entry = await self.arollback_action(action, force=True, dry_run=dry_run)

            report.entries.append(entry)
            if entry.outcome is not RollbackOutcome.RESTORED and entry.outcome not in (
                RollbackOutcome.NOTHING_TO_DO,
                RollbackOutcome.PLANNED,
            ):
                unresolved.add(action.operation_id)
            if entry.outcome is RollbackOutcome.FAILED and stop_on_error:
                halted = True

        report.finished_at = utcnow()
        return report

    def _blocking_dependent(self, action: Action, unresolved: set[str]) -> Action | None:
        """The first action that depends on ``action`` and was not rolled back."""
        if not self.block_dependencies:
            return None
        for candidate in self.session.dependents_of(action.operation_id):
            if candidate.operation_id in unresolved:
                return candidate
        return None

    @staticmethod
    def _trivial_entry(action: Action, outcome: RollbackOutcome, reason: str) -> RollbackEntry:
        return RollbackEntry(
            operation_id=action.operation_id,
            tool=action.tool,
            api_call=action.api_call,
            outcome=outcome,
            reason=reason,
            reversibility=action.reversibility,
            strategy=action.rollback_plan.strategy if action.rollback_plan else None,
        )
