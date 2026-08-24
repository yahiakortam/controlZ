"""Core data model for ControlZ.

Everything ControlZ can undo is described by an :class:`Action`: what the agent
did, why it did it, what the world looked like before and after, and — crucially
— whether and how it can be taken back.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "Action",
    "Operation",
    "Reversibility",
    "RollbackPlan",
    "RollbackStep",
    "Session",
    "utcnow",
]


def utcnow() -> datetime:
    """Timezone-aware UTC now. Used as the default timestamp for records."""
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid4().hex


class Reversibility(str, Enum):
    """How, if at all, an action can be undone.

    REVERSIBLE
        A direct inverse exists and restores the prior state exactly
        (delete a created file, close an opened issue).
    COMPENSATABLE
        No true inverse, but a compensating action limits the damage
        (a sent email cannot be unsent; a retraction can follow it).
    IRREVERSIBLE
        Nothing can undo or meaningfully compensate for it
        (a wire transfer that has settled, a hard-deleted production table).
    UNKNOWN
        Not yet classified. The safe default: callers should treat an
        UNKNOWN action as potentially irreversible until proven otherwise.
    """

    REVERSIBLE = "reversible"
    COMPENSATABLE = "compensatable"
    IRREVERSIBLE = "irreversible"
    UNKNOWN = "unknown"

    @property
    def is_undoable(self) -> bool:
        """True when a rollback attempt is meaningful at all."""
        return self in (Reversibility.REVERSIBLE, Reversibility.COMPENSATABLE)


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class RollbackStep(BaseModel):
    """A single call to make when undoing an action.

    Mirrors the shape of the forward action so the same executor can run both.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    tool: str = Field(..., min_length=1, description="Tool/integration performing the undo.")
    api_call: str = Field(..., min_length=1, description="Operation to invoke on that tool.")
    args: dict[str, Any] = Field(default_factory=dict, description="Arguments for the call.")
    description: str | None = Field(
        default=None, description="Human-readable summary of what this step undoes."
    )


