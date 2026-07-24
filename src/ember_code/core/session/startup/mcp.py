"""MCP first-connect + rebuild phase.

Owns the once-per-session ``_initialized`` flag: :meth:`ensure`
is the SOLE writer, so the "who flipped this to True" question
has exactly one answer. Every other reader (Session's
``_mcp_initialized`` compat property, backend / test writes)
routes through the property so the invariant holds.

Both entry points return a typed :class:`McpInitResult` /
``None`` — replacing the pre-refactor grep-parse-the-log
diagnostic path.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, ClassVar

from ember_code.core.session.schemas import McpClientBundle, McpInitResult
from ember_code.core.session.startup.base import SessionStartupPhase

if TYPE_CHECKING:
    from ember_code.core.session.core import Session

logger = logging.getLogger(__name__)


class McpInitPhase(SessionStartupPhase):
    """Once-per-session MCP first-connect + agent rebuild.

    Sole owner of the ``_initialized`` flag (Rule AP3 — one write
    site: :meth:`ensure`). Session exposes ``mcp_initialized`` as
    a property that routes through this phase so external
    toggles (test fixtures, ``backend/server_mcp``) still work.
    """

    _MCP_CONNECT_INFO: ClassVar[str] = "MCP init: connecting %d server(s): %s"

    def __init__(self, session: Session) -> None:
        super().__init__(session)
        self._initialized: bool = False

    @property
    def initialized(self) -> bool:
        """Whether the first-connect pass has run for this session."""
        return self._initialized

    @initialized.setter
    def initialized(self, value: bool) -> None:
        self._initialized = value

    async def ensure(self) -> McpInitResult:
        """Connect user-configured MCP servers and rebuild agents.

        Reads from .mcp.json / .ember/.mcp.json. No auto-detection —
        only servers the user explicitly configured are connected.
        Runs once on first message. INFO-level log lines bracket
        each connect so the timeline is reconstructable from
        ``~/.ember/debug.log`` when diagnosing a "MCP says connected
        but the agent doesn't see the tools" race.

        Returns a :class:`McpInitResult` describing the outcome so
        callers can branch on ``connected`` / ``failed`` /
        ``rebuilt`` / ``skipped_reason`` without log-scraping.
        """
        if self._initialized:
            return McpInitResult(skipped_reason="already_initialized")
        # Single write site — every other reader routes through
        # ``self.initialized`` / Session's compat property.
        self._initialized = True

        session = self.session
        available = session.mcp_manager.list_servers()
        if not available:
            logger.info("MCP init: no configured servers; skipping connect loop")
            return McpInitResult(skipped_reason="no_configured_servers")

        logger.info(self._MCP_CONNECT_INFO, len(available), available)
        bundle, failed = await self._connect_all(available)

        if not bundle:
            logger.info("MCP init: no clients to attach; team rebuild skipped")
            return McpInitResult(
                connected=[],
                failed=failed,
                rebuilt=False,
                skipped_reason="no_clients_connected",
            )

        self._rebuild(bundle)
        return McpInitResult(
            connected=bundle.names,
            failed=failed,
            rebuilt=True,
        )

    async def _connect_all(self, available: list[str]) -> tuple[McpClientBundle, dict[str, str]]:
        """Connect every configured MCP server, logging per-server
        timing and tool-count. Returns the connected-clients bundle
        alongside a ``name → error string`` map for the failed set.

        Also mirrors the failure map into ``session.mcp_failures``
        so the ``/mcp`` status command and the panel snapshot can
        render the error column without a manager side-channel.
        """
        session = self.session
        clients: dict[str, Any] = {}
        failed: dict[str, str] = {}
        for name in available:
            t0 = time.monotonic()
            result = await session.mcp_manager.connect(name)
            elapsed = time.monotonic() - t0
            session.record_mcp_result(name, result)
            if result.ok:
                client = result.client
                # Tool count surfaces the most common silent-failure
                # mode: server-side gating on auth that returns zero
                # tools. We let the connect succeed but flag the
                # empty case explicitly.
                tool_count = len(getattr(client, "functions", None) or {})
                logger.info(
                    "MCP init: connected '%s' in %.2fs (%d tool(s))",
                    name,
                    elapsed,
                    tool_count,
                )
                clients[name] = client
            else:
                error = result.reason or "unknown error"
                failed[name] = error
                logger.info(
                    "MCP init: connection to '%s' failed after %.2fs: %s",
                    name,
                    elapsed,
                    error,
                )
                session.display.print_info(f"MCP '{name}' connection failed: {error}")
        return McpClientBundle(clients=clients), failed

    def _rebuild(self, bundle: McpClientBundle) -> None:
        """Rebuild the agent pool + main team with MCP tools included.

        Split out of :meth:`ensure` so the "have clients → rebuild"
        step is a single call the failure branches skip cleanly.
        """
        session = self.session
        logger.info(
            "MCP init: rebuilding agents + main team with %d MCP client(s)",
            len(bundle),
        )
        session.pool.build_agents(mcp_clients=bundle.clients)
        session.rebuild_main_team()
        logger.info("MCP init: agents + main team rebuilt — tools active")

    def rebuild_current(self) -> None:
        """Rebuild agents and main team with the CURRENT MCP client set.

        Called after toggling individual MCP servers on/off. Prefers
        the public :meth:`MCPClientManager.get_client` accessor so
        the phase never reaches into a private attribute; falls back
        to the legacy ``_clients`` dict lookup only when the manager
        stub predates the public accessor (test fixtures).
        """
        session = self.session
        connected = session.mcp_manager.list_connected()
        clients: dict[str, Any] = {}
        getter = getattr(session.mcp_manager, "get_client", None)
        for name in connected:
            if callable(getter):
                client = getter(name)
            else:
                # Legacy path — test fixtures with a stubbed manager
                # that lacks ``get_client``. Real production callers
                # get the public accessor.
                client = getattr(session.mcp_manager, "_clients", {}).get(name)
            if client is not None:
                clients[name] = client
        session.pool.build_agents(mcp_clients=clients if clients else None)
        session.rebuild_main_team()
