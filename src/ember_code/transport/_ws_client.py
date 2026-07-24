"""Per-client wrapper + typed send result for the WebSocket transport.

Extracted so :mod:`ember_code.transport.websocket` no longer stores raw
``websockets`` connection objects as ``dict[str, object]`` — every entry
in ``_conns`` is now a :class:`MirroredClient` that owns the client id
alongside its live connection and exposes typed ``.send()`` / ``.close()``
methods.

The wrapper is deliberately internal (underscore-prefixed module) — it
is not part of the public transport API. If another listening transport
ever wants the same eviction pattern this can be promoted to a shared
helper without an API break.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from websockets.asyncio.server import ServerConnection

logger = logging.getLogger(__name__)


class SendResult(BaseModel):
    """Result of a single broadcast send to one mirrored client.

    Replaces the ``str | None`` sentinel the transport's ``_send_to``
    closure used to return: ``ok=True`` means the frame was delivered
    within the timeout budget, ``ok=False`` means the caller should
    evict this client. ``reason`` is present only on failure so callers
    can log at the right level (timeouts are WARNING; peer-gone errors
    are DEBUG — the client just detached).
    """

    client_id: str
    ok: bool
    reason: Literal["timeout", "conn_error"] | None = None


class MirroredClient:
    """One live browser view attached to the BE.

    Wraps the raw ``websockets`` server connection so the transport
    never needs to duck-type it. Every mirrored client keeps its
    ``client_id`` next to its ``conn`` so the transport's ``_conns``
    map is a homogeneous ``dict[str, MirroredClient]`` instead of a
    ``dict[str, object]`` that dispatches via ``getattr``.
    """

    def __init__(self, client_id: str, conn: ServerConnection) -> None:
        self._client_id = client_id
        # Public so tests that need to monkey-patch the raw conn
        # (see ``tests/test_websocket_transport.py``) can still reach
        # through as ``client.conn.send = ...``.
        self.conn = conn

    @property
    def id(self) -> str:
        return self._client_id

    async def send(self, payload: str, timeout: float) -> SendResult:
        """Send one frame; return a typed result the caller can act on.

        The wrapper deliberately does NOT log — the transport owns the
        operator-facing WARNING (timeout) and DEBUG (peer-gone)
        messages so log formatting stays in one place.
        """
        try:
            await asyncio.wait_for(self.conn.send(payload), timeout=timeout)
        except asyncio.TimeoutError:
            return SendResult(client_id=self._client_id, ok=False, reason="timeout")
        except Exception:
            return SendResult(client_id=self._client_id, ok=False, reason="conn_error")
        return SendResult(client_id=self._client_id, ok=True)

    async def close(self) -> None:
        """Close the underlying connection; swallows any driver errors —
        we're tearing down, the peer state no longer matters."""
        try:  # noqa: SIM105
            await self.conn.close()
        except Exception:
            # pragma: no cover — defensive; peer already gone
            pass
