"""Backend process entry point.

Usage: python -m ember_code.backend --socket /tmp/ember-code/<uuid>.sock

Starts a BackendServer, listens on the given Unix socket, and
processes FE messages until shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from pathlib import Path
from typing import Any

import click

logger = logging.getLogger(__name__)


async def _watch_parent(parent_pid: int, shutdown_event: asyncio.Event) -> None:
    """Self-terminate if the FE parent process dies.

    Signals are the primary cleanup path (FE's process_manager kills the
    BE process group on exit), but signals can be missed on hard crashes
    or hangups. This watchdog is the belt to that suspenders — without
    it we ended up with an 11-day-old runaway BE that had burned 117
    hours of CPU after the FE died unexpectedly.
    """
    if parent_pid <= 0:
        return
    while not shutdown_event.is_set():
        try:
            os.kill(parent_pid, 0)  # signal 0 = liveness probe only
        except ProcessLookupError:
            logger.warning("Parent FE (pid=%s) died; BE shutting down", parent_pid)
            shutdown_event.set()
            # Nudge the receive loop: SIGTERM so ``transport.receive()``
            # unblocks even if no more FE messages arrive.
            with contextlib.suppress(ProcessLookupError):
                os.kill(os.getpid(), signal.SIGTERM)
            # Last-resort escape hatch: if graceful shutdown stalls,
            # hard-exit after a grace period so we never linger as a
            # zombie burning CPU.
            await asyncio.sleep(5)
            logger.error("BE failed to shut down 5s after parent death; forcing exit")
            os._exit(1)
        except PermissionError:
            # PID exists but isn't ours — treat as alive.
            pass
        await asyncio.sleep(2)


@click.command()
@click.option("--socket", "socket_path", default=None, help="Unix socket path")
@click.option(
    "--ws-port",
    "ws_port",
    type=int,
    default=None,
    help="Listen on a loopback WebSocket port instead of a Unix socket "
    "(0 = auto-assign; the bound port is printed in the ready line). "
    "Used by GUI clients (Tauri / VSCode / JetBrains webviews).",
)
@click.option("--project-dir", type=click.Path(exists=True), default=".")
@click.option("--resume-session", "resume_session_id", default=None)
@click.option("--additional-dirs", multiple=True, default=())
@click.option("--debug", is_flag=True, default=False)
def main(
    socket_path: str | None,
    ws_port: int | None,
    project_dir: str,
    resume_session_id: str | None,
    additional_dirs: tuple[str, ...],
    debug: bool,
) -> None:
    """Start the Ember Code backend server."""
    if socket_path is None and ws_port is None:
        raise click.UsageError("at least one of --socket or --ws-port is required")
    if debug:
        log_path = Path.home() / ".ember" / "debug.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            filename=str(log_path),
            level=logging.DEBUG,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
            force=True,
        )
        logging.getLogger("ember_code").setLevel(logging.DEBUG)

    extra_dirs = [Path(d) for d in additional_dirs] if additional_dirs else None
    asyncio.run(_run(socket_path, Path(project_dir), resume_session_id, extra_dirs, ws_port))


async def _check_update() -> dict | None:
    try:
        from ember_code.core.utils.update_checker import check_for_update

        info = await check_for_update()
        if info and info.available:
            from ember_code.core.utils.update_checker import _PKG_NAME

            return {
                "available": True,
                "current_version": info.current_version,
                "latest_version": info.latest_version,
                "download_url": info.download_url,
                "pkg_name": _PKG_NAME,
            }
    except Exception:
        pass
    return None


# ── RPC dispatch table ──────────────────────────────────────────────


def _build_rpc_table(backend: Any, transport: Any, login_state: dict[str, Any]) -> dict[str, Any]:
    """Build method dispatch for RPCRequest messages."""
    from ember_code.protocol import messages as msg

    async def _login(args: dict) -> dict:
        # Cancel any previous login attempt
        old = login_state.get("task")
        if old and not old.done():
            old.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await old

        async def _on_status(text: str) -> None:
            await transport.send(
                msg.PushNotification(channel="login_status", payload={"text": text})
            )

        async def _do_login() -> None:
            try:
                success, result = await backend.login(on_status=_on_status)
                if success:
                    backend.reload_cloud_credentials()
                await transport.send(
                    msg.PushNotification(
                        channel="login_result",
                        payload={"success": success, "result": result},
                    )
                )
            except asyncio.CancelledError:
                pass  # cancelled by CancelLogin — no result to push
            except Exception as exc:
                await transport.send(
                    msg.PushNotification(
                        channel="login_result",
                        payload={"success": False, "result": str(exc)},
                    )
                )

        login_state["task"] = asyncio.create_task(_do_login())
        return {"started": True}

    async def _get_skill_definitions(args: dict) -> list[dict]:
        pool = backend.get_skill_pool()
        return [
            {"name": s.name, "description": s.description, "prompt": getattr(s, "prompt", "")}
            for s in pool.list_skills()
        ]

    # ── GUI-client parity helpers ─────────────────────────────────
    # The TUI does both of these in its own (FE) process; webview
    # clients have no filesystem or shell access, so the BE provides
    # them over RPC. Loopback-only by transport design.

    _file_index_cache: dict[str, Any] = {}

    async def _complete_files(args: dict) -> list[str]:
        from ember_code.core.utils.file_index import FileIndex

        idx = _file_index_cache.get("idx")
        if idx is None:
            idx = FileIndex(backend.project_dir)
            _file_index_cache["idx"] = idx
        await idx.ensure_loaded()
        return idx.match(str(args.get("query", "")), limit=int(args.get("limit", 50)))

    async def _list_dirs(args: dict) -> dict:
        """Subdirectory listing for the GUI folder browser.

        Same trust level as ``run_shell`` (local user over loopback).
        Dot-dirs are filtered unless ``show_hidden`` — the browser is
        for picking project roots, not spelunking.
        """

        def _scan() -> dict:
            raw = str(args.get("path") or Path.home())
            show_hidden = bool(args.get("show_hidden", False))
            base = Path(raw).expanduser()
            try:
                base = base.resolve()
                dirs = sorted(
                    (
                        p.name
                        for p in base.iterdir()
                        if p.is_dir() and (show_hidden or not p.name.startswith("."))
                    ),
                    key=str.lower,
                )
            except (OSError, PermissionError) as exc:
                return {
                    "path": str(base),
                    "parent": str(base.parent),
                    "dirs": [],
                    "home": str(Path.home()),
                    "error": str(exc),
                }
            return {
                "path": str(base),
                "parent": str(base.parent) if base != base.parent else "",
                "dirs": dirs,
                "home": str(Path.home()),
                "error": "",
            }

        return await asyncio.to_thread(_scan)

    async def _run_shell(args: dict) -> dict:
        """$-prefix shell mode. Captured (non-interactive) by design —
        parity with the TUI's inline shell for the common cases."""
        command = str(args.get("command", "")).strip()
        if not command:
            return {"output": "", "exit_code": 0}
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(backend.project_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            return {"output": "(timed out after 120s)", "exit_code": -1}
        return {
            "output": out.decode(errors="replace")[-100_000:],
            "exit_code": proc.returncode,
        }

    from ember_code.protocol.rpc import RpcMethod, validate_rpc_table

    table: dict[str, Any] = {
        # ── MCP ────────────────────────────────────────────────────
        RpcMethod.ENSURE_MCP: lambda args: backend.ensure_mcp(),
        RpcMethod.MCP_CONNECT: lambda args: backend.mcp_connect(args["server_name"]),
        RpcMethod.MCP_DISCONNECT: lambda args: backend.mcp_disconnect(args["server_name"]),
        RpcMethod.GET_MCP_STATUS: lambda args: backend.get_mcp_status(),
        RpcMethod.GET_MCP_SERVER_DETAILS: lambda args: backend.get_mcp_server_details(),
        RpcMethod.GET_MCP_SERVERS: lambda args: backend.get_mcp_servers(),
        # ── Compaction / learning ─────────────────────────────────
        RpcMethod.COMPACT_IF_NEEDED: lambda args: backend.compact_if_needed(
            args["ctx_tokens"], args["max_ctx"]
        ),
        RpcMethod.EXTRACT_LEARNINGS: lambda args: backend.extract_learnings(
            args["user_msg"], args["assistant_msg"]
        ),
        # ── /loop continuation ────────────────────────────────────
        RpcMethod.POP_PENDING_LOOP_ITERATION: lambda args: backend.pop_pending_loop_iteration(),
        RpcMethod.CANCEL_PENDING_LOOP: lambda args: backend.cancel_pending_loop(),
        RpcMethod.LOOP_STATUS: lambda args: backend.loop_status(),
        RpcMethod.LOOP_RESUME: lambda args: backend.loop_resume(),
        RpcMethod.LOOP_PAUSE: lambda args: backend.loop_pause(),
        # ── Auth ───────────────────────────────────────────────────
        RpcMethod.LOGIN: _login,
        RpcMethod.RELOAD_CLOUD_CREDENTIALS: lambda args: backend.reload_cloud_credentials(),
        RpcMethod.CLEAR_CLOUD_CREDENTIALS: lambda args: backend.clear_cloud_credentials(),
        # ── Hooks / knowledge ─────────────────────────────────────
        RpcMethod.FIRE_SESSION_START_HOOK: lambda args: backend.fire_session_start_hook(),
        RpcMethod.AUTO_SYNC_KNOWLEDGE: lambda args: backend.auto_sync_knowledge(),
        # ── Session / status ──────────────────────────────────────
        RpcMethod.SHUTDOWN: lambda args: backend.shutdown(),
        RpcMethod.GET_CHAT_HISTORY: lambda args: backend.get_chat_history(args["session_id"]),
        RpcMethod.GET_PENDING_MESSAGES: lambda args: backend.get_pending_messages(
            args["session_id"]
        ),
        RpcMethod.LIST_SESSIONS: lambda args: backend.list_sessions(),
        RpcMethod.SWITCH_SESSION: lambda args: backend.switch_session(args["session_id"]),
        RpcMethod.GET_PROCESSING: lambda args: backend.processing,
        RpcMethod.GET_SESSION_ID: lambda args: backend.session_id,
        RpcMethod.GET_RUN_TIMEOUT: lambda args: backend.run_timeout,
        RpcMethod.GET_STATUS: lambda args: backend.get_status(),
        RpcMethod.CANCEL_RUN: lambda args: backend.cancel_run(),
        # ── Scheduler ─────────────────────────────────────────────
        RpcMethod.EXECUTE_SCHEDULED_TASK: lambda args: backend.execute_scheduled_task(
            args["description"]
        ),
        RpcMethod.CANCEL_SCHEDULED_TASK: lambda args: backend.cancel_scheduled_task(
            args["task_id"]
        ),
        RpcMethod.GET_SCHEDULED_TASKS: lambda args: backend.get_scheduled_tasks(
            args.get("include_done", True)
        ),
        RpcMethod.START_SCHEDULER: lambda args: _start_scheduler_with_push(backend, transport),
        # ── Skills ─────────────────────────────────────────────────
        RpcMethod.GET_SKILL_NAMES: lambda args: backend.skill_names,
        RpcMethod.GET_SKILL_DEFINITIONS: _get_skill_definitions,
        # ── Models / config / permissions ─────────────────────────
        RpcMethod.SWITCH_MODEL: lambda args: backend.switch_model(args["model_name"]),
        RpcMethod.TOGGLE_VERBOSE: lambda args: backend.toggle_verbose(),
        RpcMethod.CHECK_PERMISSION: lambda args: backend.check_permission(
            args["tool_name"], args["func_name"], args["tool_args"]
        ),
        RpcMethod.SAVE_PERMISSION_RULE: lambda args: backend.save_permission_rule(
            args["rule"], args["level"]
        ),
        RpcMethod.GET_DISPLAY_CONFIG: lambda args: (
            backend.settings.display.model_dump()
            if hasattr(backend.settings.display, "model_dump")
            else {}
        ),
        RpcMethod.GET_MODEL_REGISTRY: lambda args: {
            "default": backend.settings.models.default,
            "max_context_window": backend.settings.models.max_context_window,
            "registry": dict(backend.settings.models.registry.items()),
        },
        RpcMethod.CHECK_FOR_UPDATE: lambda args: _check_update(),
        # ── GUI-client parity ─────────────────────────────────────
        RpcMethod.COMPLETE_FILES: _complete_files,
        RpcMethod.RUN_SHELL: _run_shell,
        RpcMethod.LIST_DIRS: _list_dirs,
        RpcMethod.GET_PROJECT_DIR: lambda args: str(backend.project_dir),
        # Pool-level method — intercepted in the dispatch loop before
        # per-runtime routing; this stub only exists so the
        # exhaustiveness check passes and a future regression (the
        # interception being removed) fails loudly.
        RpcMethod.ATTACH_SESSION: lambda args: (_ for _ in ()).throw(
            RuntimeError("attach_session must be handled at the session-pool level")
        ),
        # ── Agents ────────────────────────────────────────────────
        RpcMethod.GET_AGENT_DETAILS: lambda args: backend.get_agent_details(),
        RpcMethod.PROMOTE_EPHEMERAL_AGENT: lambda args: backend.promote_ephemeral_agent(
            args["name"]
        ),
        RpcMethod.DISCARD_EPHEMERAL_AGENT: lambda args: backend.discard_ephemeral_agent(
            args["name"]
        ),
        # ── Hooks ─────────────────────────────────────────────────
        RpcMethod.GET_HOOKS_DETAILS: lambda args: backend.get_hooks_details(),
        RpcMethod.RELOAD_HOOKS: lambda args: backend.reload_hooks_rpc(),
        # ── Skills ────────────────────────────────────────────────
        RpcMethod.GET_SKILL_DETAILS: lambda args: backend.get_skill_details(),
        # ── Knowledge ─────────────────────────────────────────────
        RpcMethod.GET_KNOWLEDGE_STATUS: lambda args: backend.get_knowledge_status(),
        RpcMethod.KNOWLEDGE_SEARCH: lambda args: backend.knowledge_search(args["query"]),
        RpcMethod.KNOWLEDGE_ADD: lambda args: backend.knowledge_add(args["source"]),
        # ── Conversation ──────────────────────────────────────────
        RpcMethod.COUNT_CONTEXT_TOKENS: lambda args: backend.count_context_tokens(),
        # ── CodeIndex ─────────────────────────────────────────────
        RpcMethod.CODEINDEX_STATUS: lambda args: backend.codeindex_status(),
        RpcMethod.CODEINDEX_SYNC: lambda args: backend.codeindex_sync(args.get("sha")),
        RpcMethod.CODEINDEX_RESYNC: lambda args: backend.codeindex_resync(args.get("sha")),
        RpcMethod.CODEINDEX_CLEAN: lambda args: backend.codeindex_clean(),
        RpcMethod.CODEINDEX_INSTALL: lambda args: backend.codeindex_install(),
        # ── Plugins ───────────────────────────────────────────────
        RpcMethod.GET_PLUGIN_DETAILS: lambda args: backend.get_plugin_details(),
        RpcMethod.SET_PLUGIN_ENABLED: lambda args: backend.set_plugin_enabled(
            args["name"], args["enabled"]
        ),
        RpcMethod.INSTALL_PLUGIN: lambda args: backend.install_plugin(
            args["ref"],
            args.get("install_ref"),
        ),
        RpcMethod.UPDATE_PLUGIN: lambda args: backend.update_plugin(
            args["name"],
            args.get("install_ref"),
        ),
        RpcMethod.REMOVE_PLUGIN: lambda args: backend.remove_plugin(args["name"]),
        RpcMethod.GET_MARKETPLACES: lambda args: backend.get_marketplaces(),
        RpcMethod.ADD_MARKETPLACE: lambda args: backend.add_marketplace(args["url"]),
        RpcMethod.REMOVE_MARKETPLACE: lambda args: backend.remove_marketplace(args["name"]),
        RpcMethod.REFRESH_MARKETPLACES: lambda args: backend.refresh_marketplaces(
            args.get("name"),
        ),
    }
    # Fail fast if any enum member is missing a handler. Catches the
    # "added a new RpcMethod, forgot to wire the dispatch entry" bug
    # before the FE ever issues a call.
    validate_rpc_table(table.keys())
    return table


def _start_scheduler_with_push(backend: Any, transport: Any) -> None:
    """Start the scheduler with push notification callbacks."""
    from ember_code.protocol import messages as msg

    def on_started(task_id: str, description: str) -> None:
        asyncio.ensure_future(
            transport.send(
                msg.PushNotification(
                    channel="scheduler_started",
                    payload={"task_id": task_id, "description": description},
                )
            )
        )

    def on_completed(task_id: str, description: str, result: str) -> None:
        asyncio.ensure_future(
            transport.send(
                msg.PushNotification(
                    channel="scheduler_completed",
                    payload={"task_id": task_id, "description": description, "result": result},
                )
            )
        )

    backend.start_scheduler(on_task_started=on_started, on_task_completed=on_completed)


# ── Main loop ──────────────────────────────────────────────────────


async def _run(
    socket_path: str | None,
    project_dir: Path,
    resume_session_id: str | None,
    additional_dirs: list[Path] | None = None,
    ws_port: int | None = None,
) -> None:
    from ember_code.backend.server import BackendServer
    from ember_code.core.config.settings import load_settings
    from ember_code.protocol import messages as msg

    settings = load_settings(project_dir=project_dir)

    # Transport selection. Both flags together = mirrored session:
    # the TUI attaches over the Unix socket while GUI tabs attach
    # over WS, and every view receives every broadcast.
    ws_transport = None
    children: list[Any] = []
    if socket_path is not None:
        from ember_code.transport.unix_socket import UnixSocketServerTransport

        children.append(UnixSocketServerTransport(socket_path))
    if ws_port is not None:
        from ember_code.transport.websocket import WebSocketServerTransport

        ws_transport = WebSocketServerTransport(port=ws_port)
        children.append(ws_transport)

    if len(children) == 1:
        transport: Any = children[0]
    else:
        from ember_code.transport.websocket import CompositeTransport

        transport = CompositeTransport(children)
    await transport.start()

    backend = BackendServer(
        settings,
        project_dir=project_dir,
        resume_session_id=resume_session_id,
        additional_dirs=additional_dirs,
    )
    # Async post-construction wiring (currently: hydrate any
    # persisted ``/loop`` state from the project's ``state.db``).
    await backend.startup()

    # Handle SIGTERM/SIGINT
    shutdown_event = asyncio.Event()

    def _signal_handler():
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    # Watchdog: exit if the FE parent dies. ``EMBER_PARENT_PID`` is
    # injected by ``process_manager.BackendProcess.start``.
    parent_pid_str = os.environ.get("EMBER_PARENT_PID", "0") or "0"
    try:
        parent_pid = int(parent_pid_str)
    except ValueError:
        parent_pid = 0
    parent_watch_task = asyncio.create_task(_watch_parent(parent_pid, shutdown_event))

    # SIGTERM/SIGINT only set ``shutdown_event`` — but the main loop
    # blocks in ``transport.receive()``. The Unix transport unblocks
    # when the dying FE closes the socket; the WS transport does NOT
    # (an idle webview keeps the connection open), which left the BE
    # unkillable-gracefully. Closing the transport on shutdown pushes
    # the close sentinel through ``receive()`` for both transports.
    async def _close_transport_on_shutdown() -> None:
        await shutdown_event.wait()
        with contextlib.suppress(Exception):
            await transport.close()

    shutdown_close_task = asyncio.create_task(_close_transport_on_shutdown())

    # Queue for message injection (replaces wire_queue_hook)
    _queue: list[str] = []
    backend.wire_queue_hook(_queue)

    # Background-process completion notifications. When the agent
    # backgrounds a long-running shell command (e.g. ``npm run build``)
    # and moves on, we push a formatted notice onto the same queue
    # used by user-typed messages — the QueueInjectorHook will splice
    # it into the next tool result so the agent reacts naturally
    # ("oh, the build I started 4 minutes ago just finished, exit
    # code 0; let me check the output"). FE also gets a
    # PushNotification so the user sees a status indicator.
    from ember_code.core.tools.shell import subscribe_to_process_completion

    def _on_process_done(info: dict) -> None:
        pid = info.get("pid")
        cmd = info.get("cmd", "")
        rc = info.get("exit_code")
        dur = info.get("duration_seconds", 0.0)
        tail = info.get("output_tail", "")
        status = "succeeded" if rc == 0 else f"failed (exit {rc})"
        # Include a one-line hint pointing at ``read_process_output``
        # so the agent knows it can pull more than the 40-line tail.
        # ``read_process_output`` is now idempotent — the agent can
        # call it repeatedly with different ``tail`` values for as
        # long as the BE is alive (output is held in a ~1MB-capped
        # in-memory buffer per process).
        hint = f"For more output: read_process_output({pid}, tail=N)"
        if tail:
            msg_text = (
                f"BACKGROUND PROCESS COMPLETED\n"
                f"PID {pid}: {cmd}\n"
                f"Status: {status}  ·  Duration: {dur:.1f}s\n\n"
                f"Last output (tail):\n{tail}\n\n{hint}"
            )
        else:
            msg_text = (
                f"BACKGROUND PROCESS COMPLETED\n"
                f"PID {pid}: {cmd}\n"
                f"Status: {status}  ·  Duration: {dur:.1f}s\n\n{hint}"
            )
        _queue.append(msg_text)
        # Best-effort UI ping. Schedule onto the loop because the shell
        # callback runs in the reader thread.
        with contextlib.suppress(Exception):
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(
                    transport.send(
                        msg.PushNotification(
                            channel="background_process_done",
                            payload={
                                "pid": pid,
                                "cmd": cmd,
                                "exit_code": rc,
                                "duration_seconds": dur,
                            },
                        )
                    )
                )
            )

    subscribe_to_process_completion(_on_process_done)

    # Orchestrate progress → push notification
    def _on_progress(line: str) -> None:
        asyncio.ensure_future(
            transport.send(
                msg.PushNotification(channel="orchestrate_progress", payload={"line": line})
            )
        )

    backend.wire_orchestrate_progress(_on_progress)

    login_state: dict[str, Any] = {"task": None}
    rpc_table = _build_rpc_table(backend, transport, login_state)

    # Signal ready AFTER init + wiring so the FE can immediately
    # send requests (e.g. refresh_cache).  Knowledge may still be
    # loading in a background thread — that's fine.
    import json as _json

    ready: dict[str, Any] = {"status": "ready"}
    if ws_transport is not None:
        # The actual bound port — meaningful when --ws-port 0 asked
        # for auto-assign. Shells parse this to point their webview.
        ready["ws_port"] = ws_transport.port
        ready["ws_url"] = f"ws://127.0.0.1:{ws_transport.port}"
    if socket_path is not None:
        ready["socket"] = str(socket_path)
    print(_json.dumps(ready), flush=True)

    # Start knowledge loading AFTER READY — model download is GIL-heavy
    # and would block the main thread if started during __init__.
    backend._session.start_knowledge_background()

    # Pull the latest code-index changeset for HEAD and watch for new commits.
    backend._session.start_codeindex_background()

    # Refresh plugin marketplace catalogs in the background. Failures
    # are logged but don't gate session readiness.
    backend._session.start_marketplace_refresh_background()

    try:
        if ws_port is not None:
            # GUI shells (Tauri / VSCode / JetBrains webviews) may open
            # long after the BE is ready, and webviews reload at will.
            # No timeout here — shutdown comes from signals, the parent
            # watchdog, or an explicit Shutdown message instead.
            await transport.wait_for_connection(timeout=None)
        else:
            await transport.wait_for_connection()
        logger.info("FE connected, processing messages")

        # Each FE message is dispatched as its own task so the main loop
        # can keep reading. Without this, a long-running streaming handler
        # (e.g. ``run_message`` while sub-agent is paused for HITL) blocks
        # later RPCs (e.g. ``check_permission``) from ever being read,
        # causing the FE to hang on the RPC future.
        # ``backend.run_message`` already rejects concurrent runs by
        # returning an Error early when ``_processing=True``, so racing
        # UserMessages degrades gracefully.
        in_flight: set[asyncio.Task] = set()

        # ── Session pool ──────────────────────────────────────────
        # Messages route by their ``session_id`` stamp: empty → the
        # default runtime (TUI behaviour, unchanged); a known id →
        # its live runtime; an unknown id → lazily resumed in its
        # own runtime. Different sessions run in parallel.
        from ember_code.backend.session_pool import (
            SessionPool,
            SessionRuntime,
            SessionStampingTransport,
        )
        from ember_code.core.session.session_directories import SessionDirectoryStore
        from ember_code.protocol.rpc import RpcMethod

        # Global session → project-dir registry: sessions can live in
        # different repos ("TUI opened in different directories", one
        # BE). Recorded on boot/rename, consulted on lazy resume.
        dir_registry = SessionDirectoryStore.from_data_dir(settings.storage.data_dir)
        dir_registry.set_dir(backend.session_id, backend.project_dir)

        default_runtime = SessionRuntime(
            backend=backend,
            rpc_table=rpc_table,
            queue=_queue,
            transport=SessionStampingTransport(transport, backend),
        )

        async def _create_runtime(session_id: str) -> SessionRuntime:
            from ember_code.backend.server import BackendServer

            # The session's own directory, if it ever had one; new or
            # unregistered sessions inherit the BE's boot directory.
            rt_dir = Path(dir_registry.get_dir(session_id) or project_dir)
            if not rt_dir.is_dir():
                logger.warning(
                    "session %s dir %s missing; falling back to %s",
                    session_id,
                    rt_dir,
                    project_dir,
                )
                rt_dir = project_dir
            # Deep-copy settings: resuming applies the session's
            # persisted model preference, which must not leak into
            # other runtimes' defaults.
            rt_settings = settings.model_copy(deep=True)
            rt_backend = BackendServer(
                rt_settings,
                project_dir=rt_dir,
                resume_session_id=session_id,
                additional_dirs=additional_dirs,
            )
            await rt_backend.startup()
            # Background services the boot runtime gets in _run —
            # without these, a session locked to another repo has no
            # knowledge load and no codeindex sync/watch for it
            # (degraded vs "the TUI opened in that repo").
            rt_backend._session.start_knowledge_background()
            rt_backend._session.start_codeindex_background()
            rt_queue: list[str] = []
            rt_backend.wire_queue_hook(rt_queue)
            stamped = SessionStampingTransport(transport, rt_backend)
            rt_table = _build_rpc_table(rt_backend, stamped, login_state)
            dir_registry.set_dir(rt_backend.session_id, rt_backend.project_dir)
            return SessionRuntime(
                backend=rt_backend,
                rpc_table=rt_table,
                queue=rt_queue,
                transport=stamped,
            )

        pool = SessionPool(default_runtime, _create_runtime)

        async def _attach_session(message: Any) -> None:
            """Pool-level RPC: bind/create a session, optionally in a
            specific project directory. Returns {session_id,
            project_dir} so the view can adopt the binding."""
            args = message.args or {}
            session_id = str(args.get("session_id") or "")
            wanted_dir = str(args.get("project_dir") or "")
            if wanted_dir:
                wd = Path(wanted_dir).expanduser()
                if not wd.is_dir():
                    await transport.send(
                        msg.RPCResponse(
                            id=message.id or "", error=f"not a directory: {wanted_dir}"
                        )
                    )
                    return
                if not session_id:
                    import uuid as _uuid

                    session_id = str(_uuid.uuid4())[:8]
                # Register BEFORE creation so the factory picks the
                # directory up.
                dir_registry.set_dir(session_id, wd.resolve())
            if not session_id:
                import uuid as _uuid

                session_id = str(_uuid.uuid4())[:8]
            rt = await pool.get_or_create(session_id)
            await transport.send(
                msg.RPCResponse(
                    id=message.id or "",
                    result={
                        "session_id": rt.backend.session_id,
                        "project_dir": str(rt.backend.project_dir),
                    },
                )
            )

        async def _dispatch(message: Any) -> None:
            try:
                # Pool-level RPCs bypass per-runtime routing.
                if (
                    isinstance(message, msg.RPCRequest)
                    and message.method == RpcMethod.ATTACH_SESSION
                ):
                    await _attach_session(message)
                    return
                rt = await pool.get_or_create(message.session_id or "")
                rt.remember_id()
                await _handle_message(
                    message, rt.backend, rt.transport, rt.rpc_table, rt.queue, login_state
                )
                # Pick up id renames (/clear) so views still stamping
                # the old id keep routing here, and keep the directory
                # registry current for the renewed id.
                rt.remember_id()
                dir_registry.set_dir(rt.backend.session_id, rt.backend.project_dir)
            except Exception as exc:
                logger.error("message handler crashed: %s", exc, exc_info=True)
                with contextlib.suppress(Exception):
                    await transport.send(
                        msg.Error(
                            id=message.id or "",
                            session_id=message.session_id or "",
                            text=f"session routing failed: {exc}",
                        )
                    )

        async for message in transport.receive():
            if isinstance(message, msg.Shutdown):
                break
            if shutdown_event.is_set():
                break
            task = asyncio.create_task(_dispatch(message))
            in_flight.add(task)
            task.add_done_callback(in_flight.discard)

        # Drain in-flight tasks before shutting down so we don't drop
        # mid-stream messages on graceful exit.
        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)

    except Exception as exc:
        logger.error("Backend error: %s", exc, exc_info=True)
    finally:
        parent_watch_task.cancel()
        shutdown_close_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await parent_watch_task
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await shutdown_close_task
        try:
            # Shut down every pooled runtime (includes the default).
            await pool.shutdown()  # noqa: F821 — defined in the try body
        except (NameError, UnboundLocalError):
            # Startup failed before the pool existed.
            await backend.shutdown()
        await transport.close()
        logger.info("Backend shut down")


