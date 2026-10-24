"""igni's side of an extension, played in-process, for the extension's tests.

`FakeHostTokenEndpoint` is the host's loopback token endpoint:

    host = FakeHostTokenEndpoint()
    ext = IgniExtension("acme.example", environ=host.environment(),
                        token_transport=host.transport, service_transport=my_fake_service)

`HostHarness` is the host's MCP client, connected to the extension over
memory streams — the same session `ext.run()` serves over stdio — and
records what the extension tells the host:

    async with HostHarness(ext) as host:
        await host.session.call_tool("do_thing", {})
        [notification] = await host.received(1)
"""

from __future__ import annotations

import base64
import contextlib
import secrets
from datetime import timedelta
from types import TracebackType
from typing import Any

import anyio
import httpx
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import ClientSession, types
from mcp.shared.message import SessionMessage
from mcp.shared.session import RequestResponder
from pydantic import BaseModel, RootModel

from igni_extension.clock import Clock
from igni_extension.extension import IgniExtension
from igni_extension.notifications import NOTIFY_METHOD, LocalNotification, NotifyNotification


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


class FakeHostTokenEndpoint:
    """The host's token endpoint for one extension process (service identity §9)."""

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
    ) -> None:
        self.service_url = service_url
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
        token = self._mint(self.person.model_copy(update={"jti": secrets.token_hex(8)}))
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
    def _mint(claims: TokenClaims) -> str:
        def part(value: bytes) -> str:
            return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

        header = part(b'{"alg":"RS256","typ":"JWT"}')
        return f"{header}.{part(claims.model_dump_json().encode())}.{part(b'not-checked-here')}"


class HostBoundNotification(RootModel[types.ServerNotification | NotifyNotification]):
    """What the host's client accepts from an extension: MCP's own, and igni's."""


class HostClientSession(ClientSession):
    """An MCP client session that also understands `notifications/igni/notify`."""

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

    async def received(self, count: int) -> list[LocalNotification]:
        """Wait until the host has `count` notifications from the extension."""
        with anyio.fail_after(self._timeout):
            while len(self.notifications) < count:
                await self._changed.wait()
                self._changed = anyio.Event()
        return list(self.notifications)

    async def _tap(
        self,
        source: MemoryObjectReceiveStream[SessionMessage],
        sink: MemoryObjectSendStream[SessionMessage | Exception],
    ) -> None:
        async for message in source:
            wire = message.message.model_dump(by_alias=True, mode="json", exclude_none=True)
            if wire.get("method") == NOTIFY_METHOD:
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
