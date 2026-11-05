"""The MCP session to the igni host, held so the process can speak first.

An MCP server answers requests; a notification it sends on its own — from
a background task, when an export finishes — needs the session, which the
`mcp` package only hands to request handlers. So the extension's server
runs `HostSession.middleware` on every message igni sends: the first one
(its `initialized` notification, right after the handshake) attaches the
session here, and anything sent before that is held and flushed then.

`mcp` 2 builds a `ServerSession` per message, but each one sends a
notification of the process's own on the connection's channel, so the
latest is as good as the first, and a new connection replaces the old.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.session import ServerSession

from igni_extension.notifications import ConditionsNotification, NotifyNotification

#: What the process says to the host on its own.
HostNotification = NotifyNotification | ConditionsNotification

logger = logging.getLogger(__name__)


class HostSession:
    """The session to the host, once there is one, and what waits for it."""

    #: Notifications kept while nobody is connected; the oldest go first.
    MAX_PENDING = 100

    def __init__(self) -> None:
        self._session: ServerSession | None = None
        self._pending: deque[HostNotification] = deque()
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._session is not None

    async def middleware(
        self, ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        """An MCP server middleware: every message igni sends attaches its session."""
        await self.attach(ctx.session)
        return await call_next(ctx)

    async def attach(self, session: ServerSession) -> None:
        self._session = session
        if not self._pending:
            return
        async with self._lock:
            while self._pending:
                await session.send_notification(self._pending.popleft())  # type: ignore[arg-type]

    async def send(self, notification: HostNotification) -> None:
        async with self._lock:
            if self._session is None:
                if len(self._pending) >= self.MAX_PENDING:
                    dropped = self._pending.popleft()
                    logger.warning("igni: dropped %s; the host has not connected", dropped.method)
                self._pending.append(notification)
                return
            await self._session.send_notification(notification)  # type: ignore[arg-type]
