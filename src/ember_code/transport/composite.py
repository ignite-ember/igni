"""Fan one BE across several transports (e.g. Unix socket for the TUI +
WebSocket for GUI tabs) so every attached view mirrors the same session.

Lives in its own module — not co-located with :mod:`ember_code.transport.websocket`
because the composite fan-out is transport-agnostic, not WS-specific.
The old ``from ember_code.transport.websocket import CompositeTransport``
import path is preserved via a re-export in that module so callers do
not need to change.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from ember_code.protocol.messages import Message
from ember_code.transport.base import ListeningTransport, Transport

logger = logging.getLogger(__name__)


class CompositeTransport(Transport):
    """Fans one BE across several transports (e.g. unix for the TUI +
    WS for GUI tabs) so every attached view mirrors the same session.

    ``send()`` broadcasts to every child; ``receive()`` merges every
    child's inbound stream. A child closing does NOT end the merged
    stream — only ``close()`` does.
    """

    def __init__(self, transports: list[Transport]):
        self._transports = transports
        self._closed = False
        self._inbox: asyncio.Queue[Message | None] = asyncio.Queue()
        self._pumps: list[asyncio.Task] = []

    def _listening_children(self) -> list[ListeningTransport]:
        """Subset of children that own a server socket lifecycle.

        Client-only transports (``NDJsonStreamClientTransport``,
        ``InProcessTransport``) stay on plain :class:`Transport` and are
        skipped for ``start`` / ``wait_for_connection`` dispatch — they
        have nothing to bind and nothing to wait on.
        """
        return [t for t in self._transports if isinstance(t, ListeningTransport)]

    async def start(self) -> None:
        for t in self._listening_children():
            await t.start()

    async def _pump(self, transport: Transport) -> None:
        try:
            async for msg in transport.receive():
                await self._inbox.put(msg)
        except Exception as exc:  # pragma: no cover — defensive
            logger.info("composite child receive ended: %s", exc)

    async def wait_for_connection(self, timeout: float | None = 30.0) -> None:
        """Resolve when ANY child gets its first connection."""
        waiters = [
            asyncio.ensure_future(t.wait_for_connection(timeout=None))
            for t in self._listening_children()
        ]
        if not waiters:
            return
        try:
            done, pending = await asyncio.wait(
                waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            for p in pending:
                p.cancel()
            if not done:
                raise asyncio.TimeoutError
        finally:
            # Start the merge pumps once something is listening.
            if not self._pumps:
                self._pumps = [asyncio.create_task(self._pump(t)) for t in self._transports]

    async def send(self, message: Message) -> None:
        for t in self._transports:
            with contextlib.suppress(Exception):
                await t.send(message)

    async def receive(self) -> AsyncIterator[Message]:
        # Pumps normally start in wait_for_connection; make sure they
        # exist even if a caller skips straight to receive().
        if not self._pumps:
            self._pumps = [asyncio.create_task(self._pump(t)) for t in self._transports]
        while not self._closed:
            msg = await self._inbox.get()
            if msg is None:
                break
            yield msg

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._inbox.put(None)
        for p in self._pumps:
            p.cancel()
        for p in self._pumps:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await p
        for t in self._transports:
            with contextlib.suppress(Exception):
                await t.close()

    @property
    def is_closed(self) -> bool:
        return self._closed
