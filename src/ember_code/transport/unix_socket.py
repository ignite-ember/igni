"""Unix domain socket transport with NDJSON framing.

Server side: BE listens on a socket, accepts one FE connection.
Client side: FE connects to the socket.

Wire framing (newline-delimited JSON), send/receive/close, and the
Pydantic hydration pipeline all live on
:class:`~ember_code.transport.ndjson_stream.NDJsonStreamTransport`. This
module only owns the Unix-socket-specific bits: binding the on-disk
socket path, accepting one client, and connecting to a server socket.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ember_code.protocol.registry import MessageRegistry
from ember_code.transport.base import ListeningTransport
from ember_code.transport.ndjson_stream import STREAM_LIMIT, NDJsonStreamTransport

logger = logging.getLogger(__name__)


class UnixSocketServerTransport(NDJsonStreamTransport, ListeningTransport):
    """BE-side transport: listens on a Unix socket, accepts one connection.

    Multiple-inheritance is used deliberately: :class:`NDJsonStreamTransport`
    provides the wire framing + ``send`` / ``receive`` / ``close`` body,
    while :class:`ListeningTransport` declares the ``start`` /
    ``wait_for_connection`` server-lifecycle contract that
    :class:`~ember_code.transport.composite.CompositeTransport` dispatches on.
    Both bases ultimately inherit from :class:`Transport`, so the MRO
    resolves cleanly.
    """

    def __init__(
        self,
        socket_path: str | Path,
        registry: MessageRegistry | None = None,
    ):
        super().__init__(registry=registry)
        self._socket_path = Path(socket_path)
        self._server: asyncio.Server | None = None
        self._connected = asyncio.Event()

    async def start(self) -> None:
        """Start listening and wait for a client to connect."""
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self._socket_path.exists():
            self._socket_path.unlink()

        self._server = await asyncio.start_unix_server(
            self._on_connect, path=str(self._socket_path), limit=STREAM_LIMIT
        )
        logger.info("BE listening on %s", self._socket_path)

    async def wait_for_connection(self, timeout: float | None = 30.0) -> None:
        """Wait until a client connects."""
        await asyncio.wait_for(self._connected.wait(), timeout=timeout)

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._connected.set()
        logger.info("FE connected")

    async def _wait_for_reader(self) -> None:
        # Server side: block on the connection event so ``receive()``
        # started before the client attaches doesn't error out.
        await self._connected.wait()

    async def _close_extra(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._socket_path.exists():
            self._socket_path.unlink()


class UnixSocketClientTransport(NDJsonStreamTransport):
    """FE-side transport: connects to a BE's Unix socket."""

    def __init__(
        self,
        socket_path: str | Path,
        registry: MessageRegistry | None = None,
    ):
        super().__init__(registry=registry)
        self._socket_path = Path(socket_path)

    async def connect(self, timeout: float = 10.0) -> None:
        """Connect to the BE socket."""
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(self._socket_path), limit=STREAM_LIMIT),
            timeout=timeout,
        )
        logger.info("Connected to BE at %s", self._socket_path)
