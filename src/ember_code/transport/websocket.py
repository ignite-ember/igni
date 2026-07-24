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
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

from websockets.asyncio.server import serve

from ember_code.protocol.messages import Message, Welcome
from ember_code.protocol.registry import MessageRegistry
from ember_code.transport._ws_client import MirroredClient, SendResult
from ember_code.transport.base import ListeningTransport

# Re-exported so the historical ``from ember_code.transport.websocket
# import CompositeTransport`` import path used by tests and callers
# keeps working after CompositeTransport moved to its own module.
from ember_code.transport.composite import CompositeTransport

if TYPE_CHECKING:
    from websockets.asyncio.server import Server, ServerConnection

__all__ = ["CompositeTransport", "WebSocketServerTransport"]

logger = logging.getLogger(__name__)

# Mirror the Unix transport's frame cap — a single message can carry
# MCP tool catalogues or large tool results.
_MAX_FRAME_BYTES = 64 * 1024 * 1024

# Per-client send timeout during broadcast. A slow or wedged client
# must not block every other attached view: after this deadline the
# offending conn is dropped from ``_conns`` and broadcast continues
# without it. 2s is plenty for any healthy localhost websocket; a
# client that misses it has either crashed mid-frame or backed up its
# OS buffer enough that recovery is unlikely.
_BROADCAST_SEND_TIMEOUT = 2.0


# ── Dedicated chunk trace handler ─────────────────────────────────
# The BE's normal ``--debug`` path configures root logging in
# :mod:`ember_code.backend.__main__`, but several downstream imports
# (httpx, litellm, …) reset root handlers between startup and the
# first :meth:`send` call, which silently drops our
# ``logger.debug`` calls. To make the chunk trace reliable we attach
# a *dedicated* file handler to this module's logger the first time
# it's imported, gated on ``EMBER_CHUNK_TRACE=1`` so non-debug runs
# don't pay the cost. The handler writes to
# ``~/.ember/chunk_trace.log`` (overridable via EMBER_CHUNK_TRACE_LOG)
# and bypasses root propagation entirely — the file is the only sink.
if os.environ.get("EMBER_CHUNK_TRACE") == "1" and not any(
    getattr(h, "_ember_chunk_trace", False) for h in logger.handlers
):
    _trace_path = Path(
        os.environ.get("EMBER_CHUNK_TRACE_LOG") or (Path.home() / ".ember" / "chunk_trace.log")
    )
    _trace_path.parent.mkdir(parents=True, exist_ok=True)

    class _FlushingFileHandler(logging.FileHandler):
        """Line-buffer after each emit so a ``tail -f`` shows chunks
        the moment they leave the BE. Default FileHandler is
        block-buffered on disk files, which hid 43 chunks behind
        ~4 KB of buffering on macOS until enough traffic arrived
        to fill the buffer."""

        def emit(self, record):  # noqa: D401 - stdlib override
            super().emit(record)
            self.flush()

    _trace_handler = _FlushingFileHandler(str(_trace_path), mode="a")
    _trace_handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    _trace_handler._ember_chunk_trace = True  # type: ignore[attr-defined]
    logger.addHandler(_trace_handler)
    logger.setLevel(logging.DEBUG)
    logger.debug("chunk_trace handler attached at %s", _trace_path)


