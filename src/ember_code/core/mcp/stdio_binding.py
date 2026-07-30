"""Stdio transport adapter for Agno's :class:`MCPTools`.

This module encapsulates the private-attribute reach-in that
Ember needs to perform on Agno's :class:`MCPTools` to redirect
the subprocess's stderr away from ``sys.stderr`` (which Textual
uses for TUI rendering — a subprocess writing to the same
stream corrupts the on-screen frame). Every ``mcp_tools._*``
assignment lives inside :meth:`StdioMCPBinding.open` and
nowhere else, matching Rule 6 (adapter reach-in encapsulated
behind one named seam).

Why the reach-in exists
-----------------------
Agno's :meth:`MCPTools.__aenter__` opens the stdio subprocess
with ``errlog=sys.stderr``. When Ember runs inside a Textual
TUI, any bytes written to ``sys.stderr`` interleave with
Textual's own rendering commands and corrupt the visible
frame. To fix this we skip Agno's ``__aenter__`` entirely and
drive the MCP SDK's :func:`stdio_client` ourselves with an
explicit ``errlog=<file handle>`` — then splice the resulting
session into the :class:`MCPTools` handle so Agno's tool-
initialisation and cleanup paths still work unchanged.

The ``_errlog`` assignment on the returned :class:`MCPTools`
is load-bearing: Agno's cleanup closes it during
:meth:`__aexit__`, so we hand the handle over rather than
holding it ourselves. See the sibling :mod:`transport`
module for the generic subprocess pipe wrapper; this module
is the :class:`MCPTools`-specific adapter.
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

try:
    from agno.tools.mcp import MCPTools
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
except ImportError:  # pragma: no cover — exercised by test_connect_import_error
    MCPTools = None  # type: ignore[assignment,misc]
    ClientSession = None  # type: ignore[assignment,misc]
    StdioServerParameters = None  # type: ignore[assignment,misc]
    stdio_client = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from ember_code.core.mcp.config import MCPServerConfig

logger = logging.getLogger(__name__)


class StdioMCPBinding:
    """Encapsulates the :class:`MCPTools` private-attr reach-in.

    Instances hold the errlog :class:`~pathlib.Path` that
    every open call should append to. Construct with the
    default class-level helper :meth:`default_errlog_path`
    or inject a custom path in tests to point the log at a
    tmpdir instead of the shared system temp file.
    """

    def __init__(self, errlog_path: Path):
        self._errlog_path = errlog_path

    @property
    def errlog_path(self) -> Path:
        """Filesystem path the subprocess stderr appends to."""
        return self._errlog_path

    @classmethod
    def default_errlog_path(cls) -> Path:
        """Default errlog path — the shared system temp file the
        pre-refactor module-level constant pointed at. Kept as
        a classmethod (not a bare module constant) so the
        default has a named home and tests can override the
        instance without touching module globals."""
        return Path(tempfile.gettempdir()) / "ember_mcp_stderr.log"

    async def open(self, name: str, config: MCPServerConfig) -> MCPTools:
        """Open a stdio-backed :class:`MCPTools` handle with
        ``errlog`` redirected to :attr:`errlog_path`.

        Bypasses Agno's :meth:`MCPTools.__aenter__` and drives
        the MCP SDK's :func:`stdio_client` manually so we can
        pass ``errlog=<file handle>`` — the reason for this
        module's existence. Every ``mcp_tools._*`` private-attr
        write is contained in this single method.

        The returned :class:`MCPTools` owns the file handle
        (via ``mcp_tools._errlog``) — Agno's :meth:`__aexit__`
        closes it during shutdown.
        """
        # Must stay open for MCP session lifetime; ownership
        # transfers to MCPTools via ``_errlog`` below.
        errlog = open(os.fspath(self._errlog_path), "a")  # noqa: SIM115
        params = StdioServerParameters(
            command=config.command,
            args=config.args or [],
            env=config.env if config.env else None,
        )

        mcp_tools = MCPTools(
            server_params=params,
            transport="stdio",
            tool_name_prefix=f"mcp_{name}",
        )

        # Drive stdio_client with our errlog handle instead of
        # letting Agno default to sys.stderr.
        mcp_tools._context = stdio_client(params, errlog=errlog)  # type: ignore[assignment]
        session_params = await mcp_tools._context.__aenter__()
        mcp_tools._active_contexts = [mcp_tools._context]
        read, write = session_params[0:2]

        timeout = getattr(mcp_tools, "timeout_seconds", 30) or 30
        mcp_tools._session_context = ClientSession(  # type: ignore[assignment]
            read, write, read_timeout_seconds=timedelta(seconds=timeout)
        )
        mcp_tools.session = await mcp_tools._session_context.__aenter__()
        mcp_tools._active_contexts.append(mcp_tools._session_context)

        # Initialize Agno tool functions from MCP session.
        # tool_name_prefix ensures MCP tools don't collide with
        # built-in tools (e.g. read_file → mcp_filesystem_read_file).
        await mcp_tools.initialize()

        # Load-bearing: Agno's __aexit__ closes this handle.
        # If we drop the assignment the file descriptor leaks.
        mcp_tools._errlog = errlog

        return mcp_tools
