"""Unix domain socket transport with NDJSON framing.

Server side: BE listens on a socket, accepts one FE connection.
Client side: FE connects to the socket.

Messages are newline-delimited JSON (one JSON object per line).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from pydantic import ValidationError

from ember_code.protocol.messages import Message
from ember_code.transport.base import Transport

logger = logging.getLogger(__name__)

# StreamReader buffer cap. asyncio's default is 64 KiB, which a single
# NDJSON message can blow past (e.g. MCP tool descriptions across several
# servers). Hitting the cap raises LimitOverrunError, killing the reader
# loop — after which every subsequent command hangs until its 60 s
# wait_for fires. 64 MiB is well clear of any realistic message.
_STREAM_LIMIT = 64 * 1024 * 1024

# Registry of message types for deserialization
_MESSAGE_TYPES: dict[str, type[Message]] = {}


def _build_registry() -> None:
    """Build a lookup table from type string to Message subclass."""
    if _MESSAGE_TYPES:
        return
    from ember_code.protocol import messages as msg_module

    for name in dir(msg_module):
        cls = getattr(msg_module, name)
        if (
            isinstance(cls, type)
            and issubclass(cls, Message)
            and cls is not Message
            and hasattr(cls, "model_fields")
        ):
            # Get the default value of the 'type' field
            type_field = cls.model_fields.get("type")
            if type_field and type_field.default:
                _MESSAGE_TYPES[type_field.default] = cls


def deserialize_message(line: str) -> Message | None:
    """Deserialize a JSON line into a protocol message."""
    _build_registry()
    try:
        data = json.loads(line)
        msg_type = data.get("type", "")
        cls = _MESSAGE_TYPES.get(msg_type)
        if cls is None:
            logger.warning("Unknown message type: %s", msg_type)
            return None
        return cls.model_validate(data)
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.warning("Failed to deserialize message: %s", exc)
        return None


class UnixSocketServerTransport(Transport):
    """BE-side transport: listens on a Unix socket, accepts one connection."""

    def __init__(self, socket_path: str | Path):
        self._socket_path = Path(socket_path)
        self._server: asyncio.Server | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._closed = False
        self._connected = asyncio.Event()

    async def start(self) -> None:
        """Start listening and wait for a client to connect."""
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self._socket_path.exists():
            self._socket_path.unlink()

        self._server = await asyncio.start_unix_server(
            self._on_connect, path=str(self._socket_path), limit=_STREAM_LIMIT
        )
        logger.info("BE listening on %s", self._socket_path)

    async def wait_for_connection(self, timeout: float = 30.0) -> None:
        """Wait until a client connects."""
        await asyncio.wait_for(self._connected.wait(), timeout=timeout)

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._connected.set()
        logger.info("FE connected")

    async def send(self, message: Message) -> None:
        if self._writer is None or self._closed:
            return
        line = message.model_dump_json() + "\n"
        self._writer.write(line.encode())
        await self._writer.drain()

    async def receive(self) -> AsyncIterator[Message]:
        if self._reader is None:
            await self._connected.wait()
        assert self._reader is not None

        while not self._closed:
            line_bytes = await self._reader.readline()
            if not line_bytes:
                break  # Connection closed
            line = line_bytes.decode().strip()
            if not line:
                continue
            msg = deserialize_message(line)
            if msg is not None:
                yield msg

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._socket_path.exists():
            self._socket_path.unlink()

    @property
    def is_closed(self) -> bool:
        return self._closed


class UnixSocketClientTransport(Transport):
    """FE-side transport: connects to a BE's Unix socket."""

    def __init__(self, socket_path: str | Path):
        self._socket_path = Path(socket_path)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._closed = False

    async def connect(self, timeout: float = 10.0) -> None:
        """Connect to the BE socket."""
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(self._socket_path), limit=_STREAM_LIMIT),
            timeout=timeout,
        )
        logger.info("Connected to BE at %s", self._socket_path)

    async def send(self, message: Message) -> None:
        if self._writer is None or self._closed:
            return
        line = message.model_dump_json() + "\n"
        self._writer.write(line.encode())
        await self._writer.drain()

    async def receive(self) -> AsyncIterator[Message]:
        if self._reader is None:
            raise RuntimeError("Not connected — call connect() first")

        while not self._closed:
            line_bytes = await self._reader.readline()
            if not line_bytes:
                break  # Connection closed
            line = line_bytes.decode().strip()
            if not line:
                continue
            msg = deserialize_message(line)
            if msg is not None:
                yield msg

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()

    @property
    def is_closed(self) -> bool:
        return self._closed