class WebSocketServerTransport(ListeningTransport):
    """BE-side transport: loopback WS server, N mirrored clients."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        broadcast_send_timeout: float = _BROADCAST_SEND_TIMEOUT,
        registry: MessageRegistry | None = None,
    ):
        self._host = host
        self._port = port
        self._broadcast_send_timeout = broadcast_send_timeout
        self._registry = registry or MessageRegistry.default()
        # Set by ``start()`` to the awaited ``serve(...)`` result.
        self._server: Server | None = None
        # client_id → live client wrapper. Insertion order preserved so
        # broadcast order is stable (oldest first).
        self._conns: dict[str, MirroredClient] = {}
        # Monotonic per-stream event sequence. Increments on every
        # ``send()``; resets to 0 on ``stream_end`` so the next turn
        # starts fresh from 1. See :meth:`send` for the FE dedup
        # contract.
        self._event_seq: int = 0
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
        self._server = await serve(
            self._handler,
            self._host,
            self._port,
            max_size=_MAX_FRAME_BYTES,
        )
        # ``Server.sockets`` may be empty on platforms that don't expose
        # underlying sockets; the ``or []`` fallback keeps us typed.
        sockets = list(self._server.sockets or [])
        if sockets:
            self._port = sockets[0].getsockname()[1]
        logger.info("BE listening on ws://%s:%d", self._host, self._port)

    async def wait_for_connection(self, timeout: float | None = 30.0) -> None:
        """Wait for the first client. ``timeout=None`` waits forever —
        GUI shells may open the webview long after spawning the BE."""
        await asyncio.wait_for(self._connected.wait(), timeout=timeout)

    async def _handler(self, conn: ServerConnection) -> None:
        client_id = f"ws-{uuid.uuid4().hex[:8]}"
        client = MirroredClient(client_id=client_id, conn=conn)
        self._conns[client_id] = client
        self._connected.set()
        logger.info("FE client %s attached (%d total)", client_id, len(self._conns))
        try:
            # Tell the client who it is so it can recognise its own
            # broadcast echoes.
            await conn.send(Welcome(client_id=client_id).model_dump_json())
            async for raw in conn:
                if isinstance(raw, bytes):
                    raw = raw.decode()
                msg = self._registry.deserialize(raw)
                if msg is not None:
                    await self._inbox.put(msg)
        except Exception as exc:
            logger.info("WS client %s ended: %s", client_id, exc)
        finally:
            self._conns.pop(client_id, None)
            logger.info("FE client %s detached (%d left)", client_id, len(self._conns))

    async def send(self, message: Message) -> None:
        """Broadcast to every attached client concurrently; drop dead /
        stalled connections.

        Concurrency matters here: with multiple sessions live on one
        BE, a single wedged client (e.g. a webview the OS suspended
        mid-tab) used to stall *every* session's emit loop while we
        awaited its ``conn.send`` in series. Each conn now gets its
        own timeout via :meth:`MirroredClient.send` so a slow client
        only delays itself, and is evicted from ``_conns`` after the
        per-conn timeout.
        """
        if self._closed or not self._conns:
            # No views attached (e.g. all webviews mid-reload). Events
            # are fire-and-forget; RPC callers re-issue after reconnect.
            return
        # Stamp a per-stream monotonic ``event_seq`` so the FE can
        # dedup events that arrive twice (e.g. when two WebSocket
        # clients are attached due to a StrictMode double-mount of
        # EmberClient) AND so the ordering is preserved across
        # duplicates — the FE keys dedup on (id, event_seq) and the
        # sequence itself is the canonical order of events in the
        # stream. ``stream_end`` resets the counter so a new turn
        # starts fresh from 1.
        if not message.event_seq:
            self._event_seq += 1
            message = message.model_copy(update={"event_seq": self._event_seq})
        if message.type == "stream_end":
            self._event_seq = 0
        # DEBUG-only chunk trace — enabled when an operator raises the
        # log level to DEBUG to verify the BE is actually streaming
        # every chunk (vs coalescing or dropping under back-pressure).
        if message.type == "content_delta":
            text = getattr(message, "text", "") or ""
            # Bypass ``logger.debug``: a downstream library (suspect
            # litellm / agno) calls ``logging.disable(DEBUG)``
            # globally after startup, which leaves ``isEnabledFor``
            # returning False even though our level is DEBUG. Emit
            # the record directly to the chunk-trace handler instead
            # so chunks actually land in the trace log.
            chunk_msg = "[chunk tx] seq=%d len=%d thinking=%s preview=%r" % (
                message.event_seq,
                len(text),
                getattr(message, "is_thinking", False),
                text[:40],
            )
            for h in logger.handlers:
                if getattr(h, "_ember_chunk_trace", False):
                    record = logging.LogRecord(
                        name=logger.name,
                        level=logging.DEBUG,
                        pathname=__file__,
                        lineno=0,
                        msg=chunk_msg,
                        args=(),
                        exc_info=None,
                    )
                    h.emit(record)
                    h.flush()
        payload = message.model_dump_json()

        results: list[SendResult] = await asyncio.gather(
            *(
                client.send(payload, timeout=self._broadcast_send_timeout)
                for client in list(self._conns.values())
            )
        )
        for result in results:
            if result.ok:
                continue
            # Log at the level operators expect. Timeouts are worth a
            # WARNING (the client backed up its OS buffer or wedged);
            # peer-gone errors are DEBUG (the client just detached and
            # the driver noticed on our send).
            if result.reason == "timeout":
                logger.warning(
                    "WS send to %s timed out after %.1fs — dropping client",
                    result.client_id,
                    self._broadcast_send_timeout,
                )
            else:
                logger.debug("WS send to %s failed (client gone?)", result.client_id)
            self._conns.pop(result.client_id, None)

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
        for client in list(self._conns.values()):
            with contextlib.suppress(Exception):
                await client.close()
        self._conns.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def is_closed(self) -> bool:
        return self._closed
