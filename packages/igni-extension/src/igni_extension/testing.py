"""igni's side of an extension, played in-process, for the extension's tests.

`FakeHostTokenEndpoint` is the host's loopback token endpoint:

    host = FakeHostTokenEndpoint()
    ext = IgniExtension("acme.example", environ=host.environment(),
                        token_transport=host.transport, service_transport=my_fake_service)

`FakeHost` is the host's object model, on a loopback socket, the way igni
serves `ext.host`:

    async with FakeHost(values={"chat.processing": False}) as host:
        ext = IgniExtension("acme.example", environ=host.environment())
        assert await ext.host.chat.processing is False
        await host.emit("chat.run_completed", {...})

`HostHarness` is the host's MCP client, connected to the extension over
memory streams — the same session `ext.run()` serves over stdio — and
records what the extension tells the host:

    async with HostHarness(ext) as host:
        await host.session.call_tool("do_thing", {})
        [notification] = await host.received(1)
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import itertools
import json
import secrets
from collections.abc import Callable
from datetime import timedelta
from types import TracebackType
from typing import Any

import anyio
import httpx
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import ClientSession, types
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.message import SessionMessage
from mcp.shared.session import RequestResponder
from pydantic import BaseModel, RootModel
from websockets.asyncio.server import serve

from igni_extension.caller import Caller, CallerKind
from igni_extension.clock import Clock
from igni_extension.extension import IgniExtension
from igni_extension.host_model import SCHEMA_VERSION
from igni_extension.notifications import (
    CONDITIONS_METHOD,
    NOTIFY_METHOD,
    ConditionsNotification,
    LocalNotification,
    NotifyNotification,
)


class ManualClock(Clock):
    """A clock that moves only when told to."""

    def __init__(self, start: float = 1_000.0) -> None:
        self._now = start

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class TokenClaims(BaseModel):
    """What the fake host's tokens say: enough for `ext.tokens.signed_in`."""

    sub: str
    org: str
    email: str = ""
    name: str = ""
    aud: str
    token_use: str = "user"
    jti: str


#: Makes the token a fake host hands out for one person — see
#: `FakeHostTokenEndpoint(minter=...)`.
Minter = Callable[["TokenClaims"], str]


class FakeHostTokenEndpoint:
    """The host's token endpoint for one extension process (service identity §9).

    Hands out tokens for whoever is signed in (`sign_in`, `sign_out`). By
    default a token is an unsigned JWT carrying the person's claims — enough
    for `ext.tokens.signed_in`, and refused by a real service. To test
    against a service that checks signatures (`igni_service.testing.FakeIgni`),
    pass `minter`: it is given the person's `TokenClaims` (`sub`, `org`,
    `email`, `name`, `aud` — the service URL — and a fresh `jti`) and
    returns the token to hand out:

        igni = FakeIgni(...)
        host = FakeHostTokenEndpoint(
            minter=lambda claims: igni.person_token(claims.sub, org_id=claims.org)
        )
    """

    TOKEN_URL = "http://127.0.0.1:47011/igni/service-token"
    SERVICE_URL = "http://service.test"

    def __init__(
        self,
        *,
        service_url: str = SERVICE_URL,
        expires_in: float = 600,
        user_id: str = "u-ada",
        org_id: str = "org-acme",
        email: str = "ada@acme.test",
        name: str = "Ada",
        minter: Minter | None = None,
    ) -> None:
        self.service_url = service_url
        self.minter: Minter = minter or self.unsigned
        self.secret = secrets.token_urlsafe(24)
        self.expires_in = expires_in
        self.person: TokenClaims | None = None
        self.sign_in(user_id=user_id, org_id=org_id, email=email, name=name)
        #: Every token handed out, in order.
        self.issued: list[str] = []
        #: Every request, including refused ones.
        self.requests: list[httpx.Request] = []

    def environment(self, **extra: str) -> dict[str, str]:
        """The variables igni starts the process with, for its service."""
        return {
            "IGNI_SERVICE_URL": self.service_url,
            "IGNI_SERVICE_TOKEN_URL": self.TOKEN_URL,
            "IGNI_SERVICE_TOKEN_SECRET": self.secret,
            **extra,
        }

    def sign_in(self, *, user_id: str, org_id: str, email: str = "", name: str = "") -> None:
        self.person = TokenClaims(
            sub=user_id, org=org_id, email=email, name=name, aud=self.service_url, jti=""
        )

    def sign_out(self) -> None:
        self.person = None

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method != "GET" or str(request.url) != self.TOKEN_URL:
            return httpx.Response(404)
        if request.headers.get("authorization") != f"Bearer {self.secret}":
            return httpx.Response(401, json={"detail": "wrong secret"})
        if self.person is None:
            return httpx.Response(503, json={"detail": "nobody is signed in to igni"})
        named = request.headers.get(Caller.TOKEN_HEADER.lower(), "")
        if named and named != f"{CallerKind.PERSON.value}:{self.person.sub}":
            # As igni: a backend serving one person hands out that person's
            # token, and nobody else's.
            return httpx.Response(403, json={"error": f"not the signed-in person: {named}"})
        token = self.minter(self.person.model_copy(update={"jti": secrets.token_hex(8)}))
        self.issued.append(token)
        return httpx.Response(
            200,
            json={
                "access_token": token,
                "expires_in": self.expires_in,
                "service_url": self.service_url,
            },
        )

    @staticmethod
    def unsigned(claims: TokenClaims) -> str:
        """The default token: an unsigned JWT of `claims`."""

        def part(value: bytes) -> str:
            return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

        header = part(b'{"alg":"RS256","typ":"JWT"}')
        return f"{header}.{part(claims.model_dump_json().encode())}.{part(b'not-checked-here')}"


