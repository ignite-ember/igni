"""`IgniExtension`: an extension's process, as its author writes it.

ext = IgniExtension("acme.example")

@ext.tool(hints=ToolHints(read_only=True, destructive=False, idempotent=True, open_world=False))
async def summarise(slug: str) -> str:
    response = await ext.service.get(f"/v1/things/{slug}")
    ...

ext.run()
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any

import anyio
import httpx
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp.server.fastmcp import FastMCP
from mcp.shared.message import SessionMessage
from pydantic import BaseModel

from igni_extension.caller import CallerScope
from igni_extension.channel import HostConnection
from igni_extension.clock import Clock
from igni_extension.environment import ExtensionEnvironment
from igni_extension.errors import NotConfigured
from igni_extension.hints import ToolHints, ToolHintsMissing
from igni_extension.hooks import HookEvent, HookHandler, HookHandlers
from igni_extension.host import Host
from igni_extension.host_model import BusyLease
from igni_extension.members import Signal
from igni_extension.prompt import PromptConditions
from igni_extension.service import ServiceClient
from igni_extension.service_events import ServiceEventHandler, ServiceEventHandlers
from igni_extension.session import HostSession
from igni_extension.signals import SignalHandlers
from igni_extension.threads import OffTheLoop
from igni_extension.tokens import HostTokens

AnyFunction = Callable[..., Any]


class IgniExtension:
    """One extension's process: its tools, its service, and the host."""

    def __init__(
        self,
        extension_id: str,
        *,
        instructions: str | None = None,
        environ: Mapping[str, str] | None = None,
        token_transport: httpx.AsyncBaseTransport | None = None,
        service_transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock | None = None,
    ) -> None:
        if not extension_id:
            raise ValueError("an extension is named by its manifest id")
        self.id = extension_id
        self.environment = ExtensionEnvironment.read(environ)
        #: What it ships for this platform: `ext.assets.path("neo4j")`.
        self.assets = self.environment.assets
        self.mcp = FastMCP(extension_id, instructions=instructions)
        self._session = HostSession(self.mcp._mcp_server)
        self._signals = SignalHandlers()
        self._hooks = HookHandlers()
        self._service_events = ServiceEventHandlers()
        #: The socket to the igni host that `ext.host` travels on.
        self.connection = HostConnection(
            self.environment.host, self._signals, self._session, self._hooks, self._service_events
        )
        #: igni, as this extension sees it: `ext.host.chat`, `ext.host.ui`, …
        self.host = Host(self.connection)
        #: Which of the conditions its prompt contributions wait on hold.
        self.prompt = PromptConditions(self._session)
        self.tokens = HostTokens(self.environment.service, transport=token_transport, clock=clock)
        self._service_transport = service_transport
        self._service: ServiceClient | None = None
        self._background: list[Callable[[], Any]] = []

    # ── What the extension offers ───────────────────────────────────

    def tool(
        self,
        *,
        hints: ToolHints,
        name: str | None = None,
        description: str | None = None,
    ) -> Callable[[AnyFunction], AnyFunction]:
        """Register a tool igni can call: `@ext.tool(hints=ToolHints(...))`.

        `hints` is required: igni's permission modes decide on what a
        tool says it is (`igni_extension.hints`). The same four go in
        the manifest's `permissions.tools` for a tool the agent calls.
        A plain `def` runs in a worker thread, so it never holds the
        other calls (`igni_extension.threads`). A parameter typed
        `Caller` receives whom the call is for, and is not part of the
        tool's input (`igni_extension.caller`).
        """
        register = self.mcp.tool(
            name=name, description=description, annotations=hints.annotations()
        )

        def off_the_loop(fn: AnyFunction) -> AnyFunction:
            # A parameter typed `Caller` is filled from the call, and is
            # not part of the tool's input (`igni_extension.caller`).
            served = CallerScope.inject(fn)
            register(served)
            # The schema is FastMCP's, from `fn` as written; only how it is
            # run changes — a plain `def` in a worker thread (`threads`).
            if not OffTheLoop.is_async(served):
                tool = self.mcp._tool_manager.get_tool(name or fn.__name__)
                if tool is not None:
                    tool.fn = OffTheLoop.wrap(served)
                    tool.is_async = True
            return fn

        return off_the_loop

    def on[T](
        self, signal: Signal[T]
    ) -> Callable[[Callable[[T], Awaitable[None] | None]], Callable[[T], Awaitable[None] | None]]:
        """Handle a host signal: `@ext.on(ext.host.<object>.<signal>)`.

        A reaction: igni never waits on it. A plain `def` runs in a
        worker thread.
        """
        return self._signals.on(signal)

    def hook[T: BaseModel](self, event: HookEvent[T]) -> Callable[[HookHandler[T]], HookHandler[T]]:
        """Answer one of igni's hook events: `@ext.hook(Hooks.PRE_TOOL_USE)`.

        On an event that decides (`event.decides`) the handler returns
        `Allow()`, `Reject(reason)` or `Modify(value)` (`igni_extension.hooks`);
        on any other it is only told. igni asks only about the events the
        manifest lists in `permissions.hooks`; one handler per event.
        """
        return self._hooks.on(event)

    def on_service_event(
        self, event_type: str | None = None
    ) -> Callable[[ServiceEventHandler], ServiceEventHandler]:
        """Handle the events this extension's service sends (`igni_extension.service_events`).

        igni posts nothing for an event: the handler acts through its host
        calls (`ext.host.sessions.chat(id).post(...)`), and the agent runs
        only if it posts. An event with no handler for its type is reported
        to the service as not handled. Without `event_type` it handles every
        type no other handler names.
        """
        return self._service_events.on(event_type)

    def on_start[F: Callable[[], Any]](self, fn: F) -> F:
        """Run `fn` in the background for as long as the process serves igni.

        It starts with `run()`, beside the MCP server, and is cancelled
        when igni closes the pipe — so work the extension owns for its
        whole life (a sync loop, a process it started) cleans up in its
        own `finally`. An exception it raises is logged by asyncio and
        does not stop the extension. A plain `def` runs in a worker
        thread, and cannot be cancelled: it ends with the process.
        """
        self._background.append(fn)
        return fn

    @property
    def signals(self) -> SignalHandlers:
        return self._signals

    # ── Busy ────────────────────────────────────────────────────────

    async def busy(self, reason: str, ttl: float | None = None) -> BusyLease:
        """Say this extension is working, so igni's process is not suspended under it.

        In the cloud igni's VM sleeps when nothing is busy. Work the
        extension does on its own — indexing, a sync — takes a lease for
        `reason`; it expires after `ttl` seconds (default and at most
        600) unless renewed by calling again, so a stuck extension cannot
        keep the VM awake. `ext.done(reason)` lets go; `ext.busy_while`
        does both and renews in between.
        """
        return await self.host.self.busy(reason, ttl)

    async def done(self, reason: str) -> None:
        """Let go of the lease `busy` took for `reason`."""
        await self.host.self.done(reason)

    @contextlib.asynccontextmanager
    async def busy_while(self, reason: str, ttl: float = 120.0) -> AsyncIterator[None]:
        """Busy for as long as the block runs, renewed every half `ttl`.

        async with ext.busy_while("indexing"):
            await index_everything()
        """
        await self.busy(reason, ttl)

        async def renew() -> None:
            while True:
                await asyncio.sleep(ttl / 2)
                await self.busy(reason, ttl)

        renewing = asyncio.get_running_loop().create_task(renew())
        try:
            yield
        finally:
            renewing.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await renewing
            with contextlib.suppress(Exception):
                await self.done(reason)

    # ── Its service ─────────────────────────────────────────────────

    @property
    def service(self) -> ServiceClient:
        """An HTTP client for this extension's own service, as the signed-in person.

        Paths are relative to the service. The person's token is attached
        and renewed; `NotSignedIn` is raised when nobody is signed in to
        igni. `ext.service.anonymous` reaches the same service with no
        token, and `ext.service.websocket(path)` opens a WebSocket to it
        as the person. `ext.service.for_(caller)` is the same service as
        the caller a tool was handed — what a tool acting for someone uses.
        """
        if self._service is None:
            if not self.environment.service.configured:
                raise NotConfigured(
                    f"{self.id} was not started with its service's URL (IGNI_SERVICE_URL)"
                )
            self._service = ServiceClient(
                base_url=self.environment.service.url,
                tokens=self.tokens,
                transport=self._service_transport,
            )
        return self._service

    async def connect(self) -> None:
        """Connect to the igni host now rather than on the first `ext.host` call.

        `ext.run()` does this itself, and stays connected so that signals
        arrive; a test or a script that drives the extension without
        `run()` calls it.
        """
        await self.connection.connect()

    async def aclose(self) -> None:
        await self.connection.close()
        if self._service is not None:
            await self._service.aclose()
            self._service = None

    # ── Running ─────────────────────────────────────────────────────

    def run(self) -> None:
        """Serve igni over stdio until it closes the pipe. The console script calls this."""
        anyio.run(self._run_stdio)

    async def check_tools(self) -> None:
        """Refuse to serve a tool that leaves one of its four hints unstated.

        `ext.run()` and `serve()` call it first. A tool registered on
        `ext.mcp` directly, rather than through `ext.tool`, is checked
        the same way.
        """
        ToolHintsMissing.check(await self.mcp.list_tools())

    async def _run_stdio(self) -> None:
        await self.check_tools()
        # The host's socket alongside MCP, so signals arrive whether or
        # not igni is calling a tool.
        host = asyncio.create_task(self.connection.serve_forever())
        background = [asyncio.create_task(OffTheLoop.call(fn)) for fn in self._background]
        try:
            await self.mcp.run_stdio_async()
        finally:
            for task in [*background, host]:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
            await self.aclose()

    async def serve(
        self,
        read_stream: MemoryObjectReceiveStream[SessionMessage | Exception],
        write_stream: MemoryObjectSendStream[SessionMessage],
    ) -> None:
        """Serve one MCP session over the given streams (what `run` does over stdio)."""
        await self.check_tools()
        server = self.mcp._mcp_server
        await server.run(read_stream, write_stream, server.create_initialization_options())
