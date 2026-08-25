"""Proxy any MCP server through ControlZ.

The protocol-level way in: instead of rewriting an agent to call ControlZ, put
ControlZ between the agent and the tools it already uses.
"""

from controlz.mcp.integration import MCPIntegration, OperationSpec, ReadSpec, ServerSpec
from controlz.mcp.proxy import ControlZProxy

__all__ = ["ControlZProxy", "MCPIntegration", "OperationSpec", "ReadSpec", "ServerSpec"]
