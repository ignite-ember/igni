"""The first answer's path, run once in the warm base before ``ready``.

``docs/CLOUD_RESUME_TODO.md`` item 3b. After a restore igni's first
answer touches some 2,200 pages — the WebSocket server's handshake,
asyncio's, igni's own handler and message path — and on a restored VM a
page a producer's igni writes to for the first time becomes its own:
stored at its suspend, read back from its own state at its resume,
instead of from the base every producer shares. Python writes to what
it runs the first time it runs it (the interpreter's specialised
bytecode, a class's version tag, a string's hash, a reference count), so
until a warm base had answered once, each producer's first handshake did
those writes itself.

:class:`FirstAnswerPractice` makes the base answer once: a client on the
loopback, as the guest agent's ``forward`` connects for the gateway
(``igni__cloudvm/docs/guest-protocol.md``: vsock to the agent, TCP to
``127.0.0.1:8765``), offering no compression as the gateway does not; a
full handshake and igni's ``welcome``; one request of the kind a window
sends first, answered as every request is before a producer is bound
(:class:`~igni_cloud.guest.binding.UnboundRefusal`); then a clean close.
It needs no producer, token or database, and leaves nothing: no session
exists before binding, the transport forgets the connection
(:meth:`~ember_code.transport.websocket.WebSocketServerTransport.forget_past_connections`),
and what it allocated is collected before the base is frozen and made
immortal (:meth:`~igni_cloud.guest.contract.CloudGuest.bind`). What a
bound igni does past the transport — the dispatch to a session — cannot
be practised without a producer.

Bounded: a practice that fails or takes over
:attr:`FirstAnswerPractice.TIMEOUT_SECONDS` is logged, and ``ready`` is
written all the same.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any, ClassVar

from pydantic import BaseModel
from websockets.asyncio.client import connect

from ember_code.core.activity import background
from ember_code.protocol import messages as msg
from igni_cloud.guest.binding import UnboundRefusal

logger = logging.getLogger(__name__)


class PracticeResult(BaseModel):
    """How the practice went, logged as ``first answer practised``."""

    ok: bool
    #: Connect → ``welcome``.
    welcome_ms: float | None = None
    #: Connect → the answer to the request.
    answer_ms: float | None = None
    error: str = ""


class FirstAnswerPractice:
    """One connection, handshake and request on igni's own listener, then gone."""

    TIMEOUT_SECONDS: ClassVar[float] = 5.0
    HOST: ClassVar[str] = "127.0.0.1"
    #: A window's first request.
    METHOD: ClassVar[str] = "get_session_id"
    REQUEST_ID: ClassVar[str] = "warm-base-practice"

    def __init__(self, transport: Any) -> None:
        self._transport = transport

    @property
    def _server(self) -> Any:
        return getattr(self._transport, "inner", self._transport)

    async def run(self) -> PracticeResult:
        port = getattr(self._server, "port", None)
        if not port:
            return PracticeResult(ok=False, error="the transport listens on no port")
        # Before a producer is bound every request is answered this way.
        answering = background.daemon(
            UnboundRefusal(self._transport).run(), name="practice-answers"
        )
        try:
            return await asyncio.wait_for(self._exchange(port), self.TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 - a practice never stops igni
            return PracticeResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        finally:
            answering.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await answering
            forget = getattr(self._server, "forget_past_connections", None)
            if forget is not None:
                forget()

    async def _exchange(self, port: int) -> PracticeResult:
        started = time.perf_counter()
        async with connect(f"ws://{self.HOST}:{port}", compression=None) as ws:
            json.loads(await ws.recv())
            welcome = time.perf_counter()
            request = msg.RPCRequest(id=self.REQUEST_ID, method=self.METHOD, args={})
            await ws.send(request.model_dump_json())
            while json.loads(await ws.recv()).get("id") != self.REQUEST_ID:
                pass
            answered = time.perf_counter()
        return PracticeResult(
            ok=True,
            welcome_ms=round((welcome - started) * 1000, 2),
            answer_ms=round((answered - started) * 1000, 2),
        )


__all__ = ["FirstAnswerPractice", "PracticeResult"]
