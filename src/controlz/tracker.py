"""The interception layer.

A :class:`Tracker` sits between an agent and its integrations. For every call it
snapshots the target, executes, snapshots again, classifies, builds a rollback
plan, and writes a complete :class:`~controlz.models.Action` to the ledger.

    tracker = Tracker(Ledger(path="run.json", autosave=True))
    tracker.register(GitHubIntegration(token=...))

    issue = tracker.call("github", "create_issue", repo="acme/widgets", title="Hi")
    # ... or, through the proxy:
    tracker.tool("github").add_labels(repo="acme/widgets", issue_number=1, labels=["bug"])
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from controlz.integrations import Integration, UnsupportedOperationError
from controlz.ledger import Ledger
from controlz.models import Action, Operation, Reversibility
from controlz.rollback import RollbackEngine, RollbackReport

__all__ = ["ToolProxy", "TrackedCall", "Tracker", "TrackingError"]


class TrackingError(RuntimeError):
    """Raised when an operation cannot be tracked at all."""


@dataclass(frozen=True)
class TrackedCall:
    """What a tracked call produced: the backend's result and the ledger entry."""

    action: Action
    result: Any


class Tracker:
    """Wraps integration calls so every one of them lands in the ledger."""

    def __init__(
        self,
        ledger: Ledger | None = None,
        integrations: list[Integration] | None = None,
        *,
        snapshot_errors: str = "record",
    ) -> None:
        """
        Args:
            ledger: Where actions are written. A fresh one is created if omitted.
            integrations: Integrations to register up front.
            snapshot_errors: What to do when a *snapshot* fails — ``"record"``
                (default) proceeds with the call and stores the error in place
                of the state, or ``"raise"`` to refuse to act blind.
        """
        if snapshot_errors not in ("record", "raise"):
            raise ValueError("snapshot_errors must be 'record' or 'raise'")
        self.ledger = ledger if ledger is not None else Ledger()
        self.snapshot_errors = snapshot_errors
        self._integrations: dict[str, Integration] = {}
        for integration in integrations or []:
            self.register(integration)

    # -- registry -----------------------------------------------------------

    def register(self, integration: Integration) -> Integration:
        """Register an integration under its :attr:`~Integration.name`."""
        if not integration.name:
            raise ValueError(f"{type(integration).__name__} declares no name")
        self._integrations[integration.name] = integration
        return integration

    def integration_for(self, tool: str) -> Integration:
        try:
            return self._integrations[tool]
        except KeyError:
            known = ", ".join(sorted(self._integrations)) or "none"
            raise TrackingError(f"no integration registered for {tool!r}; have: {known}") from None

    @property
    def tools(self) -> list[str]:
        return sorted(self._integrations)

    # -- the wrapper --------------------------------------------------------

    def call(self, tool: str, api_call: str, _intent: str | None = None, **args: Any) -> Any:
        """Perform a tracked call and return the backend's result.

        ``_intent`` is recorded as the action's intent; it carries a leading
        underscore so it cannot collide with an argument the backend expects.
        The ledger entry is available as :meth:`last_action`, or use
        :meth:`track` to get both at once.
        """
        operation = Operation(tool=tool, api_call=api_call, args=args, intent=_intent)
        return self.track(operation).result

    def track(
        self,
        operation: Operation,
        *,
        intent: str | None = None,
        dependencies: list[str] | None = None,
    ) -> TrackedCall:
        """Snapshot, execute, snapshot, classify, plan, record.

        If the call itself raises, the attempt is still recorded — classified
        ``UNKNOWN`` with no ``state_after`` and no plan, because a failed call
        may have partially landed — and the exception is re-raised.
        """
        integration = self.integration_for(operation.tool)
        if intent is not None:
            operation = operation.model_copy(update={"intent": intent})
        if not integration.supports(operation.api_call):
            raise UnsupportedOperationError(
                f"{operation.tool!r} does not support {operation.api_call!r}; "
                f"supported: {', '.join(integration.supported_operations())}"
            )

        state_before = self._snapshot(integration, operation)

        try:
            result = integration.execute(operation)
        except Exception as exc:
            self._record_failure(operation, state_before, exc, dependencies)
            raise

        state_after = self._snapshot_after(integration, operation, result)

        action = Action(
            session_id=self.ledger.session.session_id,
            tool=operation.tool,
            api_call=operation.api_call,
            args=operation.args,
            intent=operation.intent,
            state_before=state_before,
            state_after=state_after,
            reversibility=integration.classify(operation),
            dependencies=dependencies or [],
        )
        plan = integration.build_rollback_plan(action)
        if plan is not None:
            if action.reversibility is Reversibility.IRREVERSIBLE and plan.is_executable:
                # The model forbids this pairing, and the call has already run —
                # keep the ledger entry rather than raising over the plan.
                plan = plan.model_copy(
                    update={
                        "steps": [],
                        "notes": (
                            f"{plan.notes or ''} [dropped: an integration returned an "
                            "executable plan for an IRREVERSIBLE action]"
                        ).strip(),
                    }
                )
            action.rollback_plan = plan

        self.ledger.append(action)
        return TrackedCall(action=action, result=result)

    def _snapshot(self, integration: Integration, operation: Operation) -> dict[str, Any] | None:
        try:
            return integration.snapshot(operation)
        except Exception as exc:
            if self.snapshot_errors == "raise":
                raise TrackingError(
                    f"could not snapshot before {operation.api_call!r}: {exc}"
                ) from exc
            return {"error": f"snapshot failed: {exc}"}

    def _snapshot_after(
        self, integration: Integration, operation: Operation, result: Any
    ) -> dict[str, Any] | None:
        try:
            return integration.snapshot_after(operation, result)
        except Exception as exc:
            # The call already succeeded; losing the after-state must not undo that.
            return {"error": f"snapshot failed: {exc}"}

    def _record_failure(
        self,
        operation: Operation,
        state_before: dict[str, Any] | None,
        exc: BaseException,
        dependencies: list[str] | None,
    ) -> Action:
        """Record a call that raised.

        Classified UNKNOWN on purpose: a failed call may still have changed
        something, so it needs a human to look rather than an automatic undo.
        """
        action = Action(
            session_id=self.ledger.session.session_id,
            tool=operation.tool,
            api_call=operation.api_call,
            args=operation.args,
            intent=operation.intent,
            state_before=state_before,
            state_after=None,
            reversibility=Reversibility.UNKNOWN,
            dependencies=dependencies or [],
        )
        return self.ledger.append(action)

    # -- reading back -------------------------------------------------------

    def last_action(self) -> Action | None:
        """The most recently recorded action, if any."""
        return self.ledger.actions[-1] if self.ledger.actions else None

    def tool(self, name: str) -> ToolProxy:
        """A proxy whose attributes are tracked calls.

        ``tracker.tool("github").close_issue(repo=..., issue_number=...)``
        """
        self.integration_for(name)
        return ToolProxy(self, name)

    # -- undo ---------------------------------------------------------------

    @property
    def engine(self) -> RollbackEngine:
        """A rollback engine over this tracker's session and integrations."""
        return RollbackEngine(self.ledger.session, list(self._integrations.values()))

    def rollback_action(self, action: Action, *, force: bool = False, **kwargs: Any):
        """Roll back one action and return its :class:`RollbackEntry`.

        ``force=True`` is the explicit confirmation a conflict requires.
        """
        return self.engine.rollback_action(action, force=force, **kwargs)

    def rollback(self, **kwargs: Any) -> RollbackReport:
        """Roll the whole session back, newest first, and report on every action.

        Takes the same arguments as :meth:`RollbackEngine.run`.
        """
        return self.engine.run(**kwargs)


class ToolProxy:
    """Attribute-style access to one integration's tracked operations."""

    def __init__(self, tracker: Tracker, tool: str) -> None:
        self._tracker = tracker
        self._tool = tool

    def __getattr__(self, api_call: str) -> Any:
        if api_call.startswith("_"):
            raise AttributeError(api_call)
        integration = self._tracker.integration_for(self._tool)
        if not integration.supports(api_call):
            raise AttributeError(
                f"{self._tool!r} has no operation {api_call!r}; "
                f"supported: {', '.join(integration.supported_operations())}"
            )

        def _call(_intent: str | None = None, **args: Any) -> Any:
            return self._tracker.call(self._tool, api_call, _intent=_intent, **args)

        _call.__name__ = api_call
        return _call

    def __dir__(self) -> list[str]:
        return [
            *super().__dir__(),
            *self._tracker.integration_for(self._tool).supported_operations(),
        ]

    def __repr__(self) -> str:
        return f"ToolProxy(tool={self._tool!r})"
