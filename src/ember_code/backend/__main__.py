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

# Strong refs to fire-and-forget naming tasks (create_task results are
# weakly held by the loop and can be GC'd mid-flight otherwise).
_AUTO_NAME_TASKS: set[asyncio.Task[None]] = set()


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
    """Start the igni backend server."""
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
    # Canonicalise the project dir so two clients pointing at the
    # "same" folder via slightly different paths (``/tmp`` vs
    # ``/private/tmp`` on macOS, symlink resolution, trailing slash)
    # both land on the same ``.ember/state.db`` and see identical
    # session lists. ``strict=False`` lets us keep going if the
    # directory doesn't yet exist — startup will create it below.
    resolved_project = Path(project_dir).resolve(strict=False)
    asyncio.run(_run(socket_path, resolved_project, resume_session_id, extra_dirs, ws_port))


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

    async def _complete_files(args: dict) -> dict:
        from ember_code.core.utils.file_index import FileIndex

        idx = _file_index_cache.get("idx")
        if idx is None:
            idx = FileIndex(backend.project_dir)
            _file_index_cache["idx"] = idx
        await idx.ensure_loaded()
        query = str(args.get("query", ""))
        limit = int(args.get("limit", 50))
        matches, total = idx.match_with_total(query, limit=limit)
        return {"matches": matches, "total": total}

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

    async def _pick_dir_native(args: dict) -> dict:
        """Open the OS folder picker on this machine, return the path.

        The BE always runs on the user's machine (loopback-only
        transport), so the dialog appears on their desktop — even
        when the view is a plain browser tab that could never get a
        real path out of its own sandboxed file dialogs. No timeout:
        the user may take their time; the FE uses a long RPC timeout
        for this call.
        """
        import sys

        async def _run_cmd(cmd: list[str]) -> tuple[int | None, str]:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            return proc.returncode, out.decode(errors="replace").strip()

        # Start browsing at the session's CURRENTLY locked directory
        # (or an explicit override) — not at the OS default.
        start = Path(str(args.get("start") or backend.project_dir)).expanduser()
        start_dir = str(start) if start.is_dir() else ""

        if sys.platform == "darwin":
            script = 'choose folder with prompt "Lock session to a project folder"'
            if start_dir:
                escaped = start_dir.replace("\\", "\\\\").replace('"', '\\"')
                script += f' default location POSIX file "{escaped}"'
            rc, out = await _run_cmd(["osascript", "-e", f"POSIX path of ({script})"])
            if rc == 0 and out:
                return {"path": out.rstrip("/") or "/", "cancelled": False, "error": ""}
            # osascript exits non-zero on user cancel.
            return {"path": "", "cancelled": True, "error": ""}

        if sys.platform.startswith("linux"):
            zenity_cmd = ["zenity", "--file-selection", "--directory"]
            if start_dir:
                zenity_cmd.append(f"--filename={start_dir}/")
            kdialog_cmd = ["kdialog", "--getexistingdirectory"]
            if start_dir:
                kdialog_cmd.append(start_dir)
            for cmd in (zenity_cmd, kdialog_cmd):
                try:
                    rc, out = await _run_cmd(cmd)
                except FileNotFoundError:
                    continue
                if rc == 0 and out:
                    return {"path": out, "cancelled": False, "error": ""}
                return {"path": "", "cancelled": True, "error": ""}
            return {"path": "", "cancelled": False, "error": "no native dialog available"}

        if sys.platform == "win32":
            selected = (
                f"$d.SelectedPath = '{start_dir}'; " if start_dir and "'" not in start_dir else ""
            )
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
                f"{selected}"
                "if ($d.ShowDialog() -eq 'OK') { Write-Output $d.SelectedPath }"
            )
            rc, out = await _run_cmd(["powershell", "-NoProfile", "-Command", ps])
            if rc == 0 and out:
                return {"path": out, "cancelled": False, "error": ""}
            return {"path": "", "cancelled": True, "error": ""}

        return {"path": "", "cancelled": False, "error": f"unsupported platform: {sys.platform}"}

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
        RpcMethod.SET_MCP_TOOL_ENABLED: lambda args: backend.set_mcp_tool_enabled(
            server=args["server"],
            tool=args["tool"],
            enabled=args["enabled"],
        ),
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
        RpcMethod.SEARCH_CHAT: lambda args: backend.search_chat(
            args["session_id"],
            args["query"],
            int(args.get("limit", 50)),
        ),
        RpcMethod.UPLOAD_ATTACHMENT: lambda args: backend.upload_attachment(
            args["filename"], args["content_base64"]
        ),
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
        RpcMethod.CANCEL_AGENT_RUN: lambda args: backend.cancel_agent_run(args.get("run_id", "")),
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
        RpcMethod.PICK_DIR_NATIVE: _pick_dir_native,
        RpcMethod.GET_PROJECT_DIR: lambda args: str(backend.project_dir),
        # Pool-level method — intercepted in the dispatch loop before
        # per-runtime routing; this stub only exists so the
        # exhaustiveness check passes and a future regression (the
        # interception being removed) fails loudly.
        RpcMethod.ATTACH_SESSION: lambda args: (_ for _ in ()).throw(
            RuntimeError("attach_session must be handled at the session-pool level")
        ),
        # Per-client UI state — handled at the pool level so a single
        # global ClientStateStore serves every runtime. Stubs only
        # exist so the exhaustiveness check passes.
        RpcMethod.GET_CLIENT_STATE: lambda args: (_ for _ in ()).throw(
            RuntimeError("get_client_state must be handled at the session-pool level")
        ),
        RpcMethod.SET_CLIENT_STATE: lambda args: (_ for _ in ()).throw(
            RuntimeError("set_client_state must be handled at the session-pool level")
        ),
        RpcMethod.DELETE_CLIENT_STATE: lambda args: (_ for _ in ()).throw(
            RuntimeError("delete_client_state must be handled at the session-pool level")
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
        # ── Slash commands (SDK enumeration) ─────────────────────
        RpcMethod.GET_SLASH_COMMANDS: lambda args: backend.get_slash_commands(),
        # ── Todo list (CC TodoWrite parity) ──────────────────────
        RpcMethod.GET_TODOS: lambda args: backend.get_todos(),
        # ── Visualization actions (json-render round-trip) ───────
        RpcMethod.DISPATCH_VISUALIZATION_ACTION: lambda args: backend.dispatch_visualization_action(
            action=str(args.get("action", "")),
            params=args.get("params") or {},
        ),
        # ── Visualization persistence (restore across reloads) ───
        RpcMethod.SAVE_VISUALIZATION: lambda args: backend.save_visualization(
            spec_id=str(args.get("spec_id", "")),
            spec=args.get("spec") or {},
            title=str(args.get("title", "")),
            source_agent=str(args.get("source_agent", "")),
            run_id=str(args.get("run_id", "")),
        ),
        # ── Watcher panel (background process tail + kill) ───────
        RpcMethod.LIST_BACKGROUND_PROCESSES: lambda args: backend.list_background_processes(),
        RpcMethod.READ_PROCESS_TAIL: lambda args: backend.read_process_tail(
            pid=int(args.get("pid", 0)),
            tail=int(args.get("tail", 200)),
        ),
        RpcMethod.STOP_BACKGROUND_PROCESS: lambda args: backend.stop_background_process(
            pid=int(args.get("pid", 0)),
        ),
        # ── Latest plan (CC plan mode, row 50) ───────────────────
        RpcMethod.GET_LATEST_PLAN: lambda args: backend.get_latest_plan(),
        # Plan-card actions. ``Session.approve_plan`` /
        # ``Session.dismiss_plan`` are async because they
        # persist to ``session_data`` — the dispatcher awaits
        # whatever the lambda returns, so handing back the
        # coroutine is fine.
        RpcMethod.APPROVE_PLAN: lambda args: backend._session.approve_plan(
            run_id=str(args.get("run_id", "")),
        ),
        RpcMethod.DISMISS_PLAN: lambda args: backend._session.dismiss_plan(
            run_id=str(args.get("run_id", "")),
        ),
        # ── Output styles (CC row 52) ────────────────────────────
        RpcMethod.GET_OUTPUT_STYLES: lambda args: backend.get_output_styles(),
        # ── Knowledge ─────────────────────────────────────────────
        RpcMethod.GET_KNOWLEDGE_STATUS: lambda args: backend.get_knowledge_status(),
        RpcMethod.KNOWLEDGE_SEARCH: lambda args: backend.knowledge_search(args["query"]),
        RpcMethod.KNOWLEDGE_ADD: lambda args: backend.knowledge_add(args["source"]),
        RpcMethod.KNOWLEDGE_LIST: lambda args: backend.knowledge_list(),
        RpcMethod.KNOWLEDGE_GET: lambda args: backend.knowledge_get(args["id"]),
        RpcMethod.KNOWLEDGE_REMOVE: lambda args: backend.knowledge_remove(args["id"]),
        RpcMethod.READ_FILE: lambda args: backend.read_file(args["path"]),
        RpcMethod.SEARCH_CODE: lambda args: backend.search_code(
            args["snippet"], args.get("max_results", 20)
        ),
        RpcMethod.TRUNCATE_HISTORY: lambda args: backend.truncate_history(
            args["session_id"], args["run_id"]
        ),
        # ── Conversation ──────────────────────────────────────────
        RpcMethod.COUNT_CONTEXT_TOKENS: lambda args: backend.count_context_tokens(),
        # ── CodeIndex ─────────────────────────────────────────────
        RpcMethod.CODEINDEX_STATUS: lambda args: backend.codeindex_status(),
        RpcMethod.CODEINDEX_SYNC: lambda args: backend.codeindex_sync(args.get("sha")),
        RpcMethod.CODEINDEX_RESYNC: lambda args: backend.codeindex_resync(args.get("sha")),
        RpcMethod.CODEINDEX_CLEAN: lambda args: backend.codeindex_clean(),
        RpcMethod.CODEINDEX_INSTALL: lambda args: backend.codeindex_install(),
        RpcMethod.CODEINDEX_HEAD_BREAKDOWN: lambda args: backend.codeindex_head_breakdown(),
        RpcMethod.CODEINDEX_ACTIVITY: lambda args: backend.codeindex_activity(),
        # ── Plugins ───────────────────────────────────────────────
        RpcMethod.GET_PLUGIN_DETAILS: lambda args: backend.get_plugin_details(),
        RpcMethod.GET_PLUGIN_CONTENTS: lambda args: backend.get_plugin_contents(args["name"]),
        RpcMethod.PREVIEW_PLUGIN: lambda args: backend.preview_plugin(
            source=args["source"],
            branch=args.get("branch"),
            subdir=args.get("subdir"),
        ),
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
    # ``loop.add_signal_handler`` raises ``NotImplementedError`` on
    # Windows — asyncio's ProactorEventLoop / SelectorEventLoop don't
    # implement POSIX signals. On Windows we fall back to the default
    # behaviour: Ctrl-C raises KeyboardInterrupt which propagates out
    # of ``asyncio.run``, and the parent-PID watchdog still terminates
    # the BE when its parent (Tauri / VSCode / JetBrains) exits.
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _signal_handler)

    # SIGUSR1 → diagnostic "release as much memory as you can now":
    # forces a ``gc.collect()`` AND schedules an immediate
    # ``pool.evict_idle()`` sweep on the loop. Used by the
    # release-phase profiler to demonstrate the downward RSS trend
    # without waiting for the 5-minute sweep interval, and useful in
    # production triage to confirm a long-lived BE can still reclaim.
    def _release_handler():
        import gc as _gc

        before = _gc.get_count()
        collected = _gc.collect()
        logger.info(
            "SIGUSR1: forced gc.collect — collected %d objects (generation counts before: %s)",
            collected,
            before,
        )
        # Pool not yet created (boot-time signal) → NameError → noop;
        # ``pool`` is late-bound via the enclosing scope so we can't
        # check ``is None`` at definition time.
        with contextlib.suppress(NameError):
            loop.create_task(pool.evict_idle())  # noqa: F821 — late-bound

    with contextlib.suppress(NotImplementedError):  # not on Windows
        loop.add_signal_handler(signal.SIGUSR1, _release_handler)

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

    # Orchestrate progress → push notification. ``orchestrate.py`` now
    # emits structured event dicts (``{type, agent_path, …}``); we
    # forward them on a dedicated channel so the FE can build a proper
    # tree UI instead of trying to parse tree-art strings. Plain
    # strings still work for any path that hasn't been ported.
    def _on_progress(event: Any) -> None:
        if isinstance(event, dict):
            payload = event
            channel = "orchestrate_event"
        else:
            payload = {"line": str(event)}
            channel = "orchestrate_progress"
        asyncio.ensure_future(
            transport.send(msg.PushNotification(channel=channel, payload=payload))
        )

    backend.wire_orchestrate_progress(_on_progress)

    # Plan-mode broadcasts — `set_permission_mode` and
    # `exit_plan_mode` fire via `Session.broadcast(channel,
    # payload)`. We hop onto the running event loop because
    # ``broadcast`` is sync and may be called from a tool-call
    # context that isn't itself awaiting. Channels:
    # ``permission_mode_changed`` (badge update) and
    # ``plan_submitted`` (inline plan card).
    _plan_loop = asyncio.get_running_loop()

    def _make_broadcast_callback(send_through: Any):
        """Build a ``(channel, payload) → PushNotification`` shim bound to
        a specific transport. Pooled sessions get one bound to their
        ``SessionStampingTransport`` so the push gets the correct
        ``session_id`` and the FE routes it to the right view.

        Without per-runtime binding, broadcasts from pool-created
        sessions would either fail (no callback registered) or land
        unstamped on the boot transport and surface in the wrong view.
        """

        def _on_event(channel: str, payload: dict) -> None:
            def _send() -> None:
                asyncio.ensure_future(
                    send_through.send(msg.PushNotification(channel=channel, payload=payload))
                )

            # Event loop closed during shutdown — drop the push.
            with contextlib.suppress(RuntimeError):
                _plan_loop.call_soon_threadsafe(_send)

        return _on_event

    if hasattr(backend, "_session") and hasattr(backend._session, "register_broadcast_callback"):
        backend._session.register_broadcast_callback(_make_broadcast_callback(transport))

    # ── File-edit push notifications ─────────────────────────────
    # Edit tools call ``set_file_edit_listener`` from any thread the
    # toolkit happens to run on, so the listener can't directly
    # ``await transport.send(...)``. Hop onto the event loop via
    # ``call_soon_threadsafe`` and then schedule the coroutine.
    # Downstream clients (JetBrains plugin in particular) react by
    # refreshing the VFS so Local History captures the change.
    from ember_code.core.tools.edit import set_file_edit_listener

    _loop = asyncio.get_running_loop()

    def _on_file_edit(abs_path: str) -> None:
        def _schedule() -> None:
            asyncio.ensure_future(
                transport.send(
                    msg.PushNotification(
                        channel="file_edited",
                        payload={"path": abs_path},
                    )
                )
            )

        _loop.call_soon_threadsafe(_schedule)

    set_file_edit_listener(_on_file_edit)

    # ── Background-process watcher push channels ────────────────
    # The shell tool's per-line / start / exit subscribers fire
    # from the reader task on the event loop. We forward each as
    # an unstamped PushNotification on the boot transport — every
    # connected client sees the same registry (one BE = one
    # registry; watcher state is process-global, not per-session).
    # Without these forwards the FE's watcher panel would either
    # have to poll ``list_background_processes`` (lag + load) or
    # miss the exit signal entirely.
    from ember_code.core.tools.shell import (
        subscribe_to_process_completion,
        subscribe_to_process_line,
        subscribe_to_process_start,
    )

    def _push_process_event(channel: str, payload: dict) -> None:
        """Schedule a PushNotification on the running loop. Mirrors
        the file-edit forwarder shape — the subscriber callback
        fires sync on the loop, but ``transport.send`` is async."""

        def _schedule() -> None:
            asyncio.ensure_future(
                transport.send(msg.PushNotification(channel=channel, payload=payload))
            )

        with contextlib.suppress(RuntimeError):
            _loop.call_soon_threadsafe(_schedule)

    def _on_process_start(info: dict) -> None:
        _push_process_event(
            "process_started",
            {"pid": info.get("pid"), "cmd": info.get("cmd"), "started_at": info.get("started_at")},
        )

    def _on_process_line(info: dict) -> None:
        _push_process_event(
            "process_line",
            {"pid": info.get("pid"), "line": info.get("line")},
        )

    def _on_process_completion(info: dict) -> None:
        _push_process_event(
            "process_exited",
            {
                "pid": info.get("pid"),
                "cmd": info.get("cmd"),
                "exit_code": info.get("exit_code"),
                "duration_seconds": info.get("duration_seconds"),
            },
        )

    subscribe_to_process_start(_on_process_start)
    subscribe_to_process_line(_on_process_line)
    subscribe_to_process_completion(_on_process_completion)

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
        # Publish the port + version at ``<project>/.ember/backend.lock``
        # so a second client opening the same project can discover
        # this BE and connect to it instead of spawning a duplicate.
        # See ``clients/vscode/src/extension.ts`` +
        # ``clients/jetbrains/.../EmberBackendService.kt`` for the
        # discovery half. Removed on graceful shutdown below.
        from ember_code.backend.lockfile import Lockfile

        try:
            wire_version = (Path(__file__).parent.parent / "__init__.py").read_text()
            # Parse ``__version__ = "X.Y.Z"`` — one-liner without
            # importing the package (avoids circular during boot).
            import re as _re

            m = _re.search(r'__version__\s*=\s*"([^"]+)"', wire_version)
            wire_version = m.group(1) if m else "0.0.0"
        except OSError:
            wire_version = "0.0.0"
        backend_lock = Lockfile(project_dir)
        try:
            backend_lock.write(pid=os.getpid(), port=ws_transport.port, wire_version=wire_version)
        except OSError as exc:
            logger.warning("could not write backend lockfile: %s", exc)
            backend_lock = None  # type: ignore[assignment]
    else:
        backend_lock = None  # type: ignore[assignment]
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

    # Start the scheduled-task poller. Push notifications (started /
    # completed) flow through the same transport every client shares.
    # ``start_scheduler`` is idempotent (caches the runner on the pool)
    # so the existing client-side ``start_scheduler`` RPC call stays
    # safe — calling it again just returns the running instance.
    try:
        _start_scheduler_with_push(backend, transport)
    except Exception:
        logger.exception("Auto-start of scheduler failed; will retry on client RPC")

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

        # Per-client UI state — survives reloads, shared across all
        # clients (web, JetBrains, VSCode) since each holds the same
        # opaque ``client_id`` and the BE is the source of truth.
        from ember_code.core.session.client_state import ClientStateStore

        client_state = ClientStateStore.from_data_dir(settings.storage.data_dir)

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
            # Wire pool-session broadcasts (plan_submitted,
            # permission_mode_changed, …) through the stamped transport
            # so they reach the FE with the right session_id. Without
            # this, ``exit_plan_mode`` from a pooled session never
            # produces a PlanCard.
            if hasattr(rt_backend._session, "register_broadcast_callback"):
                rt_backend._session.register_broadcast_callback(_make_broadcast_callback(stamped))
            return SessionRuntime(
                backend=rt_backend,
                rpc_table=rt_table,
                queue=rt_queue,
                transport=stamped,
            )

        # ``EMBER_SESSION_IDLE_TIMEOUT`` lets ops tune the eviction
        # window without a code change (e.g. very long-running CI
        # supervisors want a tighter timeout to cap RAM). Default is
        # 30 minutes — defined in ``session_pool._DEFAULT_IDLE_TIMEOUT``.
        try:
            _idle_secs = float(os.environ.get("EMBER_SESSION_IDLE_TIMEOUT", ""))
        except ValueError:
            _idle_secs = 0.0
        pool_kwargs: dict[str, Any] = {}
        if _idle_secs > 0:
            pool_kwargs["idle_timeout_seconds"] = _idle_secs
        pool = SessionPool(default_runtime, _create_runtime, **pool_kwargs)

        # Background evictor: sweep every 5 minutes and drop runtimes
        # idle longer than ``idle_timeout_seconds``. The default
        # runtime + any currently-processing runtimes are spared (see
        # ``SessionPool.evict_idle``). Keep the strong ref so the task
        # isn't GC'd mid-loop — that's a real asyncio footgun.
        async def _evictor_loop() -> None:
            sweep_interval = 5 * 60
            while not shutdown_event.is_set():
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=sweep_interval)
                    # Shutdown fired — exit cleanly.
                    return
                except asyncio.TimeoutError:
                    pass
                try:
                    evicted = await pool.evict_idle()
                    if evicted:
                        logger.info(
                            "session pool: evicted %d idle session(s): %s",
                            len(evicted),
                            evicted,
                        )
                except Exception as exc:
                    logger.warning("evictor sweep failed: %s", exc)

        evictor_task = asyncio.create_task(_evictor_loop())

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
                        msg.RPCResponse(id=message.id or "", error=f"not a directory: {wanted_dir}")
                    )
                    return
                if not session_id:
                    import uuid as _uuid

                    session_id = str(_uuid.uuid4())[:8]
                # Register BEFORE creation so the factory picks the
                # directory up.
                dir_registry.set_dir(session_id, wd.resolve())
            elif session_id:
                # No explicit ``project_dir`` — the caller is restoring
                # a previously-bound session (typical: FE reconnecting
                # after a page reload). Refuse the attach if that
                # session belongs to a different project than this
                # BE was launched with. Without this check, a stale
                # FE session_id from a prior run in another repo
                # silently opens THAT repo's ``state.db`` inside the
                # current BE, and the sidebar starts listing sessions
                # from the wrong project.
                existing_dir = dir_registry.get_dir(session_id)
                if existing_dir and Path(existing_dir).resolve() != project_dir.resolve():
                    await transport.send(
                        msg.RPCResponse(
                            id=message.id or "",
                            error=(
                                f"session {session_id} belongs to a different "
                                f"project ({existing_dir}); this backend is "
                                f"bound to {project_dir}"
                            ),
                        )
                    )
                    return
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

        async def _client_state_rpc(message: Any) -> None:
            """Pool-level RPC: read/write per-client UI state. Not
            tied to any session — clients only need a stable
            ``client_id`` to round-trip their preferences."""
            args = message.args or {}
            client_id = str(args.get("client_id") or "").strip()
            method = message.method
            try:
                # ``result`` straddles two response shapes (read returns
                # ``dict[str, str]``, write returns ``{"ok": True}``);
                # annotate as a generic mapping so mypy stops narrowing
                # the type on the first branch.
                result: dict[str, Any]
                if method == RpcMethod.GET_CLIENT_STATE:
                    result = client_state.get_for_client(client_id)
                elif method == RpcMethod.SET_CLIENT_STATE:
                    client_state.set_value(
                        client_id, str(args.get("key") or ""), str(args.get("value") or "")
                    )
                    result = {"ok": True}
                else:  # DELETE_CLIENT_STATE
                    client_state.delete_value(client_id, str(args.get("key") or ""))
                    result = {"ok": True}
                await transport.send(msg.RPCResponse(id=message.id or "", result=result))
            except Exception as exc:
                await transport.send(
                    msg.RPCResponse(id=message.id or "", error=f"client_state: {exc}")
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
                if isinstance(message, msg.RPCRequest) and message.method in (
                    RpcMethod.GET_CLIENT_STATE,
                    RpcMethod.SET_CLIENT_STATE,
                    RpcMethod.DELETE_CLIENT_STATE,
                ):
                    await _client_state_rpc(message)
                    return
                # ``SessionList`` is a project-scoped query — "what
                # sessions exist in THIS project's ``state.db``" — not
                # a session-scoped one. Routing it through
                # ``pool.get_or_create(session_id)`` picks a runtime
                # bound to whatever project that session was created
                # in, which for a stale FE session_id can be a
                # different project than the one the user actually
                # opened. Result: the sidebar shows sessions from a
                # different repo. Force listing to run on the boot
                # runtime — the one bound to the BE's ``--project-dir``.
                if isinstance(message, msg.SessionList):
                    rt = default_runtime
                    await _handle_message(
                        message, rt.backend, rt.transport, rt.rpc_table, rt.queue, login_state
                    )
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
                # Dedupe: only write to ``sessions.db`` when this
                # runtime's id or project_dir actually changed. Without
                # this, every single dispatched message (including
                # idempotent get_status polls) opens a SQLite
                # connection — at ~330 req/s with 4 sessions that piled
                # ~180 KiB of resident Connection state per RPC until
                # GC caught up, masquerading as a memory leak.
                current_dir_key = (rt.backend.session_id, str(rt.backend.project_dir))
                last_key = getattr(rt, "_last_dir_registered", None)
                if current_dir_key != last_key:
                    dir_registry.set_dir(rt.backend.session_id, rt.backend.project_dir)
                    rt._last_dir_registered = current_dir_key  # type: ignore[attr-defined]
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
        with contextlib.suppress(NameError):
            evictor_task.cancel()  # noqa: F821 — set in the try body
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await parent_watch_task
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await shutdown_close_task
        with contextlib.suppress(asyncio.CancelledError, Exception, NameError):
            await evictor_task  # noqa: F821
        try:
            # Shut down every pooled runtime (includes the default).
            await pool.shutdown()  # noqa: F821 — defined in the try body
        except (NameError, UnboundLocalError):
            # Startup failed before the pool existed.
            await backend.shutdown()
        await transport.close()
        # Remove the discovery lockfile so the next client that
        # opens this project spawns a fresh BE instead of trying to
        # connect to a dead port. ``remove`` is idempotent — safe
        # to call even if the lockfile was never written (e.g.
        # Unix-socket-only start).
        if backend_lock is not None:  # noqa: F821 — set above
            with contextlib.suppress(Exception):
                backend_lock.remove()  # noqa: F821
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
        # Send the ORIGINAL (un-wrapped) text so chat bubbles stay
        # clean — the system-context wrapper below is for the LLM
        # only, not for display.
        await transport.send(
            msg.UserMessageReceived(text=message.text, client_id=message.client_id)
        )
        # One-shot ``/plan`` → ``enter_plan_mode`` nudge. When the
        # user just typed ``/plan`` (slash command armed
        # ``_plan_research_armed`` on the session), prepend a
        # ``<system-context>`` instruction so the agent spawns the
        # ``plan_researcher`` sub-agent on this exact request.
        # Cleared after one use — subsequent turns in the same plan
        # mode session don't get the hint again. FE strips
        # ``<system-context>`` blocks on display, so the chat
        # bubble shows only what the user typed.
        agent_text = message.text
        sess = getattr(backend, "_session", None)
        # ``is True`` (not just truthy) because mocked sessions in
        # tests use ``MagicMock`` which auto-spawns missing attrs
        # as MagicMock instances — those evaluate truthy and would
        # wrap every test message. The slash command sets the
        # attribute to the real ``True``.
        if sess is not None and getattr(sess, "_plan_research_armed", False) is True:
            sess._plan_research_armed = False
            agent_text = (
                "<system-context>\n"
                "Plan mode was just entered via the user's /plan slash "
                "command. BEFORE doing any other work, call "
                "`enter_plan_mode(task=...)` with the user's request "
                "below as the ``task`` argument so the plan_researcher "
                "sub-agent runs first and produces a structured "
                "Findings + Proposed Plan + Open Questions report. "
                "Do not skip this step — the researcher's report is "
                "what makes the eventual ``exit_plan_mode`` call "
                "grounded enough to pass the confidence validator.\n"
                "</system-context>\n\n" + message.text
            )
        async for proto in backend.run_message(agent_text, media=message.file_contents):
            if req_id:
                proto = proto.model_copy(update={"id": req_id})
            await transport.send(proto)
        await transport.send(msg.StreamEnd(id=req_id))

        # First completed run: name the session in the background so a
        # queued follow-up isn't blocked on the naming model call.
        async def _auto_name() -> None:
            name = await backend.maybe_auto_name_session()
            if name:
                await transport.send(
                    msg.PushNotification(
                        channel="session_named",
                        payload={"session_id": backend.session_id, "name": name},
                    )
                )

        task = asyncio.create_task(_auto_name())
        _AUTO_NAME_TASKS.add(task)
        task.add_done_callback(_AUTO_NAME_TASKS.discard)

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
            # ``login_state["task"]`` is set to an ``asyncio.Task`` by
            # ``_login`` but the dict is typed ``dict[str, Any]``;
            # annotate locally (under a non-shadowing name) so the
            # ``.done()`` / ``.cancel()`` calls don't trip mypy.
            login_task: asyncio.Task[None] | None = login_state.get("task")
            if login_task and not login_task.done():
                login_task.cancel()

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
