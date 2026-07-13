"""MCP (Model Context Protocol) RPCs.

Extracted from :mod:`ember_code.backend.server`. Eight free
functions taking ``BackendServer`` as arg — the class holds
one-line delegates. All operations route through
:attr:`Session.mcp_manager`:

* :func:`ensure_mcp` — initialise MCP connections on startup.
* :func:`toggle_mcp` — connect/disconnect one server. Rebuilds
  MCP tools on the team so the change is live for the next
  agent turn.
* :func:`mcp_connect` / :func:`mcp_disconnect` — single-server
  wrappers over ``toggle_mcp`` split for panel ergonomics.
* :func:`get_mcp_status` — cheap per-server connected flag.
* :func:`get_mcp_servers` — panel snapshot (name + connected).
* :func:`get_mcp_server_details` — full per-server detail for
  the expanded panel row (transport, tools, resources,
  prompts, error, policy state).
* :func:`set_mcp_tool_enabled` — enable/disable a single tool
  on a server; state persists to
  ``<project>/.ember/mcp-tool-state.json``.

Rule 2 clean — no imports needed beyond the protocol module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from ember_code.protocol import messages as msg

if TYPE_CHECKING:
    from ember_code.backend.server import BackendServer


class MCPToolToggleResult(BaseModel):
    """Wire shape for :func:`set_mcp_tool_enabled` — the panel
    reads it back to confirm the row's new state without needing
    a second RPC roundtrip."""

    server: str
    tool: str
    enabled: bool


async def ensure_mcp(backend: "BackendServer") -> None:
    """Initialize MCP connections."""
    await backend._session.ensure_mcp()


async def toggle_mcp(
    backend: "BackendServer",
    server_name: str,
    connect: bool,
) -> msg.Info:
    """Connect or disconnect an MCP server."""
    mgr = backend._session.mcp_manager
    if connect:
        await mgr.connect(server_name)
    else:
        await mgr.disconnect_one(server_name)
    backend._session.rebuild_mcp()
    return msg.Info(text=f"MCP {'connected' if connect else 'disconnected'}: {server_name}")


def get_mcp_status(backend: "BackendServer") -> list[tuple[str, bool]]:
    """Get MCP server connection status."""
    return backend._session.get_mcp_status()


def set_mcp_tool_enabled(
    backend: "BackendServer",
    server: str,
    tool: str,
    enabled: bool,
) -> MCPToolToggleResult:
    """Enable or disable a single tool on an MCP server.

    Disabled tools are still listed by :func:`get_mcp_server_details`
    with ``disabled: true`` so the panel can render them muted,
    but they're removed from the live ``MCPTools.functions``
    dict so the next agent run won't see them. State persists
    to ``<project>/.ember/mcp-tool-state.json``.
    """
    backend._session.mcp_manager.set_tool_enabled(server, tool, enabled)
    return MCPToolToggleResult(server=server, tool=tool, enabled=enabled)


async def get_mcp_server_details(backend: "BackendServer") -> list[dict]:
    """Full MCP server info for the panel UI."""
    mgr = backend._session.mcp_manager
    servers = []
    for name in mgr.list_servers():
        config = mgr.configs.get(name)
        connected = name in mgr.list_connected()
        servers.append(
            {
                "name": name,
                "connected": connected,
                "transport": config.type if config else "unknown",
                "tool_names": mgr.get_tools(name),
                "tool_descriptions": mgr.get_tool_descriptions(name),
                "tools_disabled": mgr.get_disabled_tools(name),
                "resources": await mgr.get_resources(name) if connected else [],
                "prompts": await mgr.get_prompts(name) if connected else [],
                "error": mgr.get_error(name),
                "policy_blocked": mgr._policy.is_denied(name),
            }
        )
    return servers


def get_mcp_servers(backend: "BackendServer") -> list[dict]:
    """MCP server info for the panel — cheaper subset of
    :func:`get_mcp_server_details` used when the panel just
    needs the connected-flag column, not the tools/resources
    per-row payload."""
    mgr = backend._session.mcp_manager
    servers = []
    for name in mgr.list_servers():
        connected = name in mgr.list_connected()
        servers.append({"name": name, "connected": connected})
    return servers


async def mcp_connect(backend: "BackendServer", server_name: str) -> msg.Info:
    """Connect a single MCP server."""
    await backend._session.mcp_manager.connect(server_name)
    backend._session.rebuild_mcp()
    return msg.Info(text=f"Connected MCP: {server_name}")


async def mcp_disconnect(backend: "BackendServer", server_name: str) -> msg.Info:
    """Disconnect a single MCP server."""
    await backend._session.mcp_manager.disconnect_one(server_name)
    backend._session.rebuild_mcp()
    return msg.Info(text=f"Disconnected MCP: {server_name}")
