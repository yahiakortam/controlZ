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

import inspect
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from controlz.integrations import Integration, UnsupportedOperationError
from controlz.ledger import Ledger
from controlz.models import Action, Operation, Reversibility
from controlz.policy import Policy, PolicyDecision, PolicyGate, PolicyViolation
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
        policy: Policy | None = None,
        approve: Callable[[PolicyDecision], bool] | None = None,
    ) -> None:
        """
        Args:
            ledger: Where actions are written. A fresh one is created if omitted.
            integrations: Integrations to register up front.
            snapshot_errors: What to do when a *snapshot* fails — ``"record"``
                (default) proceeds with the call and stores the error in place
                of the state, or ``"raise"`` to refuse to act blind.
            policy: Checked before every call. A blocked call raises
                :class:`~controlz.policy.PolicyViolation` and is not recorded,
                because it never happened.
            approve: Called with the decision when the policy wants a human.
                Return ``True`` to let the call proceed.
        """
        if snapshot_errors not in ("record", "raise"):
            raise ValueError("snapshot_errors must be 'record' or 'raise'")
        self.ledger = ledger if ledger is not None else Ledger()
        self.snapshot_errors = snapshot_errors
        self.policy = policy
        self.approve = approve
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

        # One call is judged on its class alone; the whole plan is judged by
        # check_policy(), which is the one that sees aggregate coverage.
        self.enforce_policy([operation], scope="call")

        state_before = self._snapshot(integration, operation)

        try:
            result = integration.execute(operation)
        except Exception as exc:
            self._record_failure(operation, state_before, exc, dependencies)
            raise

        state_after = self._snapshot_after(integration, operation, result)

        action = self._build_action(integration, operation, state_before, state_after, dependencies)
        self.ledger.append(action)
        return TrackedCall(action=action, result=result)

    def _build_action(
        self,
        integration: Integration,
        operation: Operation,
        state_before: dict[str, Any] | None,
        state_after: dict[str, Any] | None,
        dependencies: list[str] | None,
    ) -> Action:
        """Classify, plan, and assemble the ledger entry.

        Pure computation over state already in hand — no I/O — so the sync and
        async paths share it rather than keeping two copies in step.
        """
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
        return action

    def _prepare(self, operation: Operation, intent: str | None) -> tuple[Integration, Operation]:
        """Resolve the integration and refuse anything it does not support."""
        integration = self.integration_for(operation.tool)
        if intent is not None:
            operation = operation.model_copy(update={"intent": intent})
        if not integration.supports(operation.api_call):
            raise UnsupportedOperationError(
                f"{operation.tool!r} does not support {operation.api_call!r}; "
                f"supported: {', '.join(integration.supported_operations())}"
            )
        return integration, operation

    # -- the async wrapper --------------------------------------------------

    async def acall(self, tool: str, api_call: str, _intent: str | None = None, **args: Any) -> Any:
        """Async :meth:`call`."""
        operation = Operation(tool=tool, api_call=api_call, args=args, intent=_intent)
        return (await self.atrack(operation)).result

    async def atrack(
        self,
        operation: Operation,
        *,
        intent: str | None = None,
        dependencies: list[str] | None = None,
    ) -> TrackedCall:
        """Async :meth:`track`.

        Identical in behaviour, including recording a failed call as ``UNKNOWN``
        before re-raising. Only the three steps that touch the outside world —
        both snapshots and the call itself — are awaited; classification,
        planning, and the append happen on the event loop, where no other
        coroutine can interleave with them.
        """
        integration, operation = self._prepare(operation, intent)
        await self.aenforce_policy([operation], scope="call")

        state_before = await self._asnapshot(integration, operation)

        try:
            result = await integration.aexecute(operation)
        except Exception:
            # A call that raised may still have partially landed, so it is
            # recorded before the exception continues on its way.
            await self.ledger.aappend(self._failed_action(operation, state_before, dependencies))
            raise

        state_after = await self._asnapshot_after(integration, operation, result)

        action = self._build_action(integration, operation, state_before, state_after, dependencies)
        await self.ledger.aappend(action)
        return TrackedCall(action=action, result=result)

    async def _asnapshot(
        self, integration: Integration, operation: Operation
    ) -> dict[str, Any] | None:
        try:
            return await integration.asnapshot(operation)
        except Exception as exc:
            if self.snapshot_errors == "raise":
                raise TrackingError(
                    f"could not snapshot before {operation.api_call!r}: {exc}"
                ) from exc
            return {"error": f"snapshot failed: {exc}"}

    async def _asnapshot_after(
        self, integration: Integration, operation: Operation, result: Any
    ) -> dict[str, Any] | None:
        try:
            return await integration.asnapshot_after(operation, result)
        except Exception as exc:
            # The call already succeeded; losing the after-state must not undo that.
            return {"error": f"snapshot failed: {exc}"}

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
        return self.ledger.append(self._failed_action(operation, state_before, dependencies))

    def _failed_action(
        self,
        operation: Operation,
        state_before: dict[str, Any] | None,
        dependencies: list[str] | None,
    ) -> Action:
        return Action(
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

    # -- policy ---------------------------------------------------------------

    @property
    def gate(self) -> PolicyGate:
        """A policy gate over this tracker's policy and integrations."""
        return PolicyGate(self.policy, list(self._integrations.values()))

    def score(self, operations: Iterable[Operation]):
        """Score a proposed plan without running any of it."""
        return self.gate.score(operations)

    def check_policy(self, operations: Iterable[Operation]) -> PolicyDecision:
        """Score a proposed plan and apply the policy, changing nothing."""
        return self.gate.check(operations)

    def enforce_policy(
        self, operations: Iterable[Operation], *, scope: str = "task"
    ) -> PolicyDecision | None:
        """Apply the policy to a plan, raising if it may not proceed.

        ``scope="call"`` applies only the per-class rules, which are the ones
        that mean anything for a single action; see
        :meth:`~controlz.policy.Policy.for_single_call`. A tracker with no
        policy allows everything, which is the default.
        """
        if self.policy is None:
            return None
        policy = self.policy.for_single_call() if scope == "call" else self.policy
        return PolicyGate(policy, list(self._integrations.values())).enforce(
            operations, approve=self.approve
        )

    async def aenforce_policy(
        self, operations: Iterable[Operation], *, scope: str = "task"
    ) -> PolicyDecision | None:
        """Async :meth:`enforce_policy`.

        Scoring and rule evaluation are pure computation, so they run here. The
        one thing that may genuinely need to await is the approver — asking a
        human usually means a network round trip — so an ``approve`` callback
        returning an awaitable is awaited.
        """
        if self.policy is None:
            return None
        policy = self.policy.for_single_call() if scope == "call" else self.policy
        gate = PolicyGate(policy, list(self._integrations.values()))
        decision = gate.check(operations)
        if decision.blocked:
            raise PolicyViolation(decision)
        if decision.needs_approval:
            approved = False
            if self.approve is not None:
                approved = self.approve(decision)
                if inspect.isawaitable(approved):
                    approved = await approved
            if not approved:
                raise PolicyViolation(decision)
        return decision

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

    async def arollback_action(self, action: Action, *, force: bool = False, **kwargs: Any):
        """Async :meth:`rollback_action`."""
        return await self.engine.arollback_action(action, force=force, **kwargs)

    async def arollback(self, **kwargs: Any) -> RollbackReport:
        """Async :meth:`rollback`."""
        return await self.engine.arun(**kwargs)


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
