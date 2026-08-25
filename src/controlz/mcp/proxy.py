"""A ControlZ proxy in front of another MCP server.

The agent connects to this instead of the real server, and sees exactly what the
real server offers: tools, resources, and prompts all forwarded verbatim, and
only the capabilities the upstream actually declares. Every tool call passes
through the policy gate and lands in the ledger on its way, and comes back
unchanged.

    agent ──MCP──▶ ControlZ proxy ──MCP──▶ real server

Nothing about the agent changes. There is no ControlZ import in its code, no
rewritten call sites — only a different command in its MCP configuration.
"""

from __future__ import annotations

from typing import Any

from mcp import types
from mcp.server.lowlevel import Server as LowLevelServer

from controlz.ledger import Ledger
from controlz.mcp.integration import MCPIntegration, ServerSpec
from controlz.models import Operation, Session
from controlz.policy import Policy, PolicyViolation
from controlz.tracker import Tracker

__all__ = ["ControlZProxy"]


class ControlZProxy:
    """Wraps one upstream MCP session, recording everything that passes through."""

    def __init__(
        self,
        session: Any,
        *,
        spec: ServerSpec | None = None,
        ledger: Ledger | None = None,
        policy: Policy | None = None,
        approve: Any = None,
        name: str | None = None,
    ) -> None:
        """
        Args:
            session: An open MCP client for the upstream server.
            spec: What ControlZ has been told about the upstream's operations.
            ledger: Where actions are recorded. One is created if omitted.
            policy: Checked before every forwarded call. A blocked call is
                refused as a tool error and never reaches the upstream.
            approve: Called when the policy wants a human; may be async.
            name: The proxy's own server name, as the agent will see it.
        """
        self.session = session
        self.spec = spec or ServerSpec.unconfigured()
        self.integration = MCPIntegration(session, self.spec)
        # `is not None`, not `or`: Ledger defines __len__, so an empty ledger is
        # falsy and `ledger or Ledger(...)` would silently discard the caller's.
        self.ledger = (
            ledger
            if ledger is not None
            else Ledger(Session(agent="mcp-proxy", description=f"proxied {self.spec.tool}"))
        )
        self.tracker = Tracker(self.ledger, [self.integration], policy=policy, approve=approve)
        self.name = name or f"controlz({self.spec.tool})"

    def record_provenance(self, spec: str, command: list[str]) -> None:
        """Note in the ledger how this session was recorded, so it can be undone later.

        A ledger outlives the process that wrote it, and undoing an MCP session
        means relaunching the server it was recorded against. Storing that here
        is what lets `cz rollback run.json` work months later with no arguments
        beyond the file.

        Credentials are deliberately not stored. The command is, and it comes
        from the same configuration that launched the proxy in the first place.
        """
        self.ledger.session.metadata = {
            **self.ledger.session.metadata,
            "controlz": {"kind": "mcp", "spec": spec, "command": list(command)},
        }

    # -- the two handlers ---------------------------------------------------

    async def list_tools(self, _ctx: Any = None, _params: Any = None) -> types.ListToolsResult:
        """Forward the upstream's tool list, unchanged.

        Deliberately verbatim — names, descriptions, and schemas. If the proxy
        altered them the agent would behave differently through ControlZ than
        without it, and a safety layer that changes behaviour is a liability.
        """
        upstream = await self.session.list_tools()
        return types.ListToolsResult(tools=list(upstream.tools))

    async def call_tool(
        self, _ctx: Any, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        """Record, gate, forward, record."""
        operation = Operation(
            tool=self.integration.name,
            api_call=params.name,
            args=dict(params.arguments or {}),
            intent=self._intent_for(params),
        )

        try:
            tracked = await self.tracker.atrack(operation)
        except PolicyViolation as refused:
            # The call never reaches the upstream. Refusing as a tool error
            # rather than a transport error means the agent sees the reason and
            # can respond to it, instead of the connection appearing broken.
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=(f"ControlZ refused this call.\n\n{refused.decision.summary()}"),
                    )
                ],
                is_error=True,
            )

        result = tracked.result
        if isinstance(result, types.CallToolResult):
            return result
        return types.CallToolResult(
            content=list(getattr(result, "content", None) or []),
            structured_content=getattr(result, "structured_content", None),
            is_error=bool(getattr(result, "is_error", False)),
        )

    # -- everything else, forwarded untouched -------------------------------
    #
    # A proxy that quietly drops half a server is worse than no proxy: the agent
    # loses capability it would have had, and cannot tell, because the missing
    # capability is never advertised. None of these change anything, so there is
    # nothing to record — they exist so that going through ControlZ costs the
    # agent nothing.

    async def list_resources(self, _ctx: Any = None, _params: Any = None) -> Any:
        return types.ListResourcesResult(
            resources=list((await self.session.list_resources()).resources)
        )

    async def read_resource(self, _ctx: Any, params: Any) -> Any:
        upstream = await self.session.read_resource(params.uri)
        return types.ReadResourceResult(contents=list(upstream.contents))

    async def list_resource_templates(self, _ctx: Any = None, _params: Any = None) -> Any:
        upstream = await self.session.list_resource_templates()
        return types.ListResourceTemplatesResult(
            resource_templates=list(upstream.resource_templates)
        )

    async def list_prompts(self, _ctx: Any = None, _params: Any = None) -> Any:
        return types.ListPromptsResult(prompts=list((await self.session.list_prompts()).prompts))

    async def get_prompt(self, _ctx: Any, params: Any) -> Any:
        upstream = await self.session.get_prompt(params.name, params.arguments)
        return types.GetPromptResult(
            description=upstream.description, messages=list(upstream.messages)
        )

    def _intent_for(self, params: types.CallToolRequestParams) -> str | None:
        """Record the agent's stated reason, when the tool takes one."""
        spec = self.spec.operations.get(params.name)
        if spec is None or spec.intent_arg is None:
            return None
        value = (params.arguments or {}).get(spec.intent_arg)
        return str(value) if value is not None else None

    # -- checking -----------------------------------------------------------

    async def check_spec(self) -> list[str]:
        """Compare the spec against the tools the upstream really advertises."""
        upstream = await self.session.list_tools()
        return self.spec.check_against({tool.name for tool in upstream.tools})

    async def warn_about_spec(self) -> list[str]:
        """Check the spec and report any problems on stderr.

        Deliberately stderr: stdout carries the protocol, and writing anything
        else there would corrupt the stream. Deliberately a warning rather than
        a refusal: a partly wrong spec still records faithfully, and refusing to
        start would take away the recording too.
        """
        import sys

        try:
            problems = await self.check_spec()
        except Exception as exc:  # pragma: no cover - upstream may not list tools
            print(f"controlz: could not check the spec: {exc}", file=sys.stderr)
            return []
        for problem in problems:
            print(f"controlz: {problem}", file=sys.stderr)
        if problems:
            print(
                "controlz: rollbacks using those tools will fail. "
                "Check the server's tool list and fix the spec.",
                file=sys.stderr,
            )
        return problems

    # -- serving ------------------------------------------------------------

    def build_server(self) -> LowLevelServer:
        """A low-level MCP server wired to this proxy's handlers.

        The low level is the right level here: the high-level API derives tool
        schemas from Python signatures, and a proxy must pass the upstream's
        schemas through untouched.

        Handlers are registered to match the upstream's own capabilities, since
        the SDK derives what a server advertises from what it can handle.
        Registering everything would advertise capabilities the upstream may not
        have; registering only tools would hide ones it does.
        """
        server: LowLevelServer = LowLevelServer(self.name)
        capabilities = getattr(self.session, "server_capabilities", None)

        def upstream_has(name: str) -> bool:
            # Nothing to inspect — a bare session, or a test double — means
            # assume tools only, which is what every server has.
            if capabilities is None:
                return name == "tools"
            return getattr(capabilities, name, None) is not None

        if upstream_has("tools"):
            server.add_request_handler("tools/list", types.PaginatedRequestParams, self.list_tools)
            server.add_request_handler("tools/call", types.CallToolRequestParams, self.call_tool)
        if upstream_has("resources"):
            server.add_request_handler(
                "resources/list", types.PaginatedRequestParams, self.list_resources
            )
            server.add_request_handler(
                "resources/read", types.ReadResourceRequestParams, self.read_resource
            )
            server.add_request_handler(
                "resources/templates/list",
                types.PaginatedRequestParams,
                self.list_resource_templates,
            )
        if upstream_has("prompts"):
            server.add_request_handler(
                "prompts/list", types.PaginatedRequestParams, self.list_prompts
            )
            server.add_request_handler("prompts/get", types.GetPromptRequestParams, self.get_prompt)
        return server

    async def serve_stdio(self) -> None:
        """Run the proxy over stdio, the way an MCP client launches a server."""
        from mcp.server.stdio import stdio_server

        await self.warn_about_spec()
        server = self.build_server()
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
