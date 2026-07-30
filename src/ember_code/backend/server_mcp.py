"""MCP (Model Context Protocol) RPCs.

Single-class controller — :class:`McpController` — bound to the
:class:`Session` it drives. Every panel operation and every
lifecycle hook lives as a method here; ``BackendServer`` reaches
it via the ``.mcp`` cached-controller property and forwards its
own wire delegates one-liner-style.

Methods:

* :meth:`McpController.ensure` — initialise MCP connections on
  startup.
* :meth:`McpController.toggle` — connect/disconnect one server
  and rebuild the team's MCP tools so the change is live for the
  next agent turn.
* :meth:`McpController.connect` / :meth:`McpController.disconnect`
  — single-server wrappers split for panel ergonomics.
* :meth:`McpController.status` — typed :class:`McpServerStatus`
  rows for every configured server.
* :meth:`McpController.servers` — :class:`MCPServerSummary` rows
  (cheap ``(name, connected)`` projection).
* :meth:`McpController.server_details` — :class:`MCPServerSnapshot`
  rows for the fully-expanded panel row (tools + resources +
  prompts + error + policy state).
* :meth:`McpController.set_tool_enabled` — enable/disable a
  single tool on a server; state persists to
  ``<project>/.ember/mcp-tool-state.json``.

Wire shapes live in :mod:`ember_code.backend.schemas_mcp` and
are re-exported here so existing ``from
ember_code.backend.server_mcp import MCPToolToggleResult``
imports keep working.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ember_code.backend.schemas_mcp import (
    MCPServerSnapshot,
    MCPServerSummary,
    MCPToolToggleResult,
)
from ember_code.protocol import messages as msg

if TYPE_CHECKING:
    from ember_code.core.session import Session
    from ember_code.core.session.schemas import McpInitResult, McpServerStatus


__all__ = [
    "MCPServerSnapshot",
    "MCPServerSummary",
    "MCPToolToggleResult",
    "McpController",
]


class McpController:
    """MCP lifecycle + panel RPCs for a single :class:`Session`."""

    def __init__(self, session: Session) -> None:
        self._session = session

    async def ensure(self) -> McpInitResult:
        """Initialize MCP connections.

        Returns the Pattern-3 :class:`McpInitResult` envelope
        (``connected`` / ``failed`` / ``rebuilt`` / ``skipped_reason``)
        surfaced by :meth:`Session.ensure_mcp` — the RPC layer
        serialises via ``.model_dump()`` so the wire payload stays
        dict-shaped for legacy consumers that used to see ``None``."""
        return await self._session.ensure_mcp()

    async def toggle(self, server_name: str, connect: bool) -> msg.Info:
        """Connect or disconnect an MCP server."""
        mgr = self._session.mcp_manager
        if connect:
            result = await mgr.connect(server_name)
            self._session.record_mcp_result(server_name, result)
        else:
            await mgr.disconnect_one(server_name)
            self._session.record_mcp_result(server_name, None)
        self._session.rebuild_mcp()
        return msg.Info(text=f"MCP {'connected' if connect else 'disconnected'}: {server_name}")

    def status(self) -> list[McpServerStatus]:
        """Get MCP server connection status."""
        return self._session.get_mcp_status()

    def set_tool_enabled(self, server: str, tool: str, enabled: bool) -> MCPToolToggleResult:
        """Enable or disable a single tool on an MCP server.

        Disabled tools are still listed by :meth:`server_details`
        with ``disabled: true`` so the panel can render them muted,
        but they're removed from the live ``MCPTools.functions``
        dict so the next agent run won't see them. State persists
        to ``<project>/.ember/mcp-tool-state.json``.
        """
        self._session.mcp_manager.set_tool_enabled(server, tool, enabled)
        return MCPToolToggleResult(server=server, tool=tool, enabled=enabled)

    async def server_details(self) -> list[MCPServerSnapshot]:
        """Full MCP server info for the panel UI."""
        mgr = self._session.mcp_manager
        failures = self._session.mcp_failures
        return [
            await MCPServerSnapshot.from_manager(mgr, name, failures=failures)
            for name in mgr.list_servers()
        ]

    def servers(self) -> list[MCPServerSummary]:
        """MCP server info for the panel — cheaper subset of
        :meth:`server_details` used when the panel just needs the
        connected-flag column, not the tools/resources per-row
        payload."""
        mgr = self._session.mcp_manager
        return [MCPServerSummary.from_manager(mgr, name) for name in mgr.list_servers()]

    async def connect(self, server_name: str) -> msg.Info:
        """Connect a single MCP server."""
        result = await self._session.mcp_manager.connect(server_name)
        self._session.record_mcp_result(server_name, result)
        self._session.rebuild_mcp()
        return msg.Info(text=f"Connected MCP: {server_name}")

    async def disconnect(self, server_name: str) -> msg.Info:
        """Disconnect a single MCP server."""
        await self._session.mcp_manager.disconnect_one(server_name)
        self._session.record_mcp_result(server_name, None)
        self._session.rebuild_mcp()
        return msg.Info(text=f"Disconnected MCP: {server_name}")
