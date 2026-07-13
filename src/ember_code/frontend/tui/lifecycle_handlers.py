"""TUI lifecycle handlers: mount, unmount, startup background tasks.

Extracted from ``tui/app.py``. Same pattern as the other TUI
handler modules — ``@on`` / ``on_*``-decorated class methods
stay on :class:`EmberApp` as one-line delegates; bodies live
here.

Free functions taking ``app: EmberApp`` as first arg:

* :func:`on_mount_inner` — post-``on_mount`` startup: mount
  conversation container, spawn the BE subprocess, wire the
  mirror handler, welcome message, install managers, load
  history when resuming, kick every background task
  (scheduler, hooks, MCP, codeindex badge poll, first
  message).
* :func:`init_mcp_background` — connect user-configured MCP
  servers post-mount.
* :func:`refresh_cloud_models_on_startup` — pull the cloud
  model registry into ``settings.models.registry`` so the
  run controller's ``_has_usable_model`` doesn't reject the
  first message.
* :func:`auto_sync_knowledge` — pull the knowledge file →
  vector store, if enabled.
* :func:`check_for_update` — hit the update endpoint,
  populate the update-bar.
* :func:`on_unmount` — teardown: stop scheduler, redirect
  stderr to /dev/null (mask MCP cleanup noise), stop BE.

Rule 2 clean — all imports at module top.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import TYPE_CHECKING

from textual.containers import ScrollableContainer
from textual.widgets import Static

from ember_code.core.auth.credentials import CloudCredentials
from ember_code.core.config.cloud_models import (
    fetch_cloud_models,
    merge_into_registry,
)
from ember_code.core.utils.file_index import FileIndex
from ember_code.frontend.tui.conversation_view import ConversationView
from ember_code.frontend.tui.hitl_handler import HITLHandler
from ember_code.frontend.tui.input_handler import InputHandler
from ember_code.frontend.tui.process_manager import BackendProcess
from ember_code.frontend.tui.run_controller import RunController
from ember_code.frontend.tui.session_manager import SessionManager
from ember_code.frontend.tui.status_tracker import StatusTracker
from ember_code.frontend.tui.widgets import PromptInput, UpdateBar
from ember_code.protocol.rpc import RpcMethod

if TYPE_CHECKING:
    from ember_code.frontend.tui.app import EmberApp

logger = logging.getLogger(__name__)


async def on_mount_inner(app: "EmberApp") -> None:
    """Async body of Textual's ``on_mount`` — full startup.

    Split out from :meth:`EmberApp.on_mount` because Textual
    silently swallows ``on_mount`` exceptions and tears the app
    down with exit code 0 — the outer method logs any raise so
    future regressions surface in ``~/.ember/debug.log``.
    """
    # Use ANSI colors so the terminal's own palette is respected.
    app.ansi_color = True
    # ``ansi-dark`` is the registered ANSI-respecting theme on
    # Textual 8.2+ (the prior name ``textual-ansi`` was retired).
    # Guard on ``available_themes`` so a future rename can't
    # bring the app down — if the theme isn't there, the default
    # styling is fine; ``ansi_color = True`` above is what
    # actually makes Textual respect the terminal palette.
    if "ansi-dark" in app.available_themes:
        app.theme = "ansi-dark"

    container = app.query_one("#conversation", ScrollableContainer)

    # Show loading indicator while BE starts.
    loading = Static("[dim]Starting backend...[/dim]", id="loading-msg")
    await container.mount(loading)

    # Spawn BE as a separate subprocess — no Textual fd
    # restrictions.
    app._process_mgr = BackendProcess(
        project_dir=app._project_dir,
        resume_session_id=app.resume_session_id,
        additional_dirs=app._additional_dirs,
        settings=app.settings,
        debug=app._debug,
    )
    app._backend = await app._process_mgr.start()

    # Mirroring: render events from other views attached to the
    # same BE (web tabs over --ws-port). Remote drafts go to the
    # tip bar; remote user messages appear as dim info lines.
    app._backend.set_mirror_handler(app._on_mirror_event)

    # Replace loading indicator with welcome content.
    await container.remove_children()
    app._conversation = ConversationView(container, display_config=app.settings.display)

    await container.mount(Static(app._build_welcome_content(), id="welcome-box"))
    await container.mount(Static(app._build_capabilities_text(), id="capabilities"))

    app._file_index = FileIndex(app._project_dir)
    app._input_handler = InputHandler(
        app._backend.get_skill_pool(), file_index=app._file_index
    )
    # CommandHandler is now inside BackendServer — commands
    # route through _backend.handle_command().

    # Initialise managers.
    app._status = StatusTracker(app)
    app._hitl = HITLHandler(app, app._conversation)
    app._controller = RunController(
        app,
        app._conversation,
        app._status,
        app._hitl,
    )
    app._sessions = SessionManager(
        app,
        app._conversation,
        app._status,
    )

    # Resolve context window for the active model. Context
    # window comes from settings — no model registry needed in
    # FE.
    app._status.max_context_tokens = app.settings.models.max_context_window

    app._status.update_status_bar()

    # Load previous messages if resuming a session.
    if app.resume_session_id:
        await app._sessions._load_history(app.resume_session_id)

    # Show a random tip.
    app._start_tip_rotation()

    app.query_one("#user-input", PromptInput).focus()

    # ── Login push handlers (permanent — widget checks if mounted) ──
    app._backend._push_handlers["login_status"] = app._on_login_status_push
    app._backend._push_handlers["login_result"] = app._on_login_result_push

    # ── Scheduler ──────────────────────────────────────────────────
    app._start_scheduler()

    # ── Fire SessionStart hook ────────────────────────────────────
    asyncio.create_task(app._backend.fire_session_start_hook())

    # ── Non-blocking background init ──────────────────────────────
    asyncio.create_task(app._check_for_update())
    asyncio.create_task(app._init_mcp_background())
    asyncio.create_task(app._file_index.ensure_loaded())
    asyncio.create_task(app._auto_sync_knowledge())
    # Cloud-discovered models live only in memory — without an
    # on-mount refresh, ``_has_usable_model`` reports False on
    # every fresh launch even when the user has a valid token
    # and a previously-selected default. Surfaced as "No model
    # configured" on the very first message; ``/login`` or
    # ``/model`` would silently fix it because both refresh the
    # registry as a side-effect. Mirror their refresh here so
    # the first message just works.
    asyncio.create_task(app._refresh_cloud_models_on_startup())

    # CodeIndex status-bar slot — eager refresh + recurring
    # poll so the badge reflects current state even before the
    # user opens the ``/codeindex`` panel. Survives panel close
    # (independent from the 2s panel poll which only runs while
    # the panel is mounted).
    asyncio.create_task(app._refresh_codeindex_badge())
    app.set_interval(
        app._CODEINDEX_STATUSBAR_POLL_SECONDS,
        app._refresh_codeindex_badge,
    )

    if app.initial_message:
        task = asyncio.create_task(
            app._controller.process_message(app.initial_message),
        )
        app._controller.set_current_task(task)


async def init_mcp_background(app: "EmberApp") -> None:
    """Connect user-configured MCP servers in the background."""
    logger.info("FE: MCP background init starting")
    try:
        await app._backend.ensure_mcp()
        statuses = (
            await app._backend._rpc(RpcMethod.GET_MCP_STATUS)
            if hasattr(app._backend, "_rpc")
            else app._backend.get_mcp_status()
        )
        if statuses:
            # Same per-server log as the BE side — gives a
            # complete picture in ``debug.log`` of "connected
            # at T1 (BE)" → "FE saw it at T2".
            connected_names = [n for n, c in statuses if c]
            logger.info(
                "FE: MCP background init done — %d/%d connected: %s",
                len(connected_names),
                len(statuses),
                connected_names,
            )
            for name, connected in statuses:
                app._status.set_ide_status(name, connected)
        else:
            logger.info("FE: MCP background init done — no servers configured")
    except Exception as exc:
        # Upgraded from DEBUG to WARNING so this is visible
        # without the user re-running with ``--debug``.
        logger.warning("FE: MCP background init failed: %s", exc, exc_info=True)


async def refresh_cloud_models_on_startup(app: "EmberApp") -> None:
    """Repopulate ``settings.models.registry`` from cloud discovery.

    ``models.registry`` lives only in memory; nothing persists
    it across CLI runs. ``settings.models.default`` IS persisted
    (it's written to ``~/.ember/config.yaml`` on ``/model``
    selection), so the status bar reads the previously-chosen
    model and renders ready. But the run controller's
    pre-flight ``_has_usable_model`` iterates the registry —
    finds it empty on a fresh launch — and rejects the first
    message with the cryptic "No model configured" prompt.
    Running ``/login`` or ``/model`` "fixes" it because each
    refresh-on-open call also merges cloud entries; this task
    mirrors that side-effect at mount time so the first message
    just works.

    Fire-and-forget, bounded by ``fetch_cloud_models``'s
    built-in 3-second timeout. Soft-fail on any error (no
    token, network down, 403 from the org check) — the user
    can still reach ``/login`` from the TUI to recover.
    """
    try:
        cloud_token = CloudCredentials(app.settings.auth.credentials_file).access_token
        if not cloud_token:
            return
        # ``fetch_cloud_models`` is sync (3s timeout). Push to
        # a thread so the event loop stays free while it
        # blocks.
        loop = asyncio.get_event_loop()
        entries = await loop.run_in_executor(
            None, fetch_cloud_models, app.settings.api_url, cloud_token
        )
        if entries:
            added = merge_into_registry(
                app.settings.models.registry, entries, app.settings.api_url
            )
            if added:
                logger.info("FE: merged %d cloud model(s) on startup", added)
    except Exception as exc:
        logger.warning("FE: cloud-models startup refresh failed: %s", exc, exc_info=True)


async def auto_sync_knowledge(app: "EmberApp") -> None:
    """Auto-sync knowledge file → DB on startup if enabled."""
    try:
        result = await app._backend.auto_sync_knowledge()
        if result:
            app._conversation.append_info(result)
    except Exception as e:
        logger.warning("Auto knowledge sync failed: %s", e)


async def check_for_update(app: "EmberApp") -> None:
    """Check for a newer CLI version via BE RPC and populate
    the update-bar when one is available."""
    try:
        result = await app._backend._rpc(RpcMethod.CHECK_FOR_UPDATE)
        logger.debug("Update check result: %s", result)
        if result and result.get("available"):
            bar = app.query_one("#update-bar", UpdateBar)
            bar.show_update(
                current=result.get("current_version", ""),
                latest=result.get("latest_version", ""),
                url=result.get("download_url", ""),
                pkg_name=result.get("pkg_name", ""),
            )
    except Exception as e:
        logger.debug("Update check error: %s", e)


async def on_unmount(app: "EmberApp") -> None:
    """Clean up scheduler and BE subprocess on app exit.

    Redirects fd 2 → /dev/null BEFORE stopping the BE — MCP
    stdio cleanup triggers anyio cancel-scope errors that print
    after the TUI exits and mess up the terminal.
    """
    if app._scheduler_runner:
        app._scheduler_runner.stop()

    try:
        sys.stderr.flush()
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull_fd, 2)
        os.close(devnull_fd)
    except OSError:
        pass

    if app._process_mgr:
        await app._process_mgr.stop()
