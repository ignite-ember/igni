"""The socket to the igni host: how `ext.host` reaches igni.

igni starts an extension's process with `IGNI_HOST_URL` and
`IGNI_HOST_SECRET`: a WebSocket on the host's loopback interface and the
secret that says which process this is. Every `get`, `set` and `call` on
`ext.host` is one request on it, and every signal the extension handles
arrives on it. The protocol is igni's `docs/HOST_MODEL_PROTOCOL.md`; an
extension author never sees it.

Started without those variables — run outside igni, or by an igni older
than the host model — `ext.host` raises `NotConfigured`, except
`ext.host.notifications.notify`, which then travels as the MCP
notification igni has always understood.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from igni_extension.environment import HostEndpoint
from igni_extension.errors import HostError, HostUnavailable, NotConfigured
from igni_extension.hooks import HookHandlers
from igni_extension.notifications import LocalNotification, NotifyNotification
from igni_extension.session import HostSession
from igni_extension.signals import SignalHandlers

logger = logging.getLogger(__name__)

#: The one member that still works without the socket.
NOTIFY = "notifications.notify"


#: Members whose answer is the person's, so no timeout applies to them.
WAITS_ON_THE_PERSON = frozenset({"ui.show_dialog"})


class HostConnection:
    """One extension process's connection to the igni host."""

    #: How long a request waits for its answer.
    TIMEOUT = 120.0
    #: Between attempts to reach a host that is not answering yet.
    RETRY_SECONDS = 2.0

    def __init__(
        self,
        endpoint: HostEndpoint,
        signals: SignalHandlers,
        session: HostSession | None = None,
        hooks: HookHandlers | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._signals = signals
        self._hooks = hooks if hooks is not None else HookHandlers()
        self._session = session
        self._socket: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._ids = itertools.count(1)
        self._waiting: dict[int, asyncio.Future[Any]] = {}
        self._hello: asyncio.Future[dict[str, Any]] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        #: What the host said this extension holds, after connecting.
        self.granted: list[str] = []
        self.schema_version = ""
        signals.when_new(self._new_signal)

    @property
    def configured(self) -> bool:
        return self._endpoint.configured

    @property
    def connected(self) -> bool:
        return self._socket is not None

    # ── HostLink ──────────────────────────────────────────────────

    async def request(
        self,
        op: str,
        path: str,
        *,
        window: str | None = None,
        params: dict[str, Any] | None = None,
        value: Any = None,
    ) -> Any:
        if not self.configured:
            if op == "call" and path == NOTIFY and self._session is not None:
                notification = LocalNotification.model_validate((params or {})["notification"])
                await self._session.send(NotifyNotification.of(notification))
                return None
            raise NotConfigured(
                f"host.{path}: igni did not give this process the host's address "
                "(IGNI_HOST_URL, IGNI_HOST_SECRET) — it is not running inside igni"
            )
        await self.connect()
        body: dict[str, Any] = {"op": op, "path": path}
        if window is not None:
            body["window"] = window
        if params:
            body["params"] = params
        if op == "set":
            body["value"] = value
        return await self._send(body)

    # ── The socket ────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        """The secret, and — where there is one — this process's group.

        igni ends the group if the process hangs and it has to restart
        it; it checks that it started the group's leader first.
        """
        headers = {"Authorization": f"Bearer {self._endpoint.secret}"}
        if hasattr(os, "getpgid"):
            headers["X-Igni-Process-Group"] = str(os.getpgid(0))
        return headers

    async def connect(self) -> None:
        """Connect and say which signals are handled. Idempotent."""
        async with self._lock:
            if self._socket is not None:
                return
            try:
                socket = await connect(
                    self._endpoint.url,
                    additional_headers=self._headers(),
                    max_size=8 * 1024 * 1024,
                    compression=None,
                )
            except (OSError, WebSocketException) as exc:
                raise HostUnavailable(f"the igni host did not answer: {exc}") from exc
            self._socket = socket
            loop = asyncio.get_running_loop()
            self._hello = loop.create_future()
            self._reader = asyncio.create_task(self._read(socket))
            try:
                hello = await asyncio.wait_for(asyncio.shield(self._hello), self.TIMEOUT)
            except TimeoutError as exc:
                await self._drop()
                raise HostUnavailable("the igni host did not say hello") from exc
            self.granted = list(hello.get("granted") or [])
            self.schema_version = str(hello.get("schema_version") or "")
        wires = self._signals.wires()
        if wires:
            await self._subscribe(wires)

    async def serve_forever(self) -> None:
        """Stay connected while the process runs, so signals keep arriving."""
        if not self.configured:
            return
        while True:
            try:
                await self.connect()
            except HostUnavailable as exc:
                logger.debug("igni: %s; trying again", exc)
                await asyncio.sleep(self.RETRY_SECONDS)
                continue
            reader = self._reader
            if reader is not None:
                with contextlib.suppress(Exception):
                    await asyncio.shield(reader)
            await asyncio.sleep(self.RETRY_SECONDS)

    async def close(self) -> None:
        await self._drop()
        for task in list(self._tasks):
            task.cancel()

    async def _send(self, body: dict[str, Any]) -> Any:
        socket = self._socket
        if socket is None:
            raise HostUnavailable("not connected to the igni host")
        request_id = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._waiting[request_id] = future
        try:
            await socket.send(json.dumps({"id": request_id, **body}))
            # A dialog waits on the person, for as long as they take.
            limit = None if body.get("path") in WAITS_ON_THE_PERSON else self.TIMEOUT
            reply = await asyncio.wait_for(future, limit)
        except (OSError, WebSocketException) as exc:
            raise HostUnavailable(f"the igni host went away: {exc}") from exc
        except TimeoutError as exc:
            raise HostUnavailable(f"host.{body.get('path')} got no answer") from exc
        finally:
            self._waiting.pop(request_id, None)
        if reply.get("ok"):
            return reply.get("result")
        error = reply.get("error") or {}
        raise HostError(str(error.get("code") or "failed"), str(error.get("message") or "refused"))

    async def _subscribe(self, wires: list[str]) -> None:
        outcome = await self._send({"op": "subscribe", "signals": wires})
        if isinstance(outcome, dict):
            for wire in outcome.get("not_granted") or []:
                logger.warning(
                    "igni: %s will never arrive — its object is not in permissions.host", wire
                )
            for wire in outcome.get("unknown") or []:
                logger.warning("igni: this igni has no signal %s", wire)

    def _new_signal(self, wire: str) -> None:
        if self._socket is None:
            return  # sent with the rest on connect
        self._spawn(self._subscribe([wire]))

    async def _read(self, socket: ClientConnection) -> None:
        try:
            async for raw in socket:
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                op = message.get("op")
                if op == "hello" and self._hello is not None and not self._hello.done():
                    self._hello.set_result(message)
                elif op == "reply":
                    waiter = self._waiting.get(message.get("id"))
                    if waiter is not None and not waiter.done():
                        waiter.set_result(message)
                elif op == "signal":
                    # A task, not an await: a handler that reads the model
                    # needs this loop free to receive the answer.
                    self._spawn(
                        self._signals.deliver(str(message.get("signal")), message.get("value"))
                    )
                elif op == "hook":
                    # Off the read loop, as a signal is handled; igni
                    # waits on a deciding one, against a deadline.
                    hook_id = message.get("id")
                    name = str(message.get("event"))
                    if hook_id is None:
                        self._spawn(self._hooks.tell(name, message.get("value")))
                    else:
                        self._spawn(self._answer(str(hook_id), name, message.get("value")))
        except WebSocketException:
            pass
        finally:
            if self._socket is socket:
                self._socket = None
            for waiter in self._waiting.values():
                if not waiter.done():
                    waiter.set_exception(HostUnavailable("the igni host went away"))

    async def _answer(self, hook_id: str, event: str, value: Any) -> None:
        params = await self._hooks.answer(hook_id, event, value)
        try:
            await self._send({"op": "answer", "params": params})
        except (HostError, HostUnavailable) as exc:
            # Too late, or the host went away: the event went ahead or not
            # without this answer, as igni decides for a silence.
            logger.warning("igni: the answer to %s did not count: %s", event, exc)

    async def _drop(self) -> None:
        socket, self._socket = self._socket, None
        if socket is not None:
            with contextlib.suppress(Exception):
                await socket.close()
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(BaseException):
                await self._reader
            self._reader = None

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


__all__ = ["HostConnection"]