class HostBoundNotification(
    RootModel[types.ServerNotification | NotifyNotification | ConditionsNotification]
):
    """What the host's client accepts from an extension: MCP's own, and igni's."""


class HostClientSession(ClientSession):
    """An MCP client session that also understands igni's own notifications."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._receive_notification_type = HostBoundNotification


class HostHarness:
    """The igni host's MCP session with one extension, in this process."""

    def __init__(self, ext: IgniExtension, *, timeout: float = 5.0) -> None:
        self._ext = ext
        self._timeout = timeout
        self._stack = contextlib.AsyncExitStack()
        self._tasks: TaskGroup | None = None
        self._session: ClientSession | None = None
        self._changed = anyio.Event()
        #: What the extension told the host, parsed as the host parses it.
        self.notifications: list[LocalNotification] = []
        #: The same messages exactly as they crossed the wire.
        self.raw: list[dict[str, Any]] = []
        #: What the extension said about its conditions, in order.
        self.conditions: list[dict[str, bool]] = []

    @property
    def session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("enter the harness first: async with HostHarness(ext) as host")
        return self._session

    async def __aenter__(self) -> HostHarness:
        to_server_send, to_server_receive = anyio.create_memory_object_stream[
            SessionMessage | Exception
        ](16)
        from_server_send, from_server_receive = anyio.create_memory_object_stream[SessionMessage](
            16
        )
        to_client_send, to_client_receive = anyio.create_memory_object_stream[
            SessionMessage | Exception
        ](16)
        for stream in (
            to_server_send,
            to_server_receive,
            from_server_send,
            from_server_receive,
            to_client_send,
            to_client_receive,
        ):
            await self._stack.enter_async_context(stream)
        self._tasks = await self._stack.enter_async_context(anyio.create_task_group())
        self._tasks.start_soon(self._ext.serve, to_server_receive, from_server_send)
        self._tasks.start_soon(self._tap, from_server_receive, to_client_send)
        self._session = await self._stack.enter_async_context(
            HostClientSession(
                to_client_receive,
                to_server_send,
                read_timeout_seconds=timedelta(seconds=self._timeout),
                message_handler=self._on_message,
            )
        )
        await self._session.initialize()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._tasks is not None:
            self._tasks.cancel_scope.cancel()
        await self._stack.__aexit__(exc_type, exc, traceback)

    async def call(
        self, tool: str, arguments: dict[str, Any] | None = None, *, caller: Caller | None = None
    ) -> types.CallToolResult:
        """Call a tool as igni does: for `caller`, named in the request's `_meta`."""
        meta = {Caller.META_KEY: caller.model_dump(mode="json")} if caller is not None else None
        return await self.session.call_tool(tool, arguments or {}, meta=meta)

    async def received(self, count: int) -> list[LocalNotification]:
        """Wait until the host has `count` notifications from the extension."""
        with anyio.fail_after(self._timeout):
            while len(self.notifications) < count:
                await self._changed.wait()
                self._changed = anyio.Event()
        return list(self.notifications)

    async def conditions_said(self, count: int) -> list[dict[str, bool]]:
        """Wait until the extension has said `count` things about its conditions."""
        with anyio.fail_after(self._timeout):
            while len(self.conditions) < count:
                await self._changed.wait()
                self._changed = anyio.Event()
        return list(self.conditions)

    async def _tap(
        self,
        source: MemoryObjectReceiveStream[SessionMessage],
        sink: MemoryObjectSendStream[SessionMessage | Exception],
    ) -> None:
        async for message in source:
            wire = message.message.model_dump(by_alias=True, mode="json", exclude_none=True)
            if wire.get("method") in (NOTIFY_METHOD, CONDITIONS_METHOD):
                self.raw.append(wire)
            await sink.send(message)

    async def _on_message(
        self,
        message: RequestResponder[types.ServerRequest, types.ClientResult]
        | types.ServerNotification
        | Exception,
    ) -> None:
        root = getattr(message, "root", None)
        if isinstance(root, NotifyNotification):
            self.notifications.append(root.local())
            self._changed.set()
        elif isinstance(root, ConditionsNotification):
            self.conditions.append(dict(root.params.conditions))
            self._changed.set()


