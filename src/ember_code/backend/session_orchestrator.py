"""Session-pool orchestrator.

Owns everything about the "one BE, N live sessions" concern that
used to be welded into ``__main__._run``:

* the :class:`SessionPool` + its :class:`SessionRuntime` factory,
* the session-id → project-dir registry,
* the per-client UI state store,
* the pool-level RPCs (``attach_session``, ``{get,set,delete}_client_state``),
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
from pathlib import Path
from typing import Any

from ember_code.backend.login_coordinator import LoginCoordinator
from ember_code.backend.message_dispatcher import MessageDispatcher
from ember_code.backend.push_bridge import PushNotificationBridge
from ember_code.backend.rpc_router import POOL_LEVEL_RPCS, RpcRouter
from ember_code.backend.schemas_rpc import (
    AttachSessionResult,
    GetClientStateResult,
    SessionPoolConfig,
    WriteClientStateResult,
)
from ember_code.backend.session_pool import SessionPool, SessionRuntime
from ember_code.backend.session_stamping_transport import SessionStampingTransport
from ember_code.core.session.client_state import ClientStateStore
from ember_code.core.session.session_directories import SessionDirectoryStore
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
        queue: list[str],
    ) -> None:
        self._backend = backend
        self._transport = transport
        self._settings = settings
        self._project_dir = project_dir
        self._additional_dirs = additional_dirs
        self._push_bridge = push_bridge
        self._login = login
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

    # ── Setup ────────────────────────────────────────────────────

    def setup_pool(self) -> SessionPool:
        """Build the session pool + registries. Called once at boot
        after the default runtime's RPC table is ready."""
        self._dir_registry = SessionDirectoryStore.from_data_dir(self._settings.storage.data_dir)
        self._dir_registry.set_dir(self._backend.session_id, self._backend.project_dir)

        # Per-client UI state — survives reloads, shared across all
        # clients (web, JetBrains, VSCode) since each holds the same
        # opaque ``client_id`` and the BE is the source of truth.
        self._client_state = ClientStateStore.from_data_dir(self._settings.storage.data_dir)

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
        )
        # Attach runtime ref so the dispatcher's auto-name spawner
        # can park tasks on the runtime instead of a module global.
        self._backend.attach_runtime(default_runtime)
        self._default_runtime = default_runtime

        config = SessionPoolConfig()
        idle_env = os.environ.get("EMBER_SESSION_IDLE_TIMEOUT", "")
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
    def pool(self) -> SessionPool:
        assert self._pool is not None, "setup_pool must be called first"
        return self._pool

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
        # Background services the boot runtime gets in
        # BackendApp.run — without these, a session locked to
        # another repo has no knowledge load and no codeindex
        # sync/watch for it (degraded vs "the TUI opened in that
        # repo").
        rt_backend.start_all_background_services()
        rt_queue: list[str] = []
        rt_backend.wire_queue_hook(rt_queue)
        stamped = SessionStampingTransport(self._transport, rt_backend)
        rt_bridge = self._push_bridge.for_transport(stamped)
        # Per-runtime RPC router. Login is anchored to the boot
        # bridge (there is one login flow per BE process), but the
        # rest of the router hangs off this runtime's backend +
        # stamped transport.
        rt_router = RpcRouter(
            backend=rt_backend,
            transport=stamped,
            login=self._login,
            push=rt_bridge,
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

    async def _handle_attach_session(self, message: Any) -> None:
        """Bind/create a session, optionally in a specific project
        directory. Returns ``{session_id, project_dir}`` so the
        view can adopt the binding."""
        assert self._dir_registry is not None
        args = message.args or {}
        session_id = str(args.get("session_id") or "")
        wanted_dir = str(args.get("project_dir") or "")
        if wanted_dir:
            wd = Path(wanted_dir).expanduser()
            if not wd.is_dir():
                await self._transport.send(
                    msg.RPCResponse(id=message.id or "", error=f"not a directory: {wanted_dir}")
                )
                return
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
                await self._transport.send(
                    msg.RPCResponse(
                        id=message.id or "",
                        error=(
                            f"session {session_id} belongs to a different "
                            f"project ({existing_dir}); this backend is "
                            f"bound to {self._project_dir}"
                        ),
                    )
                )
                return
        if not session_id:
            session_id = str(uuid.uuid4())[:8]
        assert self._pool is not None
        rt = await self._pool.get_or_create(session_id)
        result = AttachSessionResult(
            session_id=rt.backend.session_id,
            project_dir=str(rt.backend.project_dir),
        )
        await self._transport.send(msg.RPCResponse(id=message.id or "", result=result.model_dump()))

    async def _handle_client_state_rpc(self, message: Any) -> None:
        """Read/write per-client UI state. Not tied to any session
        — clients only need a stable ``client_id`` to round-trip
        their preferences."""
        assert self._client_state is not None
        args = message.args or {}
        client_id = str(args.get("client_id") or "").strip()
        method = message.method
        try:
            if method == RpcMethod.GET_CLIENT_STATE:
                snapshot = self._client_state.get_for_client(client_id)
                payload: dict[str, Any] = GetClientStateResult(state=snapshot).model_dump()["state"]
                await self._transport.send(msg.RPCResponse(id=message.id or "", result=payload))
                return
            if method == RpcMethod.SET_CLIENT_STATE:
                self._client_state.set_value(
                    client_id, str(args.get("key") or ""), str(args.get("value") or "")
                )
            else:  # DELETE_CLIENT_STATE
                self._client_state.delete_value(client_id, str(args.get("key") or ""))
            ack = WriteClientStateResult(ok=True).model_dump()
            await self._transport.send(msg.RPCResponse(id=message.id or "", result=ack))
        except Exception as exc:
            await self._transport.send(
                msg.RPCResponse(id=message.id or "", error=f"client_state: {exc}")
            )

    # ── Message fan-out ──────────────────────────────────────────

    async def dispatch(self, message: Any) -> None:
        """Route one message to the right runtime + handler.

        Pool-level RPCs (``attach_session``, ``{get,set,delete}_client_state``)
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
                else:
                    await self._handle_client_state_rpc(message)
                return
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
        if rt is self._default_runtime:
            await self._default_dispatcher.dispatch(message)
            return
        dispatcher = MessageDispatcher(
            backend=rt.backend,
            transport=rt.transport,
            rpc_table=rt.rpc_table,
            queue=rt.queue,
            login=self._login,
        )
        await dispatcher.dispatch(message)

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
            task = asyncio.create_task(self.dispatch(message))
            self._in_flight.add(task)
            task.add_done_callback(self._in_flight.discard)

        # Drain in-flight tasks before shutting down so we don't
        # drop mid-stream messages on graceful exit.
        if self._in_flight:
            await asyncio.gather(*self._in_flight, return_exceptions=True)
