"""MCP panel event handlers for :class:`EmberApp`.

Extracted from ``tui/app.py``. Same pattern as
``codeindex_handlers.py`` and ``loop_handlers.py``.

Free functions taking ``app: EmberApp`` as first arg:

* :func:`show_mcp_panel` — build server list, mount panel.
* :func:`build_mcp_server_list` — RPC call + wire-model map.
  Kept as its own function because both the mount path and
  the post-toggle refresh need it.
* :func:`toggle_mcp` — connect / disconnect a server. Fires
  a conversation status line for both success and failure so
  the user has a durable record.
* :func:`on_mcp_panel_closed` — restore focus.

The ``@on(MCPPanelWidget.ServerToggleRequested)`` decorator
stays on the app method; it schedules `toggle_mcp` as an
`asyncio.create_task` so the toggle runs in the background
without blocking the TUI.

Rule 2 clean — all imports at module top.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ember_code.frontend.tui.widgets import (
    MCPPanelWidget,
    MCPServerInfo,
    PromptInput,
)
from ember_code.protocol.rpc import RpcMethod

if TYPE_CHECKING:
    from ember_code.frontend.tui.app import EmberApp

logger = logging.getLogger(__name__)


async def build_mcp_server_list(app: "EmberApp") -> list[MCPServerInfo]:
    """Fetch details for every registered MCP server + convert to
    the panel's wire model."""
    details = (
        await app._backend._rpc(RpcMethod.GET_MCP_SERVER_DETAILS)
        if hasattr(app._backend, "_rpc")
        else await app._backend.get_mcp_server_details()
    )
    # ``MCPServerInfo`` fields mirror the wire dict keys 1:1, so a
    # spread parse works without hand-listing each field — the
    # explicit assignment was pre-Pydantic ceremony that Rule 1
    # made obsolete.
    return [MCPServerInfo(**info) for info in details or [] if isinstance(info, dict)]


async def show_mcp_panel(app: "EmberApp") -> None:
    """Gather MCP server info and mount the panel."""
    servers = await build_mcp_server_list(app)
    panel = MCPPanelWidget(servers=servers)
    app.mount(panel)
    panel.focus()


async def toggle_mcp(app: "EmberApp", name: str, enable: bool) -> None:
    """Toggle MCP server in background — doesn't block the TUI.

    Emits a conversation status line for both connect + errors
    so the user has a durable log of the operation. On success
    (either direction), refreshes the status-bar IDE badges +
    the panel's server rows.
    """
    if enable:
        app._conversation.append_info(f"MCP '{name}': connecting...")
        try:
            result = await app._backend.mcp_connect(name)
            app._conversation.append_info(
                result.text if hasattr(result, "text") else str(result)
            )
        except Exception as exc:
            app._conversation.append_info(f"MCP '{name}': failed: {exc}")
    else:
        try:
            result = await app._backend.mcp_disconnect(name)
            app._conversation.append_info(
                result.text if hasattr(result, "text") else str(result)
            )
        except Exception as exc:
            logger.debug("MCP disconnect error: %s", exc)
    # Refresh status and panel.
    try:
        statuses = (
            await app._backend._rpc(RpcMethod.GET_MCP_STATUS)
            if hasattr(app._backend, "_rpc")
            else app._backend.get_mcp_status()
        )
        for sname, connected in statuses or []:
            app._status.set_ide_status(sname, connected)
        panel = app.query_one(MCPPanelWidget)
        panel.refresh_servers(await build_mcp_server_list(app))
    except Exception:
        pass


def on_mcp_panel_closed(app: "EmberApp") -> None:
    """Restore focus to the prompt input."""
    app.query_one("#user-input", PromptInput).focus()
