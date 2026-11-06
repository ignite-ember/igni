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

import asyncio
import contextlib
import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ember_code.core.mcp.naming import tool_name_prefix
from ember_code.core.mcp.notification_tap import McpNotificationRouter
from ember_code.core.mcp.tool_errors import raise_on_failure

try:
    from agno.tools.mcp import MCPTools
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    from ember_code.core.mcp.request_meta import RequestMeta, RequestMetaSession
except ImportError:  # pragma: no cover — exercised by test_connect_import_error
    MCPTools = None  # type: ignore[assignment,misc]
    ClientSession = None  # type: ignore[assignment,misc]
    RequestMetaSession = None  # type: ignore[assignment,misc]
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
        #: Where in :attr:`errlog_path` each server's latest process
        #: began writing, so :meth:`stderr_tail` can say what *that*
        #: process said rather than whatever the shared file ends with.
        self._offsets: dict[str, int] = {}

    @property
    def errlog_path(self) -> Path:
        """Filesystem path the subprocess stderr appends to."""
        return self._errlog_path

    def stderr_tail(self, name: str, *, lines: int = 5, max_chars: int = 600) -> str:
        """The last few lines ``name``'s latest process wrote to stderr.

        Empty when it wrote nothing or was never opened here. A process
        that exits at start-up says why on stderr, and that line is the
        one thing a person needs; without this it went to a temp file
        nobody was told about and the reason they saw was a guess.

        The log is shared by every server, so lines another server wrote
        meanwhile can appear; at start-up, which is when this is asked,
        that is rare and harmless.
        """
        start = self._offsets.get(name)
        if start is None:
            return ""
        try:
            with open(os.fspath(self._errlog_path), "rb") as log:
                log.seek(start)
                text = log.read().decode("utf-8", errors="replace")
        except OSError:
            return ""
        tail = [line.rstrip() for line in text.splitlines() if line.strip()][-lines:]
        joined = " | ".join(tail)
        return joined[-max_chars:]

    @classmethod
    def default_errlog_path(cls) -> Path:
        """Default errlog path — the shared system temp file the
        pre-refactor module-level constant pointed at. Kept as
        a classmethod (not a bare module constant) so the
        default has a named home and tests can override the
        instance without touching module globals."""
        return Path(tempfile.gettempdir()) / "ember_mcp_stderr.log"

    async def open(
        self,
        name: str,
        config: MCPServerConfig,
        *,
        notifications: McpNotificationRouter | None = None,
        request_meta: RequestMeta | None = None,
    ) -> MCPTools:
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

        ``notifications`` takes the methods it routes out of the
        read stream before the SDK sees them — see
        :mod:`ember_code.core.mcp.notification_tap`.

        ``request_meta``, when given, is asked on every tool call and
        its answer travels as the request's ``_meta``
        (:mod:`ember_code.core.mcp.request_meta`).
        """
        # Must stay open for the MCP session's lifetime; the connection
        # below owns it and closes it when the connection ends.
        errlog = open(os.fspath(self._errlog_path), "a")  # noqa: SIM115
        errlog.seek(0, os.SEEK_END)
        self._offsets[name] = errlog.tell()
        params = StdioServerParameters(
            command=config.command,
            args=config.args or [],
            env=config.env if config.env else None,
            cwd=config.cwd or None,
        )

        mcp_tools = MCPTools(
            server_params=params,
            transport="stdio",
            tool_name_prefix=tool_name_prefix(name),
        )
        # Seconds, as a float: mcp 2 takes no ``timedelta``.
        read_timeout = float(getattr(mcp_tools, "timeout_seconds", 30) or 30)

        def session_for(read: Any, write: Any) -> Any:
            if notifications is not None:
                read = notifications.tap(name, read)
            if RequestMetaSession is not None:
                return RequestMetaSession(
                    read,
                    write,
                    read_timeout_seconds=read_timeout,
                    request_meta=request_meta,
                    busy_holder=f"mcp:{name}",
                )
            return ClientSession(read, write, read_timeout_seconds=read_timeout)

        # The connection lives in a task of its own, so whichever task
        # disconnects it, it is closed where it was opened
        # (:class:`OwnedStdioConnection`). Agno's ``close`` / ``__aexit__``
        # exit ``_context``, which is that connection.
        connection = OwnedStdioConnection(params, errlog=errlog, session_for=session_for, name=name)
        mcp_tools.session = await connection.open()
        mcp_tools._context = connection  # type: ignore[assignment]
        mcp_tools._active_contexts = [connection]

        # Initialize Agno tool functions from MCP session.
        # tool_name_prefix ensures MCP tools don't collide with
        # built-in tools (e.g. read_file → mcp_filesystem_read_file).
        try:
            await mcp_tools.initialize()
        except BaseException:
            await connection.aclose()
            raise
        # A tool that fails is a failed call, in its own words
        # (``tool_errors``); the session above records the failure.
        raise_on_failure(mcp_tools)

        return mcp_tools


class OwnedStdioConnection:
    """One stdio MCP connection, opened and closed by a task of its own.

    ``mcp`` 2's ``stdio_client`` cannot be abandoned. Cancelled, its
    stdout reader drains the server's stdout under a *shielded* scope
    until the pipe closes — and nothing closes it but the client's own
    exit, which stops the server. So a connection dropped without that
    exit (igni used to drop them: in ``mcp`` 1 exiting one from another
    task raised "cancel scope in a different task") keeps a task that no
    cancellation ends, and the event loop never finishes closing: the
    process hangs on its way out.

    Here a dedicated task enters ``stdio_client`` and the session and
    holds them until :meth:`aclose`, then exits both itself. Any task may
    call :meth:`aclose`. Shaped like an async context manager, because
    Agno's ``MCPTools.close`` / ``__aexit__`` exit ``_context``.
    """

    #: How long a closing connection may take before its task is cancelled.
    CLOSE_TIMEOUT = 10.0

    def __init__(
        self,
        params: Any,
        *,
        errlog: Any,
        session_for: Callable[[Any, Any], Any],
        name: str,
    ) -> None:
        self._params = params
        self._errlog = errlog
        self._session_for = session_for
        self._name = name
        self._closing = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def open(self) -> Any:
        """Start the connection; the initialised-transport session once it is up."""
        ready: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._own(ready), name=f"mcp-stdio:{self._name}")
        try:
            return await ready
        except BaseException:
            await self.aclose()
            raise

    async def _own(self, ready: asyncio.Future[Any]) -> None:
        try:
            async with contextlib.AsyncExitStack() as stack:
                read, write = await stack.enter_async_context(
                    stdio_client(self._params, errlog=self._errlog)
                )
                session = await stack.enter_async_context(self._session_for(read, write))
                if not ready.done():
                    ready.set_result(session)
                await self._closing.wait()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(
                    exc if isinstance(exc, Exception) else ConnectionError(str(exc))
                )
            elif not isinstance(exc, asyncio.CancelledError):
                logger.debug("MCP '%s' connection ended: %s", self._name, exc)
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            with contextlib.suppress(Exception):
                self._errlog.close()

    async def aclose(self) -> None:
        """Close the session and stop the server, from whichever task asks."""
        self._closing.set()
        task = self._task
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), self.CLOSE_TIMEOUT)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
        except Exception as exc:  # noqa: BLE001 — closing reports, never raises
            logger.debug("MCP '%s' close: %s", self._name, exc)

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()
