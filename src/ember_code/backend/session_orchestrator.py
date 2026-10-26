"""Session-pool orchestrator.

Owns everything about the "one BE, N live sessions" concern that
used to be welded into ``__main__._run``:

* the :class:`SessionPool` + its :class:`SessionRuntime` factory,
* the session-id → project-dir registry,
* the per-client UI state store,
* the pool-level RPCs (``attach_session``, ``host_model``, ``window_model``),
* the ``_dispatch`` fan-out that picks the right runtime for each
  message,
* the in-flight task set for graceful drain on shutdown.

Every free function that used to take ``backend`` / ``transport`` /
``login_state`` as its first arg is now a bound method on this
class or on :class:`MessageDispatcher`, and every closure lifetime
is anchored to a concrete instance attribute.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from collections.abc import MutableSequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ember_code.backend.login_coordinator import LoginCoordinator
from ember_code.backend.message_dispatcher import MessageDispatcher
from ember_code.backend.push_bridge import PushNotificationBridge
from ember_code.backend.rpc_router import POOL_LEVEL_RPCS, RpcRouter
from ember_code.backend.schemas_extensions import PageTokenReply
from ember_code.backend.schemas_rpc import (
    AttachSessionResult,
    SessionPoolConfig,
)
from ember_code.backend.session_pool import SessionPool, SessionRuntime
from ember_code.backend.session_stamping_transport import SessionStampingTransport
from ember_code.core.activity import background
from ember_code.core.db.locator import StorageLocator
from ember_code.core.session.client_state import ClientStateStore
from ember_code.core.session.session_directories import SessionDirectoryStore
from ember_code.extensions.host.windows import WindowModelError, WindowRequest
from ember_code.extensions.registry import ExtensionRegistry
from ember_code.extensions.rpc import ExtensionRpc
from ember_code.extensions.service_token import (
    NoService,
    NotSignedIn,
    PageServiceTokens,
    TokenExchangeFailed,
)
from ember_code.protocol import messages as msg
from ember_code.protocol.rpc import RpcMethod

logger = logging.getLogger(__name__)


class SessionOrchestrator:
    """Pool-level dispatch + runtime factory + shutdown drain.

    Constructed once by :class:`BackendApp` after the boot
    :class:`BackendServer` is up. Owns instance attributes for
    every piece of state the old ``_run`` closure captured — no
    module-level mutable state remains.
    """

    def __init__(
        self,
        *,
        backend: Any,
        transport: Any,
        settings: Any,
        project_dir: Path,
        additional_dirs: list[Path] | None,
        rpc_router: RpcRouter,
        rpc_table: dict[str, Any],
        push_bridge: PushNotificationBridge,
        login: LoginCoordinator,
        extensions: ExtensionRpc | None = None,
        origins: Any = None,
        notifications: Any = None,
        queue: MutableSequence[str],
    ) -> None:
        self._backend = backend
        self._transport = transport
        self._settings = settings
        self._project_dir = project_dir
        self._additional_dirs = additional_dirs
        self._push_bridge = push_bridge
        self._login = login
        # The extension namespace, shared by every dispatcher this
        # orchestrator builds and by the boot RPC table that
        # :class:`~ember_code.backend.app.BackendApp` built before
        # this existed. One registry per backend, not one per
        # session: an extension is installed into a deployment, and a
        # second session must not see a different set of them.
        #
        # Defaulted rather than required because several tests and
        # the legacy ``_handle_message`` path construct an
        # orchestrator directly; without extensions those sessions
        # simply have none, which is what they had before.
        self._extensions = (
            extensions if extensions is not None else ExtensionRpc(ExtensionRegistry())
        )
        self._origins = origins
        # The floor's notification center, handed to every per-runtime
        # router so each session answers ``notifications_*`` from the
        # same one.
        self._notifications = notifications
        self._queue = queue
        self._boot_rpc_table = rpc_table
        # Constructed in ``setup_pool``. Kept on self so
        # ``spawn_auto_name`` etc. can reach them.
        self._pool: SessionPool | None = None
        self._default_runtime: SessionRuntime | None = None
        self._default_dispatcher: MessageDispatcher | None = None
        self._dir_registry: SessionDirectoryStore | None = None
        self._client_state: ClientStateStore | None = None
        # Task bookkeeping — replaces the module-global
        # ``_AUTO_NAME_TASKS`` set. Kept as instance attributes so
        # each orchestrator has its own lifetime.
        self._in_flight: set[asyncio.Task] = set()
        # The host object model, once :class:`BackendApp` builds it,
        # and whoever wants to see what windows send.
        self._host_model: Any = None
        self._page_tokens: PageServiceTokens | None = None
        self._inbound_watchers: list[Any] = []

    @property
    def extensions(self) -> ExtensionRpc:
        """The extension namespace, for whoever installs into it.

        Exposed because the installer is constructed elsewhere and
        has to reach the same registry every dispatcher reads. A
        second registry would authorise against one set of manifests
        while the router served another.
        """
        return self._extensions

    # ── Setup ────────────────────────────────────────────────────

    def setup_pool(self) -> SessionPool:
        """Build the session pool + registries. Called once at boot
        after the default runtime's RPC table is ready."""
        storage = StorageLocator.from_settings(self._settings)
        self._dir_registry = SessionDirectoryStore.from_locator(storage)
        self._dir_registry.set_dir(self._backend.session_id, self._backend.project_dir)

        # Per-client UI state — survives reloads, shared across all
        # clients (web, JetBrains, VSCode) since each holds the same
        # opaque ``client_id`` and the BE is the source of truth.
        self._client_state = ClientStateStore.from_locator(storage)

        default_runtime = SessionRuntime(
            backend=self._backend,
            rpc_table=self._boot_rpc_table,
            queue=self._queue,
            transport=SessionStampingTransport(self._transport, self._backend),
        )
        # Point the default dispatcher at the stamped transport so
        # broadcasts on the default runtime also get a session_id.
        self._default_dispatcher = MessageDispatcher(
            backend=self._backend,
            transport=default_runtime.transport,
            rpc_table=default_runtime.rpc_table,
            queue=default_runtime.queue,
            login=self._login,
            extensions=self._extensions,
        )
        # Attach runtime ref so the dispatcher's auto-name spawner
        # can park tasks on the runtime instead of a module global.
        self._backend.attach_runtime(default_runtime)
        self._default_runtime = default_runtime

        config = SessionPoolConfig()
        idle_env = os.environ.get("IGNI_SESSION_IDLE_TIMEOUT", "")
        try:
            secs = float(idle_env) if idle_env else 0.0
        except ValueError:
            secs = 0.0
        if secs > 0:
            config.idle_timeout_seconds = secs
        self._pool = SessionPool(
            default_runtime,
            self._create_runtime,
            **config.model_dump(exclude_none=True),
        )
        return self._pool

    @property
    def client_state(self) -> ClientStateStore:
        """The store every window's lasting model fields are kept in."""
        assert self._client_state is not None, "setup_pool must be called first"
        return self._client_state

    @property
    def pool(self) -> SessionPool:
        assert self._pool is not None, "setup_pool must be called first"
        return self._pool

    def refresh_agents(self) -> None:
        """Rebuild every live session's agents.

        What extensions do to the prompts and agents changed; a session
        built before it would otherwise keep the old ones until
        something else rebuilt it.
        """
        if self._pool is None:
            return
        for rt in self._pool.runtimes:
            rt.backend.reload_agents()

    # ── Runtime factory ──────────────────────────────────────────

    async def _create_runtime(self, session_id: str) -> SessionRuntime:
        from ember_code.backend.server import BackendServer

        assert self._dir_registry is not None
        # The session's own directory, if it ever had one; new or
        # unregistered sessions inherit the BE's boot directory.
        rt_dir = Path(self._dir_registry.get_dir(session_id) or self._project_dir)
        if not rt_dir.is_dir():
            logger.warning(
                "session %s dir %s missing; falling back to %s",
                session_id,
                rt_dir,
                self._project_dir,
            )
            rt_dir = self._project_dir
        # Deep-copy settings: resuming applies the session's
        # persisted model preference, which must not leak into
        # other runtimes' defaults.
        rt_settings = self._settings.model_copy(deep=True)
        rt_backend = BackendServer(
            rt_settings,
            project_dir=rt_dir,
            resume_session_id=session_id,
            additional_dirs=self._additional_dirs,
        )
        await rt_backend.startup()
        rt_queue = rt_backend.message_queue
        rt_backend.wire_queue_hook(rt_queue)
        stamped = SessionStampingTransport(self._transport, rt_backend)
        # The runtime's own queue, not the boot bridge's — anything a
        # bridge appends is injected as a synthetic user message in
        # the session it belongs to.
        rt_bridge = self._push_bridge.for_transport(stamped, queue=rt_queue)
        # Per-runtime RPC router. Login is anchored to the boot
        # bridge (there is one login flow per BE process), but the
        # rest of the router hangs off this runtime's backend +
        # stamped transport.
        rt_router = RpcRouter(
            backend=rt_backend,
            transport=stamped,
            login=self._login,
            push=rt_bridge,
            extensions=self._extensions,
            origins=self._origins,
            notifications=self._notifications,
        )
        rt_table = rt_router.build_table()
        self._dir_registry.set_dir(rt_backend.session_id, rt_backend.project_dir)
        # Wire pool-session broadcasts (plan_submitted,
        # permission_mode_changed, …) through the stamped
        # transport so they reach the FE with the right session_id.
        rt_bridge.bind_to_broadcast_bus(rt_backend)
        runtime = SessionRuntime(
            backend=rt_backend,
            rpc_table=rt_table,
            queue=rt_queue,
            transport=stamped,
        )
        rt_backend.attach_runtime(runtime)
        return runtime

    # ── Pool-level RPCs ──────────────────────────────────────────

    async def attach(self, session_id: str, project_dir: str | None) -> AttachSessionResult:
        """Bind/create a session, optionally in a specific project directory.

        The check a window's attach passes, in one place, so that the
        host model's ``host.sessions`` is refused exactly where a window
        would be. Raises :class:`ValueError` with the reason.
        """
        assert self._dir_registry is not None
        session_id = str(session_id or "")
        wanted_dir = str(project_dir or "")
        if wanted_dir:
            wd = Path(wanted_dir).expanduser()
            if not wd.is_dir():
                raise ValueError(f"not a directory: {wanted_dir}")
            if not session_id:
                session_id = str(uuid.uuid4())[:8]
            # Register BEFORE creation so the factory picks the
            # directory up.
            self._dir_registry.set_dir(session_id, wd.resolve())
        elif session_id:
            # No explicit ``project_dir`` — the caller is restoring
            # a previously-bound session (typical: FE reconnecting
            # after a page reload). Refuse the attach if that
            # session belongs to a different project than this BE
            # was launched with. Without this check, a stale FE
            # session_id from a prior run in another repo silently
            # opens THAT repo's ``state.db`` inside the current BE,
            # and the sidebar starts listing sessions from the
            # wrong project.
            existing_dir = self._dir_registry.get_dir(session_id)
            if existing_dir and Path(existing_dir).resolve() != self._project_dir.resolve():
                raise ValueError(
                    f"session {session_id} belongs to a different "
                    f"project ({existing_dir}); this backend is "
                    f"bound to {self._project_dir}"
                )
        if not session_id:
            session_id = str(uuid.uuid4())[:8]
        assert self._pool is not None
        rt = await self._pool.get_or_create(session_id)
        # Attaching is not looking: a window says which session it
        # shows by writing its model's ``session``, and that write is
        # ``sessions.current_changed`` — once, since writing the same
        # session again on a reload changes nothing.
        return AttachSessionResult(
            session_id=rt.backend.session_id,
            project_dir=str(rt.backend.project_dir),
        )

    async def _handle_attach_session(self, message: Any) -> None:
        """``attach_session`` from a window: :meth:`attach`, answered.

        Returns ``{session_id, project_dir}`` so the view can adopt the
        binding."""
        args = message.args or {}
        try:
            result = await self.attach(
                str(args.get("session_id") or ""), str(args.get("project_dir") or "")
            )
        except ValueError as exc:
            await self._transport.send(msg.RPCResponse(id=message.id or "", error=str(exc)))
            return
        await self._transport.send(msg.RPCResponse(id=message.id or "", result=result.model_dump()))
        await self._show_waiting_approvals(result.session_id)

    async def _show_waiting_approvals(self, session_id: str) -> None:
        """Show an attaching window the session's approvals still waiting.

        After a reload the window has lost its dialog; after a backend
        restart the approvals were read back from the database. Either
        way the person sees the question again, and answering it
        resumes the run.
        """
        assert self._pool is not None
        rt = await self._pool.get_or_create(session_id)
        for paused in rt.backend.hitl.waiting_messages():
            await rt.transport.send(paused)
        # What the previous backend was continuing when it died goes on
        # now, where a window can see it.
        async for proto in rt.backend.hitl.resume_interrupted():
            await rt.transport.send(proto)

    async def _handle_window_model(self, message: Any) -> None:
        """``window_model``: a window reading or writing its own model.

        Not tied to any session — a window spans them. The host model
        owns every window's model (:class:`~ember_code.extensions.host.
        windows.Windows`); this only carries the request there and the
        answer back. A refused write is an RPC error, so the window
        drops its optimistic copy and shows the model's.
        """
        if self._host_model is None:
            await self._transport.send(
                msg.RPCResponse(id=message.id or "", error="window_model: no host model")
            )
            return
        try:
            request = WindowRequest.model_validate(dict(message.args or {}))
            result = self._host_model.handle_window(request)
        except (ValidationError, WindowModelError) as exc:
            await self._transport.send(
                msg.RPCResponse(id=message.id or "", error=f"window_model: {exc}")
            )
            return
        await self._transport.send(msg.RPCResponse(id=message.id or "", result=result))

    async def _handle_host_model(self, message: Any) -> None:
        """``host_model``: an extension's page reaching the host object model.

        The bridge forwards ``bridge.host.…`` here with the frame's
        extension as ``caller`` — set by the host side of the bridge,
        never by the frame. The answer is the model's reply, success or
        refusal, as the result: the bridge needs the refusal's code,
        which an RPC error string would lose.
        """
        caller = getattr(message, "caller", None)
        if self._host_model is None or not caller:
            await self._transport.send(
                msg.RPCResponse(
                    id=message.id or "",
                    error="host_model: only an extension's page reaches the host model",
                )
            )
            return
        reply = await self._host_model.handle_page(caller, dict(message.args or {}))
        await self._transport.send(msg.RPCResponse(id=message.id or "", result=reply))

    async def _handle_service_token(self, message: Any) -> None:
        """``extension_service_token``: a page's token for its own service.

        ``bridge.service`` asks; igni's window forwards it with the
        frame's extension as ``caller``. Answered with ``{ok,
        access_token, expires_in, service_url}`` or ``{ok: false,
        error}`` as the result — a page treats "nobody is signed in"
        as a state, not a broken call. The audience is that
        extension's own service and nothing it asked for.
        """
        caller = getattr(message, "caller", None)
        tokens = self._page_tokens
        if tokens is None or not caller:
            await self._transport.send(
                msg.RPCResponse(
                    id=message.id or "",
                    error="extension_service_token: only an extension's page asks for one",
                )
            )
            return
        try:
            reply = await tokens.for_page(caller)
        except (NoService, NotSignedIn, TokenExchangeFailed) as exc:
            await self._transport.send(
                msg.RPCResponse(
                    id=message.id or "",
                    result=PageTokenReply(ok=False, error=str(exc)).model_dump(),
                )
            )
            return
        await self._transport.send(
            msg.RPCResponse(
                id=message.id or "",
                result=PageTokenReply(ok=True, **reply.model_dump()).model_dump(),
            )
        )

    def attach_page_tokens(self, tokens: PageServiceTokens | None) -> None:
        """Where an extension page's ``bridge.service`` token comes from."""
        self._page_tokens = tokens

    # ── Serving the host object model ────────────────────────────

    def attach_host_model(self, host_model: Any) -> None:
        """The :class:`~ember_code.extensions.host.model.HostModel` to tell
        about windows and to answer ``host_model`` with."""
        self._host_model = host_model

    def watch_inbound(self, watcher: Any) -> None:
        """Be shown every message a window sends that a runtime handles."""
        self._inbound_watchers.append(watcher)

    def _start_dispatch(self, work: Any) -> asyncio.Future[Any]:
        """A message's handling, in the background.

        Not a busy lease of its own: a message that starts a run is the
        session's to account for — running, or waiting on a person, which
        must not count as busy — and anything else it does is short.
        """
        task = background.daemon(work, name="dispatch")
        self._in_flight.add(task)
        task.add_done_callback(self._in_flight.discard)
        return task

    def spawn_dispatch(self, message: Any) -> asyncio.Task:
        """Dispatch a message as a window's would be, in the background."""
        task = self._start_dispatch(self.dispatch(message))
        return task

    async def post_from_extension(
        self,
        *,
        session_id: str,
        extension_id: str,
        text: str,
        media: dict[str, str] | None,
        queued: bool,
    ) -> None:
        """A message in a session's chat, labelled as an extension's.

        The agent answers it as it answers the person — the run streams
        to every window — but nothing about it says the person wrote
        it: every window labels it as the extension's, and the agent is
        told who sent it.
        """
        assert self._pool is not None
        rt = await self._pool.get_or_create(session_id)
        dispatcher = self._dispatcher_for(rt)
        if queued:
            await dispatcher.queue_from_extension(extension_id, text)
            return
        self._start_dispatch(dispatcher.post_from_extension(extension_id, text, media or {}))

    def _dispatcher_for(self, rt: SessionRuntime) -> MessageDispatcher:
        if rt is self._default_runtime and self._default_dispatcher is not None:
            return self._default_dispatcher
        return MessageDispatcher(
            backend=rt.backend,
            transport=rt.transport,
            rpc_table=rt.rpc_table,
            queue=rt.queue,
            login=self._login,
            extensions=self._extensions,
        )

    # ── Message fan-out ──────────────────────────────────────────

    async def dispatch(self, message: Any) -> None:
        """Route one message to the right runtime + handler.

        Pool-level RPCs (``attach_session``, ``host_model``, ``window_model``)
        bypass per-runtime routing; ``SessionList`` is forced onto
        the default runtime (see the long-form justification in the
        original ``__main__._dispatch``); everything else routes by
        the message's ``session_id`` stamp.
        """
        assert self._pool is not None
        assert self._dir_registry is not None
        try:
            if isinstance(message, msg.RPCRequest) and message.method in POOL_LEVEL_RPCS:
                if message.method == RpcMethod.ATTACH_SESSION:
                    await self._handle_attach_session(message)
                elif message.method == RpcMethod.HOST_MODEL:
                    await self._handle_host_model(message)
                elif message.method == RpcMethod.EXTENSION_SERVICE_TOKEN:
                    await self._handle_service_token(message)
                else:
                    await self._handle_window_model(message)
                return
            for watcher in self._inbound_watchers:
                watcher(message)
            # ``SessionList`` is a project-scoped query — force it
            # onto the boot runtime.
            if isinstance(message, msg.SessionList):
                await self._default_dispatcher.dispatch(message)
                return
            rt = await self._pool.get_or_create(message.session_id or "")
            rt.register_id()
            await self._dispatch_to_runtime(rt, message)
            rt.register_id()
            # Dedupe the dir-registry write — otherwise every single
            # dispatched message opens a SQLite connection. The
            # compare-and-swap lives on the runtime now (typed
            # field + method), no more monkey-patching a private
            # attribute onto it from here.
            if rt.record_dir_registered(rt.current_session_id(), rt.backend.project_dir):
                self._dir_registry.set_dir(rt.backend.session_id, rt.backend.project_dir)
        except Exception as exc:
            logger.error("message handler crashed: %s", exc, exc_info=True)
            with contextlib.suppress(Exception):
                await self._transport.send(
                    msg.Error(
                        id=message.id or "",
                        session_id=message.session_id or "",
                        text=f"session routing failed: {exc}",
                    )
                )

    async def _dispatch_to_runtime(self, rt: SessionRuntime, message: Any) -> None:
        # Default runtime already has a dispatcher; other runtimes
        # get a fresh one per call (cheap — no state carried over).
        await self._dispatcher_for(rt).dispatch(message)

    # ── Serve loop ───────────────────────────────────────────────

    async def serve(self, shutdown_event: asyncio.Event) -> None:
        """Read from the transport until shutdown, dispatching each
        message as its own task so long streaming handlers don't
        block later RPCs."""
        assert self._pool is not None
        async for message in self._transport.receive():
            if isinstance(message, msg.Shutdown):
                break
            if shutdown_event.is_set():
                break
            self._start_dispatch(self.dispatch(message))

        # Drain in-flight tasks before shutting down so we don't
        # drop mid-stream messages on graceful exit.
        if self._in_flight:
            await asyncio.gather(*self._in_flight, return_exceptions=True)
