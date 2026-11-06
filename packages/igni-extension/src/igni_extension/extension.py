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
import functools
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from typing import Any

import anyio
import httpx
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.message import SessionMessage
from pydantic import BaseModel

from igni_extension.caller import CallerScope, RequestMeta
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

#: Entered around each tool call, given the tool's name, in the call's context.
ToolObserver = Callable[[str], contextlib.AbstractContextManager[Any]]


class ToolNames(BaseModel):
    """Every name one tool goes by, from where each is written.

    `tool` is the name in this code (the function's, or `name=`); the
    others are derived from it and the extension's id, by igni's rules.
    """

    #: What `ext.tool` registered, and `HostHarness.call` takes.
    tool: str
    #: The manifest's `permissions.tools[].name` (and `permissions.rpc` is
    #: `rpc` below): namespaced by the extension's id, so an administrator
    #: reading it knows whose tool it is.
    manifest: str
    #: What a page calls with `bridge.rpc(...)`, and `permissions.rpc` grants.
    rpc: str
    #: What igni registers it as for its agent, and the name its permission
    #: rules and settings-file hooks match: `mcp_ext:<id>_<tool>`.
    registered: str

    @classmethod
    def of(cls, extension_id: str, tool: str) -> ToolNames:
        return cls(
            tool=tool,
            manifest=f"{extension_id}.{tool}",
            rpc=f"ext.{extension_id}.{tool}",
            registered=f"mcp_ext:{extension_id}_{tool}",
        )


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
        self._session = HostSession()
        #: The MCP server igni talks to. Every message igni sends passes the
        #: two middlewares: the request's `_meta` is current while it is
        #: served (`Caller.current()`), and its session is kept so the
        #: process can speak first (`igni_extension.session`).
        self.mcp = MCPServer(
            extension_id,
            instructions=instructions,
            middleware=[RequestMeta.middleware, self._session.middleware],
        )
        #: What each tool `ext.tool` registered says it is, by its name.
        self._hints: dict[str, ToolHints] = {}
        #: Told of every call to a tool `ext.tool` registered, around it, in
        #: the call's own context — what `testing.ConcurrencyHarness` watches.
        self._observers: list[ToolObserver] = []
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
        other calls (`mcp` does this, as `igni_extension.threads` does for
        the SDK's other callbacks). A parameter typed
        `Caller` receives whom the call is for, and is not part of the
        tool's input (`igni_extension.caller`).
        """
        register = self.mcp.tool(
            name=name, description=description, annotations=hints.annotations()
        )

        def registered(fn: AnyFunction) -> AnyFunction:
            tool = name or fn.__name__
            # A parameter typed `Caller` is filled from the call, and is
            # not part of the tool's input (`igni_extension.caller`). The
            # schema is `mcp`'s, from the function as written; a plain `def`
            # it runs in a worker thread.
            register(self._observed(tool, CallerScope.inject(fn)))
            self._hints[tool] = hints
            # `lookup_ticket.manifest_name`: the line `permissions.tools` needs.
            with contextlib.suppress(AttributeError, TypeError):
                fn.manifest_name = ToolNames.of(self.id, tool).manifest  # type: ignore[attr-defined]
            return fn

        return registered

    def _observed(self, tool: str, fn: AnyFunction) -> AnyFunction:
        """`fn` as the server runs it: observed, and failing in its own words.

        The observers are told of each call (`observing`). An exception
        the tool raises reaches igni — and the model — as its message, as
        it did on `mcp` 1: `mcp` 2 would send only "Error executing tool",
        which tells nobody what went wrong. A plain `def` stays plain, so
        `mcp` still runs it in a worker thread.
        """
        observers = self._observers

        def in_its_own_words(error: Exception) -> ToolError:
            return (
                error
                if isinstance(error, ToolError)
                else ToolError(str(error) or type(error).__name__)
            )

        if OffTheLoop.is_async(fn):

            @functools.wraps(fn)
            async def observed_async(*args: Any, **kwargs: Any) -> Any:
                with contextlib.ExitStack() as stack:
                    for observer in list(observers):
                        stack.enter_context(observer(tool))
                    try:
                        return await fn(*args, **kwargs)
                    except Exception as error:
                        raise in_its_own_words(error) from error

            return observed_async

        @functools.wraps(fn)
        def observed(*args: Any, **kwargs: Any) -> Any:
            with contextlib.ExitStack() as stack:
                for observer in list(observers):
                    stack.enter_context(observer(tool))
                try:
                    return fn(*args, **kwargs)
                except Exception as error:
                    raise in_its_own_words(error) from error

        return observed

    @contextlib.contextmanager
    def observing(self, observer: ToolObserver) -> Iterator[None]:
        """Tell `observer` of every tool call for as long as the block runs."""
        self._observers.append(observer)
        try:
            yield
        finally:
            self._observers.remove(observer)

    def tool_names(self, tool: str) -> ToolNames:
        """Every name `tool` goes by: in code, in the manifest, from a page, in igni.

        `KeyError` for a tool this extension does not register.
        """
        if tool not in self._hints:
            known = ", ".join(sorted(self._hints))
            raise KeyError(f"{self.id} registers no tool {tool!r} (it has: {known or 'none'})")
        return ToolNames.of(self.id, tool)

    def manifest_tools(self) -> list[dict[str, Any]]:
        """`permissions.tools` as the code states it: each tool's namespaced name and hints.

        What `igni-service-bundle` writes into the bundle's manifest when
        the extension's own leaves `permissions.tools` out, so the hints
        are stated once, here. `python -m igni_extension.manifest <module>`
        prints it. It lists the tools `ext.tool` registered, each of which
        states all four hints; a tool added on `ext.mcp` directly is
        declared in the manifest by hand (and `check_tools` still holds
        it to the four).
        """
        return [
            {
                "name": ToolNames.of(self.id, tool).manifest,
                "annotations": hints.wire(),
            }
            for tool, hints in sorted(self._hints.items())
        ]

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
        self._hint_when_outside_igni()
        anyio.run(self._run_stdio)

    def _hint_when_outside_igni(self) -> None:
        """Say what this is when somebody starts it from a terminal, rather than seem to hang.

        igni starts the process with a pipe on stdin; a terminal there
        means a person ran the console script. It still serves — an MCP
        inspector would — but says so first, on stderr.
        """
        try:
            interactive = sys.stdin.isatty()
        except (AttributeError, ValueError):
            return
        if not interactive or self.environment.host.configured:
            return
        print(
            f"{self.id}: this is an igni extension's process, an MCP server igni starts "
            "over stdio. Run inside igni: a development install (IGNI_DEV_EXTENSIONS=1 igni "
            "ext install <dir>), or its service's bundle. To try a tool without igni, "
            "test it: igni_extension.testing.HostHarness(ext).call(...). Waiting for MCP "
            "messages on stdin (Ctrl-C to stop).",
            file=sys.stderr,
        )

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
        # `MCPServer` has no public way to serve given streams yet; `mcp`'s
        # own in-memory transport reaches the same attribute
        # (`mcp.client._memory`, "TODO: Make `lowlevel_server` public").
        server = self.mcp._lowlevel_server
        await server.run(read_stream, write_stream, server.create_initialization_options())