async def _handle_message(
    message: Any,
    backend: Any,
    transport: Any,
    rpc_table: dict,
    queue: list[str],
    login_state: dict[str, Any] | None = None,
) -> None:
    from ember_code.protocol import messages as msg
    from ember_code.protocol.messages import Message

    req_id = message.id or ""

    # ── Streaming: run_message ──
    if isinstance(message, msg.UserMessage):
        # Mirroring: every attached view paints the user bubble; the
        # sender recognises its own client_id and skips the echo.
        await transport.send(
            msg.UserMessageReceived(text=message.text, client_id=message.client_id)
        )
        async for proto in backend.run_message(message.text, media=message.file_contents):
            if req_id:
                proto = proto.model_copy(update={"id": req_id})
            await transport.send(proto)
        await transport.send(msg.StreamEnd(id=req_id))

    # ── Streaming: resolve_hitl ──
    elif isinstance(message, msg.HITLResponse):
        # Mirroring: dismiss the now-stale permission dialog on every
        # other view before the resumed run starts streaming.
        await transport.send(msg.RequirementResolved(requirement_id=message.requirement_id))
        async for proto in backend.resolve_hitl(
            message.requirement_id, message.action, message.choice
        ):
            if req_id:
                proto = proto.model_copy(update={"id": req_id})
            await transport.send(proto)
        await transport.send(msg.StreamEnd(id=req_id))

    # ── Streaming: resolve_hitl_batch (multi-req pause resolution) ──
    elif isinstance(message, msg.HITLResponseBatch):
        for decision in message.decisions:
            await transport.send(msg.RequirementResolved(requirement_id=decision.requirement_id))
        async for proto in backend.resolve_hitl_batch(message.decisions):
            if req_id:
                proto = proto.model_copy(update={"id": req_id})
            await transport.send(proto)
        await transport.send(msg.StreamEnd(id=req_id))

    # ── Command ──
    elif isinstance(message, msg.Command):
        result = await backend.handle_command(message.text)
        result = result.model_copy(update={"id": req_id})
        await transport.send(result)

    # ── Session management (typed messages) ──
    elif isinstance(message, msg.SessionList):
        result = await backend.list_sessions()
        result = result.model_copy(update={"id": req_id})
        await transport.send(result)

    elif isinstance(message, msg.SessionSwitch):
        result = await backend.switch_session(message.session_id)
        result = result.model_copy(update={"id": req_id})
        await transport.send(result)

    elif isinstance(message, msg.ModelSwitch):
        result = backend.switch_model(message.model_name)
        result = result.model_copy(update={"id": req_id})
        await transport.send(result)

    elif isinstance(message, msg.MCPToggle):
        result = await backend.toggle_mcp(message.server_name, message.connect)
        result = result.model_copy(update={"id": req_id})
        await transport.send(result)

    # ── Queue injection ──
    elif isinstance(message, msg.QueueMessage):
        queue.append(message.text)
        await transport.send(
            msg.UserMessageReceived(text=message.text, client_id=message.client_id, queued=True)
        )

    # ── Mirroring: live composer drafts ──
    # Relayed to ALL views (sender included — it filters by
    # client_id). Pure fan-out: the BE adds nothing.
    elif isinstance(message, msg.Typing):
        await transport.send(message)

    # ── Cancel ──
    elif isinstance(message, msg.Cancel):
        backend.cancel_run()

    # ── Cancel login ──
    elif isinstance(message, msg.CancelLogin):
        if login_state:
            task = login_state.get("task")
            if task and not task.done():
                task.cancel()

    # ── Generic RPC ──
    elif isinstance(message, msg.RPCRequest):
        handler = rpc_table.get(message.method)
        if handler is None:
            await transport.send(
                msg.RPCResponse(id=req_id, error=f"Unknown RPC method: {message.method}")
            )
            return

        try:
            result = handler(message.args)
            if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                result = await result

            # If result is a Message, send it directly with correlation ID
            if isinstance(result, Message):
                result = result.model_copy(update={"id": req_id})
                await transport.send(result)
            else:
                # Wrap in RPCResponse for plain values
                await transport.send(msg.RPCResponse(id=req_id, result=_serialize(result)))
        except Exception as exc:
            logger.error("RPC %s failed: %s", message.method, exc, exc_info=True)
            await transport.send(msg.RPCResponse(id=req_id, error=str(exc)))

    else:
        logger.warning("Unknown FE message type: %s", type(message).__name__)


def _serialize(value: Any) -> Any:
    """Make a value JSON-serializable."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_serialize(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _serialize(v) for k, v in value.items()}
    # Pydantic models
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return str(value)


if __name__ == "__main__":
    main()
