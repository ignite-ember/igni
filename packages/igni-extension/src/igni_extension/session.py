"""The MCP session to the igni host, held so the process can speak first.

An MCP server answers requests; a notification it sends on its own — from
a background task, when an export finishes — needs the session, which the
`mcp` package only hands to request handlers. So the lowlevel server's
message dispatch is wrapped: the first message the host sends (its
`initialized` notification, right after the handshake) attaches the
session here, and anything sent before that is held and flushed then.

This reads one private method of `mcp` 1.x's lowlevel `Server`
(`_handle_message`), which is why the dependency is pinned below 2.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.lowlevel import Server
from mcp.server.session import ServerSession

from igni_extension.notifications import NotifyNotification

logger = logging.getLogger(__name__)

Dispatch = Callable[..., Awaitable[None]]


class HostSession:
    """The session to the host, once there is one, and what waits for it."""

    #: Notifications kept while nobody is connected; the oldest go first.
    MAX_PENDING = 100

    def __init__(self, server: Server[Any, Any]) -> None:
        self._session: ServerSession | None = None
        self._pending: deque[NotifyNotification] = deque()
        self._lock = asyncio.Lock()
        self._wrap(server)

    @property
    def connected(self) -> bool:
        return self._session is not None

    def _wrap(self, server: Server[Any, Any]) -> None:
        dispatch: Dispatch = server._handle_message

        async def attach_then_dispatch(
            message: Any, session: ServerSession, *args: Any, **kwargs: Any
        ) -> None:
            if session is not self._session:
                await self.attach(session)
            await dispatch(message, session, *args, **kwargs)

        server._handle_message = attach_then_dispatch  # type: ignore[method-assign]

    async def attach(self, session: ServerSession) -> None:
        async with self._lock:
            self._session = session
            while self._pending:
                await session.send_notification(self._pending.popleft())  # type: ignore[arg-type]

    async def send(self, notification: NotifyNotification) -> None:
        async with self._lock:
            if self._session is None:
                if len(self._pending) >= self.MAX_PENDING:
                    dropped = self._pending.popleft()
                    logger.warning(
                        "igni: dropped notification %r; the host has not connected",
                        dropped.params.event_key or dropped.params.title,
                    )
                self._pending.append(notification)
                return
            await self._session.send_notification(notification)  # type: ignore[arg-type]
