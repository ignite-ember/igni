"""MCP auto-(dis)connect helpers driven by plugin state changes.

Called by :class:`Session` after `PluginLoader.enable` /
`disable` reshuffles the contributed MCP-server set. Split out
from ``session/core.py`` so the god-file has fewer top-level
concerns — each function takes the session as an argument and
delegates to ``session.mcp_manager`` + ``session.rebuild_mcp``.

Both functions iterate sequentially on purpose:

* **Auto-disconnect** flushes any final tool-call cleanup per
  server before moving to the next. Parallel would risk two
  disconnect handshakes racing on the same MCP process.
* **Auto-connect** doesn't parallelise because the first-use
  approval prompt is a modal UI — firing N at once would stack
  permission dialogs on the user.

After a successful batch, the session's main team is rebuilt
so the live ``Agent`` instance's tool surface reflects the new
MCP-client set. Skipping this step is the source of the "I can
call tool X" hallucination against a server that's gone (or the
"I don't have access" against a server that just came up).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ember_code.core.session.core import Session

logger = logging.getLogger(__name__)


async def disconnect_removed_mcps(session: "Session", names: set[str]) -> None:
    """Disconnect MCP servers whose owning plugin was just disabled
    or removed. Rebuilds the main team once any server actually
    disconnected."""
    logger.info(
        "Auto-disconnect: stopping %d MCP server(s): %s",
        len(names),
        sorted(names),
    )
    any_disconnected = False
    for name in sorted(names):
        try:
            ok = await session.mcp_manager.disconnect_one(name)
            if ok:
                logger.info("Auto-disconnect: '%s' stopped", name)
                any_disconnected = True
            else:
                # Wasn't in ``_clients`` — typical if the server
                # failed to connect earlier. The config removal
                # still happened upstream; nothing else to do.
                logger.info(
                    "Auto-disconnect: '%s' wasn't connected — no-op",
                    name,
                )
        except Exception:
            logger.warning(
                "Auto-disconnect of MCP server '%s' failed",
                name,
                exc_info=True,
            )

    # Rebuild even on a no-op stop set IF something was actually
    # live — the team needs the new (smaller) tool surface attached
    # or the model will still try to call the disconnected server's
    # tools.
    if any_disconnected:
        logger.info("Auto-disconnect: rebuilding main team to drop stale MCP tools")
        session.rebuild_mcp()
        logger.info("Auto-disconnect: main team rebuilt")


async def auto_connect_mcps(session: "Session", names: set[str]) -> None:
    """Connect newly-contributed MCP servers in the background.
    Rebuilds the main team once any server actually connected so
    the freshly-attached tools land on the next agent turn."""
    logger.info("Auto-connect: starting %d MCP server(s): %s", len(names), sorted(names))
    any_connected = False
    for name in sorted(names):
        try:
            t0 = asyncio.get_event_loop().time()
            client = await session.mcp_manager.connect(name)
            elapsed = asyncio.get_event_loop().time() - t0
            if client:
                tool_count = len(getattr(client, "functions", None) or {})
                logger.info(
                    "Auto-connect: '%s' connected in %.2fs (%d tool(s))",
                    name,
                    elapsed,
                    tool_count,
                )
                any_connected = True
            else:
                logger.info(
                    "Auto-connect: '%s' not connected after %.2fs "
                    "(user denied, policy block, empty tools, or transport error)",
                    name,
                    elapsed,
                )
        except Exception:
            logger.warning("Auto-connect of MCP server '%s' failed", name, exc_info=True)

    # Even one new client warrants a team rebuild — without this the
    # freshly-connected tools are visible in ``mcp_manager._clients``
    # but absent from the agent's tool surface.
    if any_connected:
        logger.info("Auto-connect: rebuilding main team to attach new MCP tools")
        session.rebuild_mcp()
        logger.info("Auto-connect: main team rebuilt")
