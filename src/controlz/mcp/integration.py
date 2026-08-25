"""An integration whose backend is another MCP server.

Ordinary integrations are written in Python, one class per tool, because each
one needs hand-written knowledge of an API. This one is different: it knows
nothing about the tool it wraps and learns everything from configuration.

That is the point. An MCP server already exposes its tools over a protocol, so
ControlZ does not need to reimplement them — it only needs to be told which of
them can be taken back, and how. That knowledge is declarative:

.. code-block:: yaml

    tool: github-mcp
    operations:
      create_issue:
        reversibility: compensatable
        undo:
          tool: close_issue
          args: {issue_number: "$result.number"}
      update_issue:
        reversibility: reversible

Anything not listed stays ``UNKNOWN``, which the default policy refuses — so an
unconfigured proxy records faithfully and undoes nothing, rather than guessing.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from controlz.integrations import Integration, IntegrationError
from controlz.models import Action, Operation, Reversibility, RollbackPlan, RollbackStep
from controlz.rollback import ConflictDetail

__all__ = ["MCPIntegration", "OperationSpec", "ReadSpec", "ServerSpec"]


class UndoSpec(BaseModel):
    """How to undo one operation: which tool to call, and with what."""

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(..., min_length=1, description="The upstream tool that undoes this.")
    args: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Arguments for the undo call. A value of '$args.x' takes x from the "
            "original call; '$result.x' takes x from what the call returned."
        ),
    )
    description: str | None = None
    notes: str | None = None


class ReadSpec(BaseModel):
    """A read-only tool the proxy can call to capture state before a change.

    Without one, a proxy can undo a *creation* (delete the thing it made) but
    can never restore a *previous value*, because it never knew it. Naming a
    read tool is what turns the proxy from an undo-creates layer into a real
    rollback layer — and it is also the only way it can detect a conflict.
    """

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(..., min_length=1, description="A tool that reads, and changes nothing.")
    args: dict[str, Any] = Field(
        default_factory=dict, description="Arguments for the read, usually from '$args.x'."
    )


class OperationSpec(BaseModel):
    """What ControlZ has been told about one tool."""

    model_config = ConfigDict(extra="forbid")

    reversibility: Reversibility = Reversibility.UNKNOWN
    read: ReadSpec | None = Field(
        default=None, description="How to read the target's state before and after."
    )
    undo: UndoSpec | None = None
    conflict_fields: list[str] | None = Field(
        default=None,
        description=(
            "Fields to compare when checking for drift. Without this the whole "
            "reading is compared, which is safe but blocks on unrelated edits."
        ),
    )
    intent_arg: str | None = Field(
        default=None, description="An argument to record as the action's intent, if any."
    )

    def resolved_args(
        self,
        args: dict[str, Any],
        result: dict[str, Any],
        before: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fill an undo spec's placeholders from the call, its result, and prior state."""
        if self.undo is None:
            return {}
        return {
            key: _resolve(value, args, result, before or {})
            for key, value in self.undo.args.items()
        }

    def read_args(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.read is None:
            return {}
        return {key: _resolve(value, args, {}, {}) for key, value in self.read.args.items()}


def _resolve(
    value: Any,
    args: dict[str, Any],
    result: dict[str, Any],
    before: dict[str, Any] | None = None,
) -> Any:
    """Substitute a ``$args.x`` / ``$result.x`` / ``$before.x`` placeholder.

    ``$before`` is the one that matters for restoring a previous value, and it
    is only available when the operation declares a read tool.
    """
    if not isinstance(value, str) or not value.startswith("$"):
        return value
    source, _, path = value[1:].partition(".")
    root = {"args": args, "result": result, "before": before or {}}.get(source)
    if root is None:
        return value
    current: Any = root
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


class ServerSpec(BaseModel):
    """Everything ControlZ has been told about one MCP server."""

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(default="mcp", description="Name recorded as Action.tool.")
    operations: dict[str, OperationSpec] = Field(default_factory=dict)

    def referenced_tools(self) -> dict[str, list[str]]:
        """Every upstream tool this spec names, and what each is used for."""
        referenced: dict[str, list[str]] = {}
        for name, operation in self.operations.items():
            referenced.setdefault(name, []).append("operation")
            if operation.read is not None:
                referenced.setdefault(operation.read.tool, []).append(f"read for {name}")
            if operation.undo is not None:
                referenced.setdefault(operation.undo.tool, []).append(f"undo for {name}")
        return referenced

    def check_against(self, available: set[str] | list[str]) -> list[str]:
        """Report tools this spec names that the server does not actually have.

        A misspelt or absent undo tool is the worst kind of bug here: the
        classification claims the action is recoverable, the score counts it as
        recoverable, and the failure only appears when someone tries to roll
        back — the moment they can least afford a surprise.
        """
        available = set(available)
        problems = []
        for tool, uses in sorted(self.referenced_tools().items()):
            if tool not in available:
                problems.append(f"{tool!r} is not a tool on this server ({', '.join(uses)})")
        return problems

    @classmethod
    def from_yaml(cls, path: str | os.PathLike[str]) -> ServerSpec:
        import yaml

        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path} must contain a YAML mapping")
        return cls.model_validate(data)

    @classmethod
    def unconfigured(cls, tool: str = "mcp") -> ServerSpec:
        """A spec that knows nothing — everything is UNKNOWN and nothing is undoable."""
        return cls(tool=tool)


