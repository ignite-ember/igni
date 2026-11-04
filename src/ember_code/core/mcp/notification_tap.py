"""Notifications a server sends that the MCP SDK has no type for.

The SDK validates every incoming notification against its own closed
union of ``notifications/*`` methods, and drops anything else with a
warning. That is right for a general client and wrong for igni, which
gives the servers it starts a vocabulary of their own — an extension
tells the host "your export is ready" with ``notifications/igni/notify``
(igni server's ``docs/service-identity.md`` §10.2), and the SDK would
throw it away before any code here saw it.

So the read side of a stdio connection is tapped: a message whose
method somebody registered for is taken out of the stream and handed to
that handler, together with the name of the server it arrived on.
Everything else passes through untouched, so the SDK's own handling of
progress, cancellation and the rest is unchanged.

**The connection names the sender.** The handler is told which server
the notification came in on, and nothing in the notification itself
can change that — which is what lets the host accept a notification
"only from that extension".
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import anyio.abc

try:
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCNotification
except ImportError:  # pragma: no cover — the ``mcp`` extra is optional
    SessionMessage = None  # type: ignore[assignment,misc]
    JSONRPCNotification = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

#: ``handler(server_name, params)``. Runs on the connection's reader,
#: so it must return promptly; anything slow belongs in a task.
NotificationHandler = Callable[[str, dict[str, Any]], None]


class McpNotificationRouter:
    """Which handler takes which non-standard notification method."""

    def __init__(self) -> None:
        self._handlers: dict[str, NotificationHandler] = {}

    def handle(self, method: str, handler: NotificationHandler) -> None:
        """Take ``method`` out of every tapped stream and give it to ``handler``."""
        self._handlers[method] = handler

    def handles(self, method: str) -> bool:
        return method in self._handlers

    def deliver(self, server: str, method: str, params: dict[str, Any]) -> None:
        """Hand one notification to its handler. A handler that raises is
        logged: one bad notification must not end the connection."""
        handler = self._handlers.get(method)
        if handler is None:
            return
        try:
            handler(server, params)
        except Exception:  # noqa: BLE001 — the connection outlives a bad handler
            logger.exception("handling %s from %s failed", method, server)

    def tap(self, server: str, stream: Any) -> NotificationTapStream:
        """``stream``, with this router's methods taken out of it."""
        return NotificationTapStream(stream, server=server, router=self)


class NotificationTapStream(anyio.abc.ObjectReceiveStream[Any]):
    """A receive stream that diverts registered notification methods."""

    def __init__(self, inner: Any, *, server: str, router: McpNotificationRouter) -> None:
        self._inner = inner
        self._server = server
        self._router = router

    async def receive(self) -> Any:
        while True:
            message = await self._inner.receive()
            notification = self._registered(message)
            if notification is None:
                return message
            params = notification.params if isinstance(notification.params, dict) else {}
            self._router.deliver(self._server, notification.method, params)

    async def aclose(self) -> None:
        await self._inner.aclose()

    def _registered(self, message: Any) -> Any:
        """The notification inside ``message``, when it is one this router takes."""
        if SessionMessage is None or not isinstance(message, SessionMessage):
            return None
        root = message.message.root
        if not isinstance(root, JSONRPCNotification):
            return None
        return root if self._router.handles(root.method) else None
