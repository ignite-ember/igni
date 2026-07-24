"""Composition root for the backend process.

Replaces the ~700-line ``_run`` function that used to live in
:mod:`ember_code.backend.__main__`. That function was one big
imperative script — every collaborator was a nested closure, every
piece of shared state was a captured local, and shutdown ordering
lived in a wall of ``with contextlib.suppress`` blocks. This module
turns the same lifecycle into two classes:

* :class:`TransportBuilder` — pick the transport shape (Unix, WS,
  or the composite of both) from the CLI flags.
* :class:`BackendApp` — the composition root. Its constructor
  wires every coordinator together; its :meth:`run` method walks
  the lifecycle in order and hands ``teardown`` to
  :class:`BackendSupervisor` in a ``finally`` block.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from ember_code.backend.login_coordinator import LoginCoordinator
from ember_code.backend.push_bridge import PushNotificationBridge
from ember_code.backend.rpc_router import RpcRouter
from ember_code.backend.schemas_rpc import BackendReadyLine
from ember_code.backend.server import BackendServer
from ember_code.backend.session_orchestrator import SessionOrchestrator
from ember_code.backend.supervisor import BackendSupervisor
from ember_code.core.config.settings import load_settings

logger = logging.getLogger(__name__)


class TransportBuilder:
    """Pick and build the right transport for the CLI flags.

    Both flags together = mirrored session: the TUI attaches over
    the Unix socket while GUI tabs attach over WS, and every view
    receives every broadcast (via the composite transport's fan-out).
    """

    def __init__(self, *, socket_path: str | None, ws_port: int | None) -> None:
        self._socket_path = socket_path
        self._ws_port = ws_port
        self._ws_transport: Any = None

    @property
    def ws_transport(self) -> Any:
        """The WS transport instance (if any). Exposed so the app
        can read the bound port for the ready line + lockfile."""
        return self._ws_transport

    async def build(self) -> Any:
        children: list[Any] = []
        if self._socket_path is not None:
            from ember_code.transport.unix_socket import UnixSocketServerTransport

            children.append(UnixSocketServerTransport(self._socket_path))
        if self._ws_port is not None:
            from ember_code.transport.websocket import WebSocketServerTransport

            self._ws_transport = WebSocketServerTransport(port=self._ws_port)
            children.append(self._ws_transport)

        if len(children) == 1:
            transport: Any = children[0]
        else:
            from ember_code.transport.websocket import CompositeTransport

            transport = CompositeTransport(children)
        await transport.start()
        return transport


class BackendApp:
    """The BE's composition root.

    Owns the whole lifecycle end-to-end: transport build → backend
    construct → collaborator wire-up → ready line → serve →
    shutdown. Every collaborator is stored as an instance attribute
    so tests can construct the app with dependency-injected
    replacements if needed.
    """

    def __init__(
        self,
        *,
        socket_path: str | None,
        ws_port: int | None,
        project_dir: Path,
        resume_session_id: str | None,
        additional_dirs: list[Path] | None,
    ) -> None:
        self._socket_path = socket_path
        self._ws_port = ws_port
        self._project_dir = project_dir
        self._resume_session_id = resume_session_id
        self._additional_dirs = additional_dirs

        self._transport_builder = TransportBuilder(socket_path=socket_path, ws_port=ws_port)
        # Wired in ``run`` — kept as attributes so shutdown/teardown
        # can access whatever the boot happened to build.
        self._transport: Any = None
        self._backend: BackendServer | None = None
        self._supervisor: BackendSupervisor | None = None
        self._push_bridge: PushNotificationBridge | None = None
        self._login: LoginCoordinator | None = None
        self._rpc_router: RpcRouter | None = None
        self._orchestrator: SessionOrchestrator | None = None
        self._queue: list[str] = []

    async def run(self) -> None:
        """Boot, serve, and tear down. Exceptions in the boot or
        serve phases are logged; teardown always runs."""
        self._transport = await self._transport_builder.build()

        settings = load_settings(project_dir=self._project_dir)
        self._backend = BackendServer(
            settings,
            project_dir=self._project_dir,
            resume_session_id=self._resume_session_id,
            additional_dirs=self._additional_dirs,
        )
        # Async post-construction wiring (hydrate any persisted
        # ``/loop`` state from the project's ``state.db``).
        await self._backend.startup()

        loop = asyncio.get_running_loop()
        self._supervisor = BackendSupervisor(
            transport=self._transport,
            loop=loop,
            project_dir=self._project_dir,
        )
        self._supervisor.set_backend_fallback(self._backend)
        self._supervisor.install_signal_handlers()
        self._supervisor.start_parent_watchdog()
        self._supervisor.start_transport_close_watcher()

        # Push bridge — the sink every callback/listener ends up
        # pushing through.
        self._push_bridge = PushNotificationBridge(
            transport=self._transport,
            loop=loop,
            queue=self._queue,
        )
        self._backend.wire_queue_hook(self._queue)
        self._push_bridge.wire_all(self._backend)

        self._login = LoginCoordinator(backend=self._backend, push_bridge=self._push_bridge)
        self._rpc_router = RpcRouter(
            backend=self._backend,
            transport=self._transport,
            login=self._login,
            push=self._push_bridge,
        )
        rpc_table = self._rpc_router.build_table()

        # Emit the "ready" line BEFORE background services fire —
        # the FE gates on this line and knowledge load is
        # GIL-heavy, so if we started it first the FE would think
        # we hung.
        self._emit_ready_line()

        # Background services now that the ready line is out.
        self._backend.start_boot_background_services()

        # Scheduler auto-start is idempotent (see
        # ``core.session.scheduler``); if it fails we log and
        # move on — the FE can retry via the ``start_scheduler``
        # RPC.
        try:
            self._push_bridge.start_scheduler(self._backend)
        except Exception:
            logger.exception("Auto-start of scheduler failed; will retry on client RPC")

        # Orchestrator: builds the pool, wires the default runtime,
        # registers itself as the receive-loop consumer.
        self._orchestrator = SessionOrchestrator(
            backend=self._backend,
            transport=self._transport,
            settings=settings,
            project_dir=self._project_dir,
            additional_dirs=self._additional_dirs,
            rpc_router=self._rpc_router,
            rpc_table=rpc_table,
            push_bridge=self._push_bridge,
            login=self._login,
            queue=self._queue,
        )
        pool = self._orchestrator.setup_pool()
        self._supervisor.set_pool(pool)
        self._supervisor.start_evictor()
        self._supervisor.mark_running()

        try:
            if self._ws_port is not None:
                # GUI shells (Tauri / VSCode / JetBrains webviews)
                # may open long after the BE is ready, and webviews
                # reload at will. No timeout here — shutdown comes
                # from signals, the parent watchdog, or an explicit
                # Shutdown message instead.
                await self._transport.wait_for_connection(timeout=None)
            else:
                await self._transport.wait_for_connection()
            logger.info("FE connected, processing messages")
            await self._orchestrator.serve(self._supervisor.shutdown_event)
        except Exception as exc:
            logger.error("Backend error: %s", exc, exc_info=True)
        finally:
            await self._supervisor.teardown()

    def _emit_ready_line(self) -> None:
        """Write the "ready" JSON line to stdout so the parent
        knows how to connect. Serialised via
        :class:`BackendReadyLine` so the wire shape is typed —
        ``model_dump_json(exclude_none=True)`` produces bytes
        byte-compatible with the old ``json.dumps(dict)`` output
        (verified by ``tests/test_backend_ready_line.py``)."""
        ws_transport = self._transport_builder.ws_transport
        ready = BackendReadyLine(status="ready")
        if ws_transport is not None:
            ready.ws_port = ws_transport.port
            ready.ws_url = f"ws://127.0.0.1:{ws_transport.port}"
            # Publish the port + version at
            # ``<project>/.ember/backend.lock`` so a second client
            # opening the same project can discover this BE.
            assert self._supervisor is not None
            self._supervisor.write_discovery_lockfile(ws_transport.port)
        if self._socket_path is not None:
            ready.socket = str(self._socket_path)
        # ``exclude_none`` matches the legacy shape: keys the caller
        # didn't set are omitted rather than serialised as null.
        payload = json.loads(ready.model_dump_json(exclude_none=True))
        print(json.dumps(payload), flush=True)