class MCPIntegration(Integration):
    """Records and undoes calls to another MCP server.

    Async by nature: the upstream is reached over the protocol, so the async
    hooks are the real implementation and the synchronous ones raise. There is
    no sensible blocking version — the proxy runs inside an event loop.
    """

    name: ClassVar[str] = "mcp"

    def __init__(self, session: Any, spec: ServerSpec | None = None) -> None:
        """
        Args:
            session: An open MCP client for the upstream server. Anything with
                an awaitable ``call_tool(name, arguments)`` will do.
            spec: What ControlZ has been told about this server's operations.
        """
        self.session = session
        self.spec = spec or ServerSpec.unconfigured()
        self.name = self.spec.tool
        self.classification = {
            api_call: operation.reversibility
            for api_call, operation in self.spec.operations.items()
        }

    # -- capability ---------------------------------------------------------
    #
    # Unlike a hand-written integration, this one must forward every tool the
    # upstream advertises, including ones it knows nothing about. `supports`
    # therefore answers "can I forward this?", which is always yes, while
    # `classification` still answers "do I know how reversible it is?".

    def supports(self, api_call: str) -> bool:  # type: ignore[override]
        return True

    def supported_operations(self) -> list[str]:  # type: ignore[override]
        return sorted(self.classification)

    def classify(self, operation: Operation) -> Reversibility:
        """Look the operation up in the configuration. Absent means UNKNOWN."""
        return self.classification.get(operation.api_call, Reversibility.UNKNOWN)

    def describe_target(self, operation: Operation) -> str:
        for key in ("repo", "repository", "path", "channel", "url", "id", "name"):
            if operation.args.get(key):
                return str(operation.args[key])
        return self.name

    # -- the async implementation -------------------------------------------

    async def aexecute(self, operation: Operation) -> Any:
        """Forward the call to the upstream server."""
        return await self.session.call_tool(operation.api_call, operation.args)

    async def _read(self, api_call: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """Call the declared read tool, if there is one. Returns None if not."""
        spec = self.spec.operations.get(api_call)
        if spec is None or spec.read is None:
            return None
        result = await self.session.call_tool(spec.read.tool, spec.read_args(args))
        return _result_payload(result)

    async def asnapshot(self, operation: Operation) -> dict[str, Any] | None:
        """Capture prior state, if the operation declares a way to read it.

        A protocol that only exposes "call this tool" gives no generic way to
        ask what a tool is about to change, so this is only as good as the
        configuration. Without a declared read tool the before-state records
        the request alone, and ``captured`` says so rather than implying more.
        """
        before = await self._read(operation.api_call, operation.args)
        state: dict[str, Any] = {
            "tool": operation.api_call,
            "args": dict(operation.args),
            "captured": before is not None,
        }
        if before is not None:
            state["before"] = before
        return state

    async def asnapshot_after(self, operation: Operation, result: Any) -> dict[str, Any] | None:
        state: dict[str, Any] = {
            "tool": operation.api_call,
            "args": dict(operation.args),
            "result": _result_payload(result),
        }
        after = await self._read(operation.api_call, operation.args)
        state["captured"] = after is not None
        if after is not None:
            state["after"] = after
        return state

    async def acheck_conflict(self, action: Action) -> list[ConflictDetail]:
        """Re-read the target and compare, when the operation declares a read tool.

        Without one there is nothing to compare, and the rollback proceeds
        unchecked — a real weakness against a hand-written integration, and one
        the plan's notes state plainly rather than hiding.
        """
        recorded = (action.state_after or {}).get("after")
        if recorded is None:
            return []

        try:
            current = await self._read(action.api_call, action.args)
        except Exception as exc:
            return [
                ConflictDetail(
                    field="<state>",
                    detail=f"could not re-read the target: {exc}",
                    expected="readable state",
                )
            ]
        if current is None:
            return []

        spec = self.spec.operations.get(action.api_call)
        fields = spec.conflict_fields if spec else None
        if fields is None:
            # No guidance, so compare everything the read returns. Safe, and it
            # will refuse on edits a narrower comparison would have allowed.
            fields = sorted(set(recorded) | set(current))

        return [
            ConflictDetail(
                field=field,
                expected=recorded.get(field),
                actual=current.get(field),
                detail=f"{field} changed after this action ran",
            )
            for field in fields
            if recorded.get(field) != current.get(field)
        ]

    async def aexecute_rollback(self, action: Action) -> None:
        if action.tool != self.name:
            raise IntegrationError(
                f"action {action.operation_id} belongs to {action.tool!r}, not {self.name!r}"
            )
        plan = action.rollback_plan or self.build_rollback_plan(action)
        if plan is None or not plan.is_executable:
            raise IntegrationError(f"no executable rollback plan for {action.operation_id}")
        await self.aexecute_rollback_plan(action)

    # -- planning (pure computation, so it stays synchronous) ---------------

    def build_rollback_plan(self, action: Action) -> RollbackPlan | None:
        spec = self.spec.operations.get(action.api_call)
        if spec is None or spec.undo is None:
            return None
        if action.state_after is None:
            return None

        result = (action.state_after or {}).get("result") or {}
        before = (action.state_before or {}).get("before") or {}
        args = spec.resolved_args(action.args, result, before)
        if spec.undo.notes:
            notes = spec.undo.notes
        elif spec.read is None:
            notes = (
                "Undo declared in configuration. No read tool is declared for this "
                "operation, so prior state was never captured and the rollback is "
                "not conflict-checked."
            )
        else:
            notes = "Undo declared in configuration, against state read before the call."
        return RollbackPlan(
            strategy=f"declared-undo:{spec.undo.tool}",
            steps=[
                RollbackStep(
                    tool=self.name,
                    api_call=spec.undo.tool,
                    args=args,
                    description=spec.undo.description
                    or f"Call {spec.undo.tool} to undo {action.api_call}.",
                )
            ],
            notes=notes,
        )

    # -- the synchronous half does not exist --------------------------------

    _SYNC_MESSAGE = (
        "MCPIntegration talks to its upstream over the protocol and is async only; "
        "use the a-prefixed methods (aexecute, asnapshot, arollback...)."
    )

    def snapshot(self, operation: Operation) -> dict[str, Any] | None:
        raise IntegrationError(self._SYNC_MESSAGE)

    def execute(self, operation: Operation) -> Any:
        raise IntegrationError(self._SYNC_MESSAGE)

    def execute_rollback(self, action: Action) -> None:
        raise IntegrationError(self._SYNC_MESSAGE)


def _result_payload(result: Any) -> dict[str, Any]:
    """Reduce an MCP tool result to something JSON-serializable for the ledger."""
    if result is None:
        return {}

    payload: dict[str, Any] = {}
    texts = [
        block.text
        for block in getattr(result, "content", None) or []
        if getattr(block, "text", None) is not None
    ]
    if texts:
        payload["text"] = "\n".join(texts)

    fields = _structured_fields(result)
    if fields is None and texts:
        # Tool results are very often JSON in a text block. Parsing it is what
        # lets a declared undo reference "$result.id" instead of scraping text.
        fields = _maybe_json(texts[0])
    if isinstance(fields, dict):
        payload.update(fields)

    if getattr(result, "is_error", False):
        payload["is_error"] = True
    return payload


def _structured_fields(result: Any) -> dict[str, Any] | None:
    """The result's own fields, unwrapped.

    A tool that returns a bare value is wrapped by the protocol as
    ``{"result": <value>}``. Unwrapping that is what lets a configuration say
    ``$result.id`` rather than the uglier and more brittle ``$result.result.id``.
    """
    structured = getattr(result, "structured_content", None)
    if not isinstance(structured, dict):
        return None
    if set(structured) == {"result"}:
        inner = structured["result"]
        if isinstance(inner, str):
            parsed = _maybe_json(inner)
            if isinstance(parsed, dict):
                return parsed
        if isinstance(inner, dict):
            return inner
    return structured


def _maybe_json(text: str) -> Any:
    import json

    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None
