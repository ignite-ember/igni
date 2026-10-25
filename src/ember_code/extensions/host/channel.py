"""The host model's channel to an extension's process: a loopback WebSocket.

An extension's process reaches ``ext.host`` over one WebSocket on this
host, opened by its SDK. igni starts the process with two variables
(:meth:`HostChannel.grant`):

* ``IGNI_HOST_URL`` — ``ws://127.0.0.1:<port>/v1/host``;
* ``IGNI_HOST_SECRET`` — the per-process secret, the same one the
  service-token endpoint takes (``IGNI_SERVICE_TOKEN_SECRET``, when the
  extension has a service): one secret names one process.

The secret is the only thing that says which extension is connecting —
it is never a parameter — so an extension can only ever be itself.
Loopback only; a request carrying an ``Origin`` is refused, because a
browser sends one and the SDK does not, and a page on any site could
otherwise try a loopback port; ``Host`` must be the loopback address
(DNS rebinding). The protocol on the socket is in
``docs/HOST_MODEL_PROTOCOL.md``; nothing but the SDKs speaks it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import http
import json
import logging
from typing import Any

from pydantic import ValidationError
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Request as HttpRequest
from websockets.http11 import Response as HttpResponse

from ember_code.extensions.host.hooks import ExtensionHooks
from ember_code.extensions.host.model import HostModel
from ember_code.extensions.host.processes import PROCESS_GROUP_HEADER, ExtensionProcesses
from ember_code.extensions.host.spec import ErrorCode
from ember_code.extensions.host.wire import (
    ErrorBody,
    Hello,
    HookAnswer,
    Op,
    Reply,
    Request,
    SignalMessage,
)

logger = logging.getLogger(__name__)

#: The variables an extension process is started with.
ENV_HOST_URL = "IGNI_HOST_URL"
ENV_HOST_SECRET = "IGNI_HOST_SECRET"

LOOPBACK = "127.0.0.1"
PATH = "/v1/host"

#: A frame bigger than this is not a request. Views (json-render
#: specs) are the largest thing an extension sends.
MAX_FRAME = 8 * 1024 * 1024


class _SocketSink:
    """Signals for one connection, written in order by one task."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=10_000)

    def deliver(self, message: SignalMessage) -> None:
        try:
            self.queue.put_nowait(message.model_dump_json())
        except asyncio.QueueFull:
            logger.warning("host channel: a subscriber is not reading; dropped %s", message.signal)

    def send(self, frame: str) -> None:
        """A hook event, in order with everything else on the socket."""
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            logger.warning("host channel: a process is not reading; a hook event is lost")


