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
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from typing import Any

import anyio
import httpx
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp.server.fastmcp import FastMCP
from mcp.shared.message import SessionMessage

from igni_extension.channel import HostConnection
from igni_extension.clock import Clock
from igni_extension.environment import ExtensionEnvironment
from igni_extension.errors import NotConfigured
from igni_extension.hints import ToolHints, ToolHintsMissing
from igni_extension.host import Host
from igni_extension.members import Signal
from igni_extension.prompt import PromptConditions
from igni_extension.service import ServiceClient
from igni_extension.session import HostSession
from igni_extension.signals import SignalHandlers
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
        #: The socket to the igni host that `ext.host` travels on.
        self.connection = HostConnection(self.environment.host, self._signals, self._session)
        #: igni, as this extension sees it: `ext.host.chat`, `ext.host.ui`, …
        self.host = Host(self.connection)
        #: Which of the conditions its prompt contributions wait on hold.
        self.prompt = PromptConditions(self._session)
        self.tokens = HostTokens(self.environment.service, transport=token_transport, clock=clock)
        self._service_transport = service_transport
        self._service: ServiceClient | None = None
        self._background: list[Callable[[], Coroutine[Any, Any, None]]] = []

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
        """
        return self.mcp.tool(name=name, description=description, annotations=hints.annotations())

    def on[T](
        self, signal: Signal[T]
    ) -> Callable[[Callable[[T], Awaitable[None]]], Callable[[T], Awaitable[None]]]:
        """Handle a host signal: `@ext.on(ext.host.<object>.<signal>)`."""
        return self._signals.on(signal)

    def on_start(
        self, fn: Callable[[], Coroutine[Any, Any, None]]
    ) -> Callable[[], Coroutine[Any, Any, None]]:
        """Run `fn` in the background for as long as the process serves igni.

        It starts with `run()`, beside the MCP server, and is cancelled
        when igni closes the pipe — so work the extension owns for its
        whole life (a sync loop, a process it started) cleans up in its
        own `finally`. An exception it raises is logged by asyncio and
        does not stop the extension.
        """
        self._background.append(fn)
        return fn

    @property
    def signals(self) -> SignalHandlers:
        return self._signals

    # ── Its service ─────────────────────────────────────────────────

    @property
    def service(self) -> ServiceClient:
        """An HTTP client for this extension's own service, as the signed-in person.

        Paths are relative to the service. The person's token is attached
        and renewed; `NotSignedIn` is raised when nobody is signed in to
        igni. `ext.service.anonymous` reaches the same service with no
        token, and `ext.service.websocket(path)` opens a WebSocket to it
        as the person.
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
        background = [asyncio.create_task(fn()) for fn in self._background]
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
