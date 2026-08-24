"""The integration interface every backend implements.

An integration knows four things about the operations it supports: what the
world looks like before one runs (:meth:`Integration.snapshot`), how reversible
it is (:meth:`Integration.classify`), how to undo it
(:meth:`Integration.build_rollback_plan`), and how to run that undo
(:meth:`Integration.execute_rollback`).

It also knows how to perform the forward call itself (:meth:`Integration.execute`),
which is what lets :class:`~controlz.tracker.Tracker` sit in the middle of an
agent's call and record both sides of it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from controlz.models import Action, Operation, Reversibility, RollbackPlan

__all__ = ["Integration", "IntegrationError", "UnsupportedOperationError"]

# Concrete integrations are imported lazily by name; see controlz.integrations.github.


class IntegrationError(RuntimeError):
    """Raised when an integration cannot carry out a request."""


class UnsupportedOperationError(IntegrationError):
    """Raised when an integration is handed an ``api_call`` it does not implement."""


class Integration(ABC):
    """Abstract base for anything ControlZ can record and undo.

    Subclasses declare :attr:`name` (the value that appears as ``Action.tool``)
    and :attr:`classification`, a hardcoded map from ``api_call`` to
    :class:`~controlz.models.Reversibility`. Anything absent from that map is
    :attr:`~controlz.models.Reversibility.UNKNOWN` — and therefore treated as
    potentially irreversible.
    """

    name: ClassVar[str] = ""
    classification: ClassVar[dict[str, Reversibility]] = {}

    # -- capability ---------------------------------------------------------

    @classmethod
    def supported_operations(cls) -> list[str]:
        """The ``api_call`` names this integration implements."""
        return sorted(cls.classification)

    @classmethod
    def supports(cls, api_call: str) -> bool:
        return api_call in cls.classification

    def _require_supported(self, api_call: str) -> None:
        if not self.supports(api_call):
            raise UnsupportedOperationError(
                f"{self.name!r} does not support {api_call!r}; "
                f"supported: {', '.join(self.supported_operations())}"
            )

    # -- the interface ------------------------------------------------------

    @abstractmethod
    def snapshot(self, operation: Operation) -> dict[str, Any] | None:
        """Capture the state this operation is about to change.

        Returns the ``state_before`` of the resulting action, or ``None`` when
        there is nothing meaningful to capture. Must not mutate anything.
        """

    @abstractmethod
    def classify(self, operation: Operation) -> Reversibility:
        """Classify how reversible this operation is.

        Implementations look the answer up in a hardcoded table — no inference.
        """

    @abstractmethod
    def build_rollback_plan(self, action: Action) -> RollbackPlan | None:
        """Build the plan for undoing a recorded action.

        Called after the action has run, so it can use ``state_before`` and
        ``state_after``. Returns ``None`` when no plan can be built.
        """

    @abstractmethod
    def execute_rollback(self, action: Action) -> None:
        """Run the action's rollback plan against the live system."""

    @abstractmethod
    def execute(self, operation: Operation) -> Any:
        """Perform the forward call and return the backend's raw result.

        Not part of the undo contract, but required for the tracker to wrap an
        operation rather than merely observe one.
        """

    # -- optional hooks -----------------------------------------------------

    def snapshot_after(self, operation: Operation, result: Any) -> dict[str, Any] | None:
        """Capture state once the operation has run.

        Defaults to re-running :meth:`snapshot`, which is correct whenever the
        operation's arguments identify the target. Operations that *create* the
        thing they touch override this to read the identifier off ``result``.
        """
        return self.snapshot(operation)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r})"