class HostChannel:
    """The loopback listener extension processes reach the host model on."""

    def __init__(
        self,
        model: HostModel,
        *,
        host: str = LOOPBACK,
        hooks: ExtensionHooks | None = None,
        processes: ExtensionProcesses | None = None,
    ) -> None:
        self._model = model
        self._hooks = hooks if hooks is not None else ExtensionHooks.shared()
        self._processes = processes if processes is not None else ExtensionProcesses.shared()
        self._host = host
        self._port = 0
        self._server: Server | None = None
        self._secrets: dict[str, str] = {}
        self._connections: dict[str, set[ServerConnection]] = {}
        self._start_lock = asyncio.Lock()

    @property
    def url(self) -> str:
        if not self._port:
            raise RuntimeError("the host channel is not listening; await start()")
        return f"ws://{self._host}:{self._port}{PATH}"

    async def start(self) -> None:
        async with self._start_lock:
            if self._server is not None:
                return
            self._server = await serve(
                self._serve,
                self._host,
                0,
                process_request=self._admit,
                max_size=MAX_FRAME,
                compression=None,
            )
            sockets = list(self._server.sockets or [])
            self._port = int(sockets[0].getsockname()[1])
            logger.debug("host channel listening at %s", self.url)

    async def stop(self) -> None:
        server, self._server = self._server, None
        self._port = 0
        self._secrets.clear()
        if server is None:
            return
        server.close()
        with contextlib.suppress(Exception):
            await server.wait_closed()

    async def grant(self, extension_id: str, secret: str) -> dict[str, str]:
        """Let one extension process connect, with its per-process secret.

        Returns the environment to start it with. A second grant replaces
        the first, so a restarted process cannot use its predecessor's.
        """
        await self.start()
        self._secrets[extension_id] = secret
        return {ENV_HOST_URL: self.url, ENV_HOST_SECRET: secret}

    def revoke(self, extension_id: str) -> None:
        self._secrets.pop(extension_id, None)
        for connection in list(self._connections.pop(extension_id, set())):
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().create_task(connection.close(4001, "revoked"))

    # ── Admission ─────────────────────────────────────────────────

    def _extension_for(self, headers: Headers) -> str | None:
        scheme, _, presented = headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not presented.strip():
            return None
        presented = presented.strip()
        found: str | None = None
        # Every secret compared, in constant time, whatever matches.
        for extension_id, secret in self._secrets.items():
            if hmac.compare_digest(secret.encode(), presented.encode()):
                found = extension_id
        return found

    def _admit(self, connection: ServerConnection, request: HttpRequest) -> HttpResponse | None:
        if request.path.split("?", 1)[0] != PATH:
            return connection.respond(http.HTTPStatus.NOT_FOUND, "no such endpoint\n")
        if "Origin" in request.headers:
            # A browser. The SDK never sends one.
            return connection.respond(http.HTTPStatus.FORBIDDEN, "not for browsers\n")
        if request.headers.get("Host", "") not in (
            f"{self._host}:{self._port}",
            f"[{self._host}]:{self._port}",
        ):
            return connection.respond(http.HTTPStatus.MISDIRECTED_REQUEST, "wrong host\n")
        if self._extension_for(request.headers) is None:
            return connection.respond(
                http.HTTPStatus.UNAUTHORIZED, "the secret is not one this host issued\n"
            )
        return None

    # ── One connection ────────────────────────────────────────────

    async def _serve(self, connection: ServerConnection) -> None:
        request = connection.request
        extension_id = self._extension_for(request.headers) if request is not None else None
        if extension_id is None:
            await connection.close(4001, "unauthorized")
            return
        self._connections.setdefault(extension_id, set()).add(connection)
        sink = _SocketSink()
        subscriber = self._model.bus.attach(extension_id, sink)
        grants = self._model.grants_for(extension_id)
        hello = Hello(
            schema_version=self._model.schema.version,
            extension_id=extension_id,
            granted=grants.granted(self._model.schema.permission_names()) if grants else [],
        )
        writer = asyncio.create_task(self._write(connection, sink))
        pending: set[asyncio.Task[None]] = set()
        self._hooks.connect(extension_id, sink.send)
        try:
            await connection.send(hello.model_dump_json())
            group = request.headers.get(PROCESS_GROUP_HEADER) if request is not None else None
            if group is not None:
                # Which process group to end if it hangs (:mod:`.processes`).
                await self._processes.claim(extension_id, group)
            async for raw in connection:
                task = asyncio.create_task(self._answer(extension_id, subscriber, sink, raw))
                pending.add(task)
                task.add_done_callback(pending.discard)
        except Exception as exc:  # noqa: BLE001 — a connection ends however it ends
            logger.debug("host channel: %s's connection ended: %s", extension_id, exc)
        finally:
            self._hooks.disconnect(extension_id, sink.send)
            self._model.bus.detach(subscriber)
            self._connections.get(extension_id, set()).discard(connection)
            for task in pending:
                task.cancel()
            sink.queue.put_nowait(None)
            with contextlib.suppress(Exception):
                await writer

    async def _answer(
        self, extension_id: str, subscriber: Any, sink: _SocketSink, raw: str | bytes
    ) -> None:
        data: Any = None
        try:
            data = json.loads(raw)
            request = Request.model_validate(data)
        except (ValueError, ValidationError) as exc:
            request_id = data.get("id", 0) if isinstance(data, dict) else 0
            reply = Reply(
                id=request_id,
                ok=False,
                error=ErrorBody(code=ErrorCode.INVALID, message=f"not a host-model request: {exc}"),
            )
        else:
            if request.op is Op.ANSWER:
                reply = self._answered(extension_id, request)
            else:
                reply = await self._model.handle(extension_id, request, subscriber=subscriber)
        with contextlib.suppress(asyncio.QueueFull):
            sink.queue.put_nowait(reply.model_dump_json())

    def _answered(self, extension_id: str, request: Request) -> Reply:
        """The process's answer to a hook event. Only a process answers; a page cannot."""
        try:
            answer = HookAnswer.model_validate(request.params)
        except ValidationError as exc:
            return Reply(
                id=request.id,
                ok=False,
                error=ErrorBody(code=ErrorCode.INVALID, message=f"not an answer: {exc}"),
            )
        if not self._hooks.answered(extension_id, answer):
            return Reply(
                id=request.id,
                ok=False,
                error=ErrorBody(
                    code=ErrorCode.NOT_FOUND,
                    message=f"nothing waits on {answer.hook!r} from this extension; "
                    "it was answered already, or ran out of time",
                ),
            )
        return Reply(id=request.id, ok=True)

    @staticmethod
    async def _write(connection: ServerConnection, sink: _SocketSink) -> None:
        while True:
            frame = await sink.queue.get()
            if frame is None:
                return
            try:
                await connection.send(frame)
            except Exception:  # noqa: BLE001 — the connection is gone
                return


__all__ = ["ENV_HOST_SECRET", "ENV_HOST_URL", "PATH", "HostChannel"]