class RollbackPlan(BaseModel):
    """Ordered recipe for undoing an action.

    Steps run in list order. An empty plan is legitimate — it means "recorded,
    but nothing to run" — so :attr:`is_executable` is the thing to check before
    handing a plan to an executor.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    strategy: str = Field(
        default="", description="Short label for the approach, e.g. 'delete-created-file'."
    )
    steps: list[RollbackStep] = Field(default_factory=list)
    notes: str | None = Field(default=None, description="Caveats, prerequisites, blast radius.")
    requires_confirmation: bool = Field(
        default=False, description="Whether a human should approve before this plan runs."
    )

    @property
    def is_executable(self) -> bool:
        return bool(self.steps)


class Operation(_Base):
    """A call an agent intends to make, before it has been made.

    The forward half of an :class:`Action`: enough to snapshot the target,
    classify the call, and execute it. The ledger records the :class:`Action`
    that results.
    """

    tool: str = Field(..., min_length=1, description="Tool/integration to use, e.g. 'github'.")
    api_call: str = Field(
        ..., min_length=1, description="Operation to invoke, e.g. 'create_issue'."
    )
    args: dict[str, Any] = Field(default_factory=dict, description="Arguments for the call.")
    intent: str | None = Field(default=None, description="Why the agent wants to do this.")


class Action(_Base):
    """One recorded operation performed by an agent."""

    operation_id: str = Field(default_factory=_new_id, description="Unique id for this action.")
    session_id: str = Field(..., min_length=1, description="Session this action belongs to.")
    timestamp: datetime = Field(default_factory=utcnow, description="When the action occurred.")

    tool: str = Field(..., min_length=1, description="Tool/integration used, e.g. 'github'.")
    api_call: str = Field(..., min_length=1, description="Operation invoked, e.g. 'create_issue'.")
    args: dict[str, Any] = Field(default_factory=dict, description="Arguments passed to the call.")
    intent: str | None = Field(default=None, description="Why the agent took this action.")

    state_before: dict[str, Any] | None = Field(
        default=None, description="Relevant state captured before the call."
    )
    state_after: dict[str, Any] | None = Field(
        default=None, description="Relevant state captured after the call."
    )

    reversibility: Reversibility = Field(
        default=Reversibility.UNKNOWN, description="Undo classification for this action."
    )
    rollback_plan: RollbackPlan | None = Field(
        default=None, description="How to undo this action, when that is possible."
    )
    dependencies: list[str] = Field(
        default_factory=list,
        description="operation_ids this action builds on; undo them after this one.",
    )

    @field_validator("timestamp")
    @classmethod
    def _ensure_aware(cls, value: datetime) -> datetime:
        """Treat naive timestamps as UTC so ordering never mixes tz-aware and naive."""
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @field_validator("dependencies")
    @classmethod
    def _dedupe_dependencies(cls, value: list[str]) -> list[str]:
        seen: dict[str, None] = {}
        for dep in value:
            if not dep:
                raise ValueError("dependency operation_ids must be non-empty")
            seen.setdefault(dep, None)
        return list(seen)

    @model_validator(mode="after")
    def _check_self_dependency(self) -> Action:
        if self.operation_id in self.dependencies:
            raise ValueError("an action cannot depend on itself")
        return self

    @model_validator(mode="after")
    def _check_irreversible_has_no_plan(self) -> Action:
        if self.reversibility is Reversibility.IRREVERSIBLE and (
            self.rollback_plan is not None and self.rollback_plan.is_executable
        ):
            raise ValueError(
                "an IRREVERSIBLE action cannot carry an executable rollback plan; "
                "classify it as COMPENSATABLE if the plan limits the damage"
            )
        return self


class Session(_Base):
    """An ordered log of actions taken by one agent run."""

    session_id: str = Field(default_factory=_new_id)
    created_at: datetime = Field(default_factory=utcnow)
    agent: str | None = Field(default=None, description="Identifier of the agent being recorded.")
    description: str | None = Field(default=None, description="What this session was for.")
    metadata: dict[str, Any] = Field(default_factory=dict)
    actions: list[Action] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_actions_belong(self) -> Session:
        seen: set[str] = set()
        for action in self.actions:
            if action.session_id != self.session_id:
                raise ValueError(
                    f"action {action.operation_id} belongs to session "
                    f"{action.session_id}, not {self.session_id}"
                )
            if action.operation_id in seen:
                raise ValueError(f"duplicate operation_id {action.operation_id}")
            seen.add(action.operation_id)
        return self

    def append(self, action: Action) -> Action:
        """Append an action to the end of the log.

        Rejects actions from another session and duplicate ``operation_id``s.
        """
        if action.session_id != self.session_id:
            raise ValueError(
                f"action {action.operation_id} belongs to session "
                f"{action.session_id}, not {self.session_id}"
            )
        if any(existing.operation_id == action.operation_id for existing in self.actions):
            raise ValueError(f"duplicate operation_id {action.operation_id}")
        # Assign through the attribute so validate_assignment re-checks invariants.
        self.actions = [*self.actions, action]
        return action

    def record(self, **kwargs: Any) -> Action:
        """Build an :class:`Action` for this session and append it."""
        kwargs.setdefault("session_id", self.session_id)
        return self.append(Action(**kwargs))

    def get(self, operation_id: str) -> Action | None:
        """Return the action with this id, or ``None``."""
        for action in self.actions:
            if action.operation_id == operation_id:
                return action
        return None

    def dependents_of(self, operation_id: str) -> list[Action]:
        """Actions that declare ``operation_id`` as a dependency."""
        return [a for a in self.actions if operation_id in a.dependencies]

    def undo_order(self) -> list[Action]:
        """Actions in the order they should be rolled back — newest first.

        Reverse chronological order already respects dependencies, since an
        action can only depend on one recorded before it.
        """
        return list(reversed(self.actions))

    def __len__(self) -> int:
        return len(self.actions)
