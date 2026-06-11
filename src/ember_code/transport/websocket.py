"""WebSocket transport — one JSON protocol message per text frame.

Serves browser-based frontends (the shared web UI used by the Tauri
app, the VSCode webview, and the JetBrains JCEF panel). Wire format
is the same JSON the Unix-socket transport uses, except WebSocket
frames already delimit messages so no newline framing is needed.

Multi-client session mirroring: the BE owns ONE session; every
connected client is a live view of it. ``send()`` broadcasts to all
attached clients; each client gets a ``Welcome`` with its
``client_id`` at attach so it can recognise its own echoes. Clients
self-identify on inbound messages that need attribution (typing,
message echoes) by stamping that id.

Other properties callers should know about:

* **Reconnect-friendly.** ``receive()`` does NOT terminate when a
  client drops — webviews reload at will. The BE only exits via
  ``Shutdown``, signals, or the parent watchdog.
* **Loopback only by default.** The BE executes arbitrary tool
  calls; it must never listen on a routable interface.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator

from ember_code.protocol.messages import Message, Welcome
from ember_code.transport.base import Transport
from ember_code.transport.unix_socket import deserialize_message

logger = logging.getLogger(__name__)

# Mirror the Unix transport's frame cap — a single message can carry
# MCP tool catalogues or large tool results.
_MAX_FRAME_BYTES = 64 * 1024 * 1024


class WebSocketServerTransport(Transport):
    """BE-side transport: loopback WS server, N mirrored clients."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self._host = host
        self._port = port
        self._server = None
        # client_id → live connection. Insertion order preserved so
        # broadcast order is stable (oldest first).
        self._conns: dict[str, object] = {}
        self._closed = False
        self._connected = asyncio.Event()
        # Incoming frames from every client land here; ``receive()``
        # drains it. ``None`` is the close sentinel — enqueued only by
        # ``close()``, never on client disconnect.
        self._inbox: asyncio.Queue[Message | None] = asyncio.Queue()

    @property
    def port(self) -> int:
        """The bound port — meaningful after ``start()`` (supports port=0)."""
        return self._port

    @property
    def client_count(self) -> int:
        return len(self._conns)

    async def start(self) -> None:
        from websockets.asyncio.server import serve

        self._server = await serve(
            self._handler,
            self._host,
            self._port,
            max_size=_MAX_FRAME_BYTES,
        )
        sockets = self._server.sockets or []
        if sockets:
            self._port = sockets[0].getsockname()[1]
        logger.info("BE listening on ws://%s:%d", self._host, self._port)

    async def wait_for_connection(self, timeout: float | None = 30.0) -> None:
        """Wait for the first client. ``timeout=None`` waits forever —
        GUI shells may open the webview long after spawning the BE."""
        await asyncio.wait_for(self._connected.wait(), timeout=timeout)

    async def _handler(self, conn) -> None:
        client_id = f"ws-{uuid.uuid4().hex[:8]}"
        self._conns[client_id] = conn
        self._connected.set()
        logger.info("FE client %s attached (%d total)", client_id, len(self._conns))
        try:
            # Tell the client who it is so it can recognise its own
            # broadcast echoes.
            await conn.send(Welcome(client_id=client_id).model_dump_json())
            async for raw in conn:
                if isinstance(raw, bytes):
                    raw = raw.decode()
                msg = deserialize_message(raw)
                if msg is not None:
                    await self._inbox.put(msg)
        except Exception as exc:
            logger.info("WS client %s ended: %s", client_id, exc)
        finally:
            self._conns.pop(client_id, None)
            logger.info("FE client %s detached (%d left)", client_id, len(self._conns))

    async def send(self, message: Message) -> None:
        """Broadcast to every attached client; drop dead connections."""
        if self._closed or not self._conns:
            # No views attached (e.g. all webviews mid-reload). Events
            # are fire-and-forget; RPC callers re-issue after reconnect.
            return
        payload = message.model_dump_json()
        dead: list[str] = []
        for client_id, conn in list(self._conns.items()):
            try:
                await conn.send(payload)  # type: ignore[attr-defined]
            except Exception as exc:
                logger.debug("WS send to %s failed (client gone?): %s", client_id, exc)
                dead.append(client_id)
        for client_id in dead:
            self._conns.pop(client_id, None)

    async def receive(self) -> AsyncIterator[Message]:
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
        for conn in list(self._conns.values()):
            with contextlib.suppress(Exception):
                await conn.close()  # type: ignore[attr-defined]
        self._conns.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def is_closed(self) -> bool:
        return self._closed


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

    async def start(self) -> None:
        for t in self._transports:
            start = getattr(t, "start", None)
            if start is not None:
                await start()

    async def _pump(self, transport: Transport) -> None:
        try:
            async for msg in transport.receive():
                await self._inbox.put(msg)
        except Exception as exc:  # pragma: no cover — defensive
            logger.info("composite child receive ended: %s", exc)

    async def wait_for_connection(self, timeout: float | None = 30.0) -> None:
        """Resolve when ANY child gets its first connection."""
        waiters = [
            asyncio.ensure_future(t.wait_for_connection(timeout=None))  # type: ignore[attr-defined]
            for t in self._transports
            if hasattr(t, "wait_for_connection")
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