class HostCall(BaseModel):
    """One request an extension made of `FakeHost`."""

    op: str
    path: str
    window: str | None = None
    #: The session, for `host.sessions.chat(id)`.
    session: str | None = None
    params: dict[str, Any] = {}
    value: Any = None


class FakeHost:
    """igni's host object model for one extension process, on a loopback socket.

    Speaks the protocol igni does (`docs/HOST_MODEL_PROTOCOL.md` in igni).
    What it answers is up to the test:

    * `values` — `path -> value` for `get`; a `set` stores into it;
    * `answers` — `path -> value` (or a callable of the params) for `call`;
    * `refusals` — `path -> (code, message)`: that member is refused, as
      igni refuses an object the manifest does not grant;

    `emit(signal, value)` fires a signal at whoever subscribed, and
    `hook(event, value)` asks the extension about a deciding hook event as
    igni does, and returns its answer; `tell(event, value)` tells it one
    that decides nothing. Everything asked is kept in `calls`.
    """

    def __init__(
        self,
        *,
        extension_id: str = "acme.example",
        granted: tuple[str, ...] = (),
        values: dict[str, Any] | None = None,
        answers: dict[str, Any] | None = None,
        refusals: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        self.extension_id = extension_id
        self.granted = list(granted)
        self.values: dict[str, Any] = dict(values or {})
        self.answers: dict[str, Any] = dict(answers or {})
        self.refusals: dict[str, tuple[str, str]] = dict(refusals or {})
        self.secret = secrets.token_urlsafe(16)
        self.calls: list[HostCall] = []
        self.subscribed: set[str] = set()
        self._subscribed_changed = asyncio.Event()
        self._sockets: set[Any] = set()
        self._server: Any = None
        self._hooks: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._hook_ids = itertools.count(1)
        self.url = ""

    def environment(self, **extra: str) -> dict[str, str]:
        """The variables igni starts the process with, for its host."""
        return {"IGNI_HOST_URL": self.url, "IGNI_HOST_SECRET": self.secret, **extra}

    async def __aenter__(self) -> FakeHost:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.stop()

    async def start(self) -> None:
        self._server = await serve(self._serve, "127.0.0.1", 0, compression=None)
        port = int(next(iter(self._server.sockets)).getsockname()[1])
        self.url = f"ws://127.0.0.1:{port}/v1/host"

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def emit(self, signal: str, value: Any) -> None:
        """Fire `signal` (its wire name) at every connection that subscribed."""
        frame = json.dumps({"op": "signal", "signal": signal, "value": value})
        if signal not in self.subscribed:
            return
        for socket in list(self._sockets):
            with contextlib.suppress(Exception):
                await socket.send(frame)

    async def _connected(self, timeout: float) -> None:
        with anyio.fail_after(timeout):
            while not self._sockets:
                await asyncio.sleep(0.01)

    async def _send_all(self, frame: str) -> None:
        for socket in list(self._sockets):
            with contextlib.suppress(Exception):
                await socket.send(frame)

    async def hook(
        self, event: str, value: Any, timeout: float = 5.0, *, caller: Caller | None = None
    ) -> dict[str, Any]:
        """Ask the connected extension about a deciding `event`, as igni does; its answer's parameters."""
        await self._connected(timeout)
        hook_id = f"h-{next(self._hook_ids)}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._hooks[hook_id] = future
        await self._send_all(
            json.dumps(
                {
                    "op": "hook",
                    "id": hook_id,
                    "event": event,
                    "value": value,
                    "principal": caller.model_dump(mode="json") if caller is not None else None,
                }
            )
        )
        try:
            with anyio.fail_after(timeout):
                return await future
        finally:
            self._hooks.pop(hook_id, None)

    async def tell(
        self, event: str, value: Any, timeout: float = 5.0, *, caller: Caller | None = None
    ) -> None:
        """Tell the connected extension an `event` that decides nothing, as igni does."""
        await self._connected(timeout)
        principal = caller.model_dump(mode="json") if caller is not None else None
        await self._send_all(
            json.dumps(
                {"op": "hook", "id": None, "event": event, "value": value, "principal": principal}
            )
        )

    async def wait_subscribed(self, signal: str, timeout: float = 5.0) -> None:
        """Wait until the extension has subscribed to `signal`."""
        with anyio.fail_after(timeout):
            while signal not in self.subscribed:
                self._subscribed_changed.clear()
                await self._subscribed_changed.wait()

    async def _serve(self, socket: Any) -> None:
        headers = socket.request.headers if socket.request is not None else {}
        if headers.get("Authorization") != f"Bearer {self.secret}":
            await socket.close(4001, "unauthorized")
            return
        self._sockets.add(socket)
        try:
            await socket.send(
                json.dumps(
                    {
                        "op": "hello",
                        "protocol": 1,
                        "schema_version": SCHEMA_VERSION,
                        "extension_id": self.extension_id,
                        "granted": self.granted,
                    }
                )
            )
            async for raw in socket:
                request = json.loads(raw)
                await socket.send(json.dumps(self._answer(request)))
        finally:
            self._sockets.discard(socket)

    def _answer(self, request: dict[str, Any]) -> dict[str, Any]:
        op = str(request.get("op"))
        path = str(request.get("path") or "")
        reply: dict[str, Any] = {"op": "reply", "id": request.get("id"), "ok": True, "result": None}
        if op == "answer":
            params = dict(request.get("params") or {})
            waiting = self._hooks.get(str(params.get("hook")))
            if waiting is None or waiting.done():
                return {
                    **reply,
                    "ok": False,
                    "error": {"code": "not_found", "message": "nothing waits on it"},
                }
            waiting.set_result(params)
            return reply
        if op in ("subscribe", "unsubscribe"):
            wires = list(request.get("signals") or [])
            if op == "subscribe":
                self.subscribed.update(wires)
                self._subscribed_changed.set()
                reply["result"] = {"subscribed": wires, "not_granted": [], "unknown": []}
            else:
                self.subscribed.difference_update(wires)
            return reply
        self.calls.append(
            HostCall(
                op=op,
                path=path,
                window=request.get("window"),
                session=request.get("session"),
                params=dict(request.get("params") or {}),
                value=request.get("value"),
            )
        )
        if path in self.refusals:
            code, message = self.refusals[path]
            return {**reply, "ok": False, "error": {"code": code, "message": message}}
        if op == "get":
            if path not in self.values:
                return {**reply, "ok": False, "error": {"code": "not_found", "message": path}}
            reply["result"] = self.values[path]
        elif op == "set":
            self.values[path] = request.get("value")
        else:
            answer = self.answers.get(path)
            reply["result"] = (
                answer(dict(request.get("params") or {})) if callable(answer) else answer
            )
        return reply


# ── Concurrency: many callers at once ─────────────────────────────────


class ConcurrentCall(BaseModel):
    """One call the harness makes, as one caller."""

    tool: str
    arguments: dict[str, Any] = {}
    caller: Caller


class ConcurrentResult(BaseModel):
    call: ConcurrentCall
    result: Any = None
    is_error: bool = False
    #: Seconds from sending the call to its answer.
    took: float = 0.0


class ConcurrencyReport(BaseModel):
    """What one concurrent run found."""

    results: list[ConcurrentResult]
    #: The longest the extension's event loop went without running anything
    #: else, in seconds — what a blocking call costs every other caller.
    longest_stall: float
    #: Calls during which `Caller.current()` named somebody else.
    leaks: list[str]


class ConcurrencyProblem(AssertionError):
    """The extension blocked its loop, or let one call see another's caller."""


class ConcurrencyHarness:
    """Fires calls from different callers at once, as igni does, and checks two things.

    * **Nothing blocks the loop.** One extension process serves every
      caller of its backend, on one event loop. A call that holds it — a
      sync tool's work inside an `async def`, CPU work not moved off it —
      makes every other caller wait, and a hook that waits past igni's
      deadline counts against the extension. The harness measures the
      longest stall while the calls run, and fails above `stall_limit`.
    * **Callers stay apart.** Every call carries its caller; the harness
      checks that `Caller.current()`, as each tool starts and as it ends,
      is the one that call was sent with. A caller kept in a global, or
      a scope that leaks, fails here.

        async def test_two_callers_at_once():
            report = await ConcurrencyHarness(ext).run([
                ConcurrentCall(tool="mine", caller=ada),
                ConcurrentCall(tool="mine", caller=grace),
            ])
            assert report.results[0].result != report.results[1].result

    It checks the SDK's view of the caller; what the extension does with
    it — a cache keyed by nothing, a module-level "current user" — is the
    author's to assert on the results.
    """

    #: The ticker's period, in seconds.
    TICK = 0.005
    #: The `_meta` key the harness numbers its calls with.
    CALL_KEY = "igni/test-call"

    def __init__(
        self, ext: IgniExtension, *, stall_limit: float = 0.1, timeout: float = 10.0
    ) -> None:
        self._ext = ext
        self._stall_limit = stall_limit
        self._timeout = timeout

    async def run(self, calls: list[ConcurrentCall], *, check: bool = True) -> ConcurrencyReport:
        """Make every call at once; `ConcurrencyProblem` on a stall or a leak, unless `check=False`."""
        leaks: list[str] = []
        restore = self._watch(calls, leaks)
        stalls: list[float] = []
        running = True

        async def tick() -> None:
            loop = asyncio.get_running_loop()
            while running:
                started = loop.time()
                await asyncio.sleep(self.TICK)
                stalls.append(max(0.0, loop.time() - started - self.TICK))

        try:
            async with HostHarness(self._ext, timeout=self._timeout) as host:
                ticker = asyncio.create_task(tick())
                try:
                    results = await asyncio.gather(
                        *(self._one(host, index, call) for index, call in enumerate(calls))
                    )
                finally:
                    running = False
                    await ticker
        finally:
            restore()
        report = ConcurrencyReport(
            results=list(results), longest_stall=max(stalls, default=0.0), leaks=leaks
        )
        if check:
            self.check(report)
        return report

    def check(self, report: ConcurrencyReport) -> None:
        problems: list[str] = []
        if report.longest_stall > self._stall_limit:
            problems.append(
                f"the event loop was held for {report.longest_stall:.3f}s "
                f"(limit {self._stall_limit:.3f}s): something blocks it — a sync call inside "
                "an `async def`, or CPU work not moved off the loop"
            )
        problems.extend(report.leaks)
        if problems:
            raise ConcurrencyProblem("; ".join(problems))

    async def _one(self, host: HostHarness, index: int, call: ConcurrentCall) -> ConcurrentResult:
        meta = {Caller.META_KEY: call.caller.model_dump(mode="json"), self.CALL_KEY: index}
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await host.session.call_tool(call.tool, call.arguments, meta=meta)
        took = loop.time() - started
        return ConcurrentResult(
            call=call,
            result=result.structuredContent
            if result.structuredContent is not None
            else result.content,
            is_error=bool(result.isError),
            took=took,
        )

    def _watch(self, calls: list[ConcurrentCall], leaks: list[str]) -> Callable[[], None]:
        """Wrap every tool to compare the caller it sees with the one its call was sent with."""
        manager = self._ext.mcp._tool_manager
        originals: dict[str, Any] = {}
        call_key = self.CALL_KEY
        for tool in manager.list_tools():
            original = tool.fn
            originals[tool.name] = (tool, original, tool.is_async)
            run = OffTheLoopCall(original, tool.is_async)

            async def watched(
                *args: Any, _run: OffTheLoopCall = run, _name: str = tool.name, **kwargs: Any
            ) -> Any:
                expected = calls[ConcurrencyHarness.sent_index(call_key)].caller
                ConcurrencyHarness.compare(leaks, _name, "as it started", expected)
                try:
                    return await _run(*args, **kwargs)
                finally:
                    ConcurrencyHarness.compare(leaks, _name, "as it ended", expected)

            tool.fn = watched
            tool.is_async = True

        def restore() -> None:
            for tool, original, is_async in originals.values():
                tool.fn = original
                tool.is_async = is_async

        return restore

    @staticmethod
    def sent_index(key: str) -> int:
        """Which of the harness's calls the request being served is."""
        meta = request_ctx.get().meta
        raw = meta.model_dump().get(key) if meta is not None else None
        return int(raw) if raw is not None else -1

    @staticmethod
    def compare(leaks: list[str], tool: str, when: str, expected: Caller) -> None:
        seen = Caller.current()
        if seen != expected:
            leaks.append(
                f"{tool} {when} saw caller {seen.ref if seen else None!r}, "
                f"but was called as {expected.ref!r}"
            )


class OffTheLoopCall:
    """A tool's function as FastMCP would call it: awaited when async, called when not."""

    def __init__(self, fn: Callable[..., Any], is_async: bool) -> None:
        self._fn = fn
        self._is_async = is_async

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self._is_async:
            return await self._fn(*args, **kwargs)
        return self._fn(*args, **kwargs)
