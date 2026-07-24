"""Abstract transport interface for BE↔FE communication."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from ember_code.protocol.messages import Message


class Transport(ABC):
    """Bidirectional message transport between backend and frontend.

    Implementations must handle serialization/deserialization of
    protocol messages to/from the underlying medium.
    """

    @abstractmethod
    async def send(self, message: Message) -> None:
        """Send a protocol message to the other side."""

    @abstractmethod
    def receive(self) -> AsyncIterator[Message]:
        """Receive protocol messages from the other side.

        Returns an async iterator that yields messages as they arrive.
        Raises StopAsyncIteration when the connection is closed.
        """

    @abstractmethod
    async def close(self) -> None:
        """Close the transport gracefully."""

    @property
    @abstractmethod
    def is_closed(self) -> bool:
        """Whether the transport has been closed."""


class ListeningTransport(Transport):
    """A :class:`Transport` that owns a server socket lifecycle.

    Server-side transports (Unix socket, WebSocket) need two extra
    lifecycle hooks beyond the base ``send`` / ``receive`` / ``close``
    contract: ``start()`` binds the listening socket, and
    ``wait_for_connection()`` blocks until the first client attaches.

    Making this an explicit ABC lets :class:`~ember_code.transport.composite.CompositeTransport`
    dispatch to child listening transports via ``isinstance`` instead
    of duck-typing them with ``getattr`` / ``hasattr`` — pure
    client-side transports (``NDJsonStreamClientTransport`` under
    ``UnixSocketClientTransport``, ``InProcessTransport``) stay on
    plain :class:`Transport` and are not asked to grow spurious
    ``start`` / ``wait_for_connection`` methods.
    """

    @abstractmethod
    async def start(self) -> None:
        """Bind the listening socket; must be awaited before ``send`` / ``receive``."""

    @abstractmethod
    async def wait_for_connection(self, timeout: float | None = 30.0) -> None:
        """Block until the first client attaches (or ``timeout`` expires)."""
