"""Backend server — processes FE messages and streams BE events.

Owns the Session object and all Agno/AI logic. The FE never touches
Session directly — everything goes through protocol messages.

In Phase 2 (single-process), this is called in-process by the TUI.
In Phase 4 (multi-process), this runs as a separate process with
socket transport.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ember_code.backend import (
    server_auth,
    server_codeindex,
    server_context,
    server_files,
    server_history,
    server_hitl,
    server_knowledge,
    server_lifecycle,
    server_loop,
    server_mcp,
    server_panels,
    server_pause,
    server_plugin,
    server_processes,
    server_rehydrate,
    server_run,
    server_search,
    server_sessions,
)
from ember_code.backend.server_helpers import (  # noqa: F401 — re-exported for tests
    _SEARCH_CHAT_SNIPPET_HALF_WIDTH,
    PluginContents,
    _scan_plugin_dir,
    _search_history,
    _split_assistant_content_for_restore,
)
from ember_code.protocol import messages as msg
from pydantic import BaseModel

if TYPE_CHECKING:
    from ember_code.core.config.settings import Settings
    from ember_code.core.plugins.models import MarketplaceInfo, PluginInfo
    from ember_code.core.pool import AgentInfo
    from ember_code.core.skills.parser import SkillInfo

logger = logging.getLogger(__name__)


class CancelAgentRunResult(BaseModel):
    """Wire shape for :meth:`BackendServer.cancel_agent_run` — the
    FE renders a toast on ``ok=False`` so ``error`` differentiates
    the unknown-run-id case from a live cancel error."""

    ok: bool
    error: str = ""


class LatestPlanResult(BaseModel):
    """Wire shape for :meth:`BackendServer.get_latest_plan` — the
    plan-mode panel reads this on open + after each ``exit_plan_mode``.

    ``state`` is ``"pending"`` when a plan exists (user hasn't
    approved/dismissed yet) or ``""`` when no plan submitted.
    ``tasks`` mirrors ``TodoStore.snapshot`` (activeForm camelCase
    dicts) so the FE renders the plan and task list from a single
    payload."""

    latest: str = ""
    history: list[str] = []
    tasks: list[dict] = []
    state: str = ""


class VisualizationActionResult(BaseModel):
    """Wire shape for :meth:`BackendServer.dispatch_visualization_action`
    — the FE's tool result echo of the action name + user-supplied
    params so it can render "you clicked X" in the conversation."""

    ok: bool
    action: str
    params: dict = {}


class KnowledgeStatus(BaseModel):
    """Wire shape for :meth:`BackendServer.get_knowledge_status` —
    KB panel header. ``embedder`` carries the active embedding
    provider (empty when KB disabled)."""

    enabled: bool
    collection_name: str
    document_count: int
    embedder: str


class BackendServer:
    """Wraps Session and handles all FE→BE protocol messages."""

    def __init__(
        self,
        settings: Settings,
        project_dir: Path | None = None,
        resume_session_id: str | None = None,
        additional_dirs: list[Path] | None = None,
    ):
        from ember_code.core.code_index.paths import state_db_path
        from ember_code.core.session import Session
        from ember_code.core.session.session_preferences import SessionPreferencesStore

        # Per-session prefs need to be consulted BEFORE the Session
        # builds its main team, since the team binds whatever model
        # is in ``settings.models.default`` at construction time. The
        # store lives in the project-local ``state.db``, so we have
        # to know the project_dir up front — fall back to cwd to
        # mirror what Session does internally.
        resolved_project_dir = project_dir or Path.cwd()
        self._session_prefs = SessionPreferencesStore(
            state_db_path(resolved_project_dir, data_dir=settings.storage.data_dir),
        )
        if resume_session_id:
            persisted_model = self._session_prefs.get_model(resume_session_id)
            if persisted_model and persisted_model in settings.models.registry:
                settings.models.default = persisted_model

        self._session = Session(
            settings,
            project_dir=project_dir,
            resume_session_id=resume_session_id,
            additional_dirs=additional_dirs,
        )
        self._settings = settings
        self._pending_requirements: dict[str, Any] = {}  # requirement_id → Agno requirement
        # Auto-resolved requirements waiting to merge into the next
        # ``acontinue_run`` call. Populated by ``_handle_pause`` when
        # the permission evaluator (plan / acceptEdits / bypass / deny
        # rules) decides a paused tool BEFORE the user is asked.
        # ``resolve_hitl_batch`` drains the bucket for the same run_id
        # and includes them alongside the user-resolved reqs so Agno
        # gets the full resolution set in one resume.
        self._auto_resolved_requirements: dict[str, list[Any]] = {}
        self._processing = False
        self._current_team: Any = None  # held during HITL pause
        # Task currently iterating run_message → tool calls → events.
        # ``cancel_run`` calls ``.cancel()`` on this to bail out of any
        # awaits — the most reliable way to stop a streamed run when
        # Agno's cooperative cancellation doesn't propagate (e.g. a
        # broadcast sub-agent is mid-tool-call). Set when iteration
        # starts, cleared in ``finally``.
        self._current_run_task: asyncio.Task | None = None
        # Serialises concurrent ``run_message`` calls. The FE unblocks
        # user input on ``StreamingDone`` (emitted when Agno's content
        # stream ends) but the previous run's Agno tail —
        # compression, memory/learning extraction, final persistence —
        # is still draining. Two ``team.arun()`` calls in flight on the
        # same Agno team would race on session/memory state, so the
        # lock makes the second call wait silently until the previous
        # tail finishes. From the user's POV the second submit just
        # shows the normal "Thinking" UI for a beat longer than usual.
        self._run_lock = asyncio.Lock()
        # Set during ``startup`` if the resumed session's last run
        # had ``status=running`` — i.e. the previous process crashed
        # mid-chain. The next ``run_message`` injects a system note
        # so the agent knows it was interrupted and can decide
        # whether to recap, retry, or pick up where it left off.
        # Cleared after the note is consumed (one-shot per launch).
        self._interrupted_run_summary: str | None = None
        # Pending-message ids surfaced on the next ``--continue``
        # boot. Kept alive in the store (not discarded by
        # ``_detect_interrupted_run``) so the FE can fetch them via
        # ``get_pending_messages`` and render the interrupted prompt
        # as a chat-history entry. Cleared from the store after the
        # next ``run_message`` consumes the summary.
        self._pending_message_ids_to_drop: list[str] = []
        # Latched from RunCompleted.input_tokens for the live ctx
        # footer — kept on the Session so /clear (which goes through
        # CommandHandler with only a session ref) can reset it.
        self._session._last_input_tokens = 0
        # Pre-persist user messages BEFORE handing them to Agno.
        # Agno's streaming runs don't write to disk until the run
        # completes, so a kill mid-stream loses the user's prompt
        # entirely — the partial response, sure, but also the
        # question they asked. The store lives in the same
        # state.db file Agno uses; the table is created on first
        # touch via ``CREATE TABLE IF NOT EXISTS``.
        from ember_code.core.session.pending_messages import PendingMessageStore

        self._pending_store = PendingMessageStore(
            state_db_path(
                self._session.project_dir,
                data_dir=settings.storage.data_dir,
            ),
        )

    # No .session property — all access goes through backend methods

    @property
    def project_dir(self) -> Path:
        return self._session.project_dir

    async def startup(self) -> None:
        """See :func:`backend.server_lifecycle.startup`."""
        await server_lifecycle.startup(self)

    async def _rehydrate_event_log(self) -> None:
        """See :func:`backend.server_rehydrate.rehydrate_event_log`."""
        await server_rehydrate.rehydrate_event_log(self)

    async def _rehydrate_orphan_processes(self) -> None:
        """See :func:`backend.server_rehydrate.rehydrate_orphan_processes`."""
        await server_rehydrate.rehydrate_orphan_processes(self)

    async def _rehydrate_plan_decisions(self) -> None:
        """See :func:`backend.server_rehydrate.rehydrate_plan_decisions`."""
        await server_rehydrate.rehydrate_plan_decisions(self)

    async def _rehydrate_todos(self) -> None:
        """See :func:`backend.server_rehydrate.rehydrate_todos`."""
        await server_rehydrate.rehydrate_todos(self)

    async def _rehydrate_plan_store(self) -> None:
        """See :func:`backend.server_rehydrate.rehydrate_plan_store`."""
        await server_rehydrate.rehydrate_plan_store(self)

    async def _detect_interrupted_run(self) -> None:
        """See :func:`backend.server_lifecycle.detect_interrupted_run`."""
        await server_lifecycle.detect_interrupted_run(self)

    # ── Run a user message (streaming) ────────────────────────────

    async def run_message(
        self, text: str, media: dict[str, Any] | None = None
    ) -> AsyncIterator[msg.Message]:
        """See :func:`backend.server_run.run_message`."""
        async for proto in server_run.run_message(self, text, media):
            yield proto

    async def _run_message_locked(
        self, text: str, media: dict[str, Any] | None
    ) -> AsyncIterator[msg.Message]:
        """See :func:`backend.server_run.run_message_locked`."""
        async for proto in server_run.run_message_locked(self, text, media):
            yield proto

    async def _stream_with_subagent_hitl(
        self, team_stream: AsyncIterator[Any]
    ) -> AsyncIterator[msg.Message]:
        """See :func:`backend.server_pause.stream_with_subagent_hitl`."""
        async for proto in server_pause.stream_with_subagent_hitl(self, team_stream):
            yield proto

    def _build_subagent_run_paused(self, entries: list) -> msg.Message:
        """See :func:`backend.server_pause.build_subagent_run_paused`."""
        return server_pause.build_subagent_run_paused(entries)

    async def _periodic_checkpoint(self, team: Any, interval: float = 3.0) -> None:
        """See :func:`backend.server_pause.periodic_checkpoint`."""
        await server_pause.periodic_checkpoint(self, team, interval)

    async def _checkpoint_session(self, team: Any) -> None:
        """See :func:`backend.server_pause.checkpoint_session`."""
        await server_pause.checkpoint_session(self, team)

    def _drop_pending_for_run(self, run_id: str) -> None:
        """See :func:`backend.server_pause.drop_pending_for_run`."""
        server_pause.drop_pending_for_run(self, run_id)

    def _handle_pause(self, event: Any) -> tuple[list[msg.Message], list[Any], str | None]:
        """See :func:`backend.server_pause.handle_pause`."""
        return server_pause.handle_pause(self, event)

    async def resolve_hitl_batch(
        self, decisions: list[msg.HITLDecision]
    ) -> AsyncIterator[msg.Message]:
        """See :func:`backend.server_hitl.resolve_hitl_batch`."""
        async for proto in server_hitl.resolve_hitl_batch(self, decisions):
            yield proto

    async def resolve_hitl(
        self, requirement_id: str, action: str, choice: str = "once"
    ) -> AsyncIterator[msg.Message]:
        """See :func:`backend.server_hitl.resolve_hitl`."""
        async for proto in server_hitl.resolve_hitl(self, requirement_id, action, choice):
            yield proto

    async def handle_command(self, text: str) -> msg.CommandResult:
        """Process a slash command and return the result."""
        from ember_code.backend.command_handler import CommandHandler

        handler = CommandHandler(self._session)
        result = await handler.handle(text)
        return msg.CommandResult(
            kind=result.kind,
            content=result.content,
            display_content=getattr(result, "display_content", "") or "",
            action=result.action or "",
        )

    # ── Session management ────────────────────────────────────────

    async def list_sessions(self) -> msg.SessionListResult:
        """See :func:`backend.server_sessions.list_sessions`."""
        return await server_sessions.list_sessions(self)

    async def maybe_auto_name_session(self) -> str | None:
        """See :func:`backend.server_sessions.maybe_auto_name_session`."""
        return await server_sessions.maybe_auto_name_session(self)

    async def switch_session(self, session_id: str) -> msg.Info:
        """See :func:`backend.server_sessions.switch_session`."""
        return await server_sessions.switch_session(self, session_id)

    # ── MCP ───────────────────────────────────────────────────────

    async def ensure_mcp(self) -> None:
        """See :func:`backend.server_mcp.ensure_mcp`."""
        await server_mcp.ensure_mcp(self)

    async def toggle_mcp(self, server_name: str, connect: bool) -> msg.Info:
        """See :func:`backend.server_mcp.toggle_mcp`."""
        return await server_mcp.toggle_mcp(self, server_name, connect)

    def get_mcp_status(self) -> list[tuple[str, bool]]:
        """See :func:`backend.server_mcp.get_mcp_status`."""
        return server_mcp.get_mcp_status(self)

    def set_mcp_tool_enabled(
        self, server: str, tool: str, enabled: bool
    ) -> "server_mcp.MCPToolToggleResult":
        """See :func:`backend.server_mcp.set_mcp_tool_enabled`."""
        return server_mcp.set_mcp_tool_enabled(self, server, tool, enabled)

    # ── Permissions ────────────────────────────────────────────────

    def check_permission(self, tool_name: str, func_name: str, tool_args: dict) -> str:
        """See :func:`backend.server_hitl.check_permission`."""
        return server_hitl.check_permission(self, tool_name, func_name, tool_args)

    def save_permission_rule(self, rule: str, level: str) -> None:
        """See :func:`backend.server_hitl.save_permission_rule`."""
        server_hitl.save_permission_rule(self, rule, level)

    def _maybe_persist_choice(self, decision: Any, req: Any) -> None:
        """See :func:`backend.server_hitl.maybe_persist_choice`."""
        server_hitl.maybe_persist_choice(self, decision, req)

    def switch_model(self, model_name: str) -> msg.Info:
        """Switch the active model and persist the choice.

        Two layers of persistence so the choice survives both an
        app restart AND a session resume:

        * **User-level default** — written to
          ``~/.ember/config.yaml`` so any new session opened next
          launch uses this model. Best-effort: a save failure is
          logged but doesn't fail the switch (the in-memory state
          is already updated).
        * **Per-session preference** — written to
          ``state.db``'s ``ember_session_preferences`` table keyed
          by session_id. ``--continue`` consults this on startup
          and overrides the user-level default for the resumed
          session.
        """
        from ember_code.core.config.settings import save_default_model

        old_name = self._session.settings.models.default
        old_cfg = self._session.settings.models.registry.get(old_name, {})
        new_cfg = self._session.settings.models.registry.get(model_name, {})

        self._session.settings.models.default = model_name
        self._session.main_team = self._session._build_main_agent()

        # User-level persistence.
        try:
            save_default_model(model_name)
        except Exception as exc:
            logger.warning("failed to persist model choice to user config: %s", exc)

        # Per-session persistence.
        try:
            self._session_prefs.set_model(self._session.session_id, model_name)
        except Exception as exc:
            logger.debug("failed to persist per-session model preference: %s", exc)

        note = f"Switched to {model_name}"
        # Warn if switching from vision to non-vision with media in history
        if old_cfg.get("vision") and not new_cfg.get("vision"):
            note += (
                "\nNote: previous messages may contain images/files. "
                "Use /clear to reset if you get errors."
            )
        return msg.Info(text=note)

    # ── Login/Logout ──────────────────────────────────────────────

    async def login(self, on_status=None) -> tuple[bool, str]:
        """See :func:`backend.server_auth.login`."""
        return await server_auth.login(self, on_status)

    def reload_cloud_credentials(self) -> msg.StatusUpdate:
        """See :func:`backend.server_auth.reload_cloud_credentials`."""
        return server_auth.reload_cloud_credentials(self)

    def clear_cloud_credentials(self) -> msg.StatusUpdate:
        """See :func:`backend.server_auth.clear_cloud_credentials`."""
        return server_auth.clear_cloud_credentials(self)

    async def get_cloud_plan(self) -> "server_auth.CloudPlan | None":
        """See :func:`backend.server_auth.get_cloud_plan`."""
        return await server_auth.get_cloud_plan(self)

    # ── Status ────────────────────────────────────────────────────

    def get_status(self) -> msg.StatusUpdate:
        """See :func:`backend.server_context.get_status`."""
        return server_context.get_status(self)

    # ── /loop continuation ────────────────────────────────────────

    async def pop_pending_loop_iteration(self) -> "server_loop.LoopAdvance | None":
        """See :func:`backend.server_loop.pop_pending_loop_iteration`."""
        return await server_loop.pop_pending_loop_iteration(self)

    async def cancel_pending_loop(self) -> bool:
        """See :func:`backend.server_loop.cancel_pending_loop`."""
        return await server_loop.cancel_pending_loop(self)

    async def loop_pause(self) -> bool:
        """See :func:`backend.server_loop.loop_pause`."""
        return await server_loop.loop_pause(self)

    async def loop_resume(self) -> str:
        """See :func:`backend.server_loop.loop_resume`."""
        return await server_loop.loop_resume(self)

    async def loop_status(self) -> "server_loop.LoopStatus":
        """See :func:`backend.server_loop.loop_status`."""
        return await server_loop.loop_status(self)

    # ── Compaction ────────────────────────────────────────────────

    async def count_context_tokens(self) -> int:
        """See :func:`backend.server_context.count_context_tokens`."""
        return await server_context.count_context_tokens(self)

    async def compact_if_needed(self, ctx_tokens: int, max_ctx: int) -> msg.SessionCleared | None:
        """See :func:`backend.server_context.compact_if_needed`."""
        return await server_context.compact_if_needed(self, ctx_tokens, max_ctx)

    async def extract_learnings(self, user_msg: str, assistant_msg: str) -> None:
        """See :func:`backend.server_context.extract_learnings`."""
        await server_context.extract_learnings(self, user_msg, assistant_msg)

    # ── Cleanup ───────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """See :func:`backend.server_lifecycle.shutdown`."""
        await server_lifecycle.shutdown(self)

    # ── Accessors for FE (read-only state) ──────────────────────

    def wire_queue_hook(self, queue: list) -> None:
        """Wire queue hooks onto the team.

        - Tool-hook (injector) drains the queue after each tool call so the
          model sees queued text on its next iteration.
        - Post-hook (persister) records those drained items as proper
          user-role history entries before the session is saved.
        """
        from ember_code.core.queue_hook import create_queue_hook

        injector, persister = create_queue_hook(queue=queue)
        team = self._session.main_team
        existing_tool_hooks = team.tool_hooks or []
        team.tool_hooks = [*existing_tool_hooks, injector]
        existing_post_hooks = team.post_hooks or []
        team.post_hooks = [*existing_post_hooks, persister]

    def wire_orchestrate_progress(self, callback) -> None:
        """Set a progress callback on the orchestrate tool."""
        from ember_code.core.tools.orchestrate import OrchestrateTools

        for tool in self._session.main_team.tools or []:
            if isinstance(tool, OrchestrateTools):
                tool._on_progress = callback
                break

    @staticmethod
    async def _close_model_http_client(team: Any) -> None:
        """See :func:`backend.server_run.close_model_http_client`."""
        await server_run.close_model_http_client(team)

    def cancel_agent_run(self, run_id: str) -> CancelAgentRunResult:
        """Cancel a specific sub-agent run by its Agno ``run_id``.

        Used by the team-progress UI when the user wants to stop one
        specialist mid-broadcast without killing the whole team. Agno
        flags the run for cooperative cancellation — the sub-agent
        bails at its next ``await`` boundary, siblings keep going.

        The FE renders a quick toast on ``ok=False``; ``error``
        carries the specific reason (unknown run_id, live cancel
        failure).
        """
        if not run_id:
            return CancelAgentRunResult(ok=False, error="missing run_id")
        try:
            from agno.agent import Agent

            Agent.cancel_run(run_id)
            logger.info("Cancelled sub-agent run %s", run_id)
            return CancelAgentRunResult(ok=True)
        except Exception as exc:
            logger.warning("cancel_agent_run failed: %s", exc)
            return CancelAgentRunResult(ok=False, error=str(exc))

    def get_latest_plan(self) -> LatestPlanResult:
        """Snapshot of the plan store + todos + display state for the
        FE panel.

        ``state`` is ``"pending"`` when a plan exists (the FE
        proves otherwise via ``approve_plan`` / ``dismiss_plan``)
        and empty when no plan has been submitted yet. Never
        inferred from permission mode — a mode flip without an
        explicit user click leaves the plan pending.
        """
        store = getattr(self._session, "plan_store", None)
        todo_store = getattr(self._session, "todo_store", None)
        if store is None:
            return LatestPlanResult()
        snap = store.snapshot()
        latest = snap.latest or ""
        tasks: list[dict] = []
        if todo_store is not None:
            try:
                tasks = todo_store.snapshot()
            except Exception as exc:
                logger.debug("get_latest_plan todo snapshot failed: %s", exc)
        return LatestPlanResult(
            latest=latest,
            history=list(snap.history),
            tasks=tasks,
            state="pending" if latest else "",
        )

    def dispatch_visualization_action(
        self, action: str, params: dict | None = None
    ) -> VisualizationActionResult:
        """User interacted with a component inside a rendered
        json-render spec (Button click, Select change, etc.).

        The FE forwards the action name + params here. Two side
        effects: stash the event on the session so a future
        agent tool can query it, and broadcast a
        ``visualization_action_dispatched`` push so anything
        else (log panels, dev tools) can observe.
        """
        p = dict(params or {})
        # Bounded ring so a chatty UI (e.g. a Slider firing on
        # every drag tick) doesn't grow forever. 32 is generous
        # for the "one-off action after the user reviews a card"
        # use case.
        MAX_ACTIONS = 32
        buf = getattr(self._session, "_visualization_actions", None)
        if buf is None:
            buf = []
            self._session._visualization_actions = buf
        buf.append({"action": action, "params": p})
        if len(buf) > MAX_ACTIONS:
            del buf[: len(buf) - MAX_ACTIONS]
        broadcast = getattr(self._session, "broadcast", None)
        if broadcast is not None:
            with contextlib.suppress(Exception):
                broadcast(
                    "visualization_action_dispatched",
                    {"action": action, "params": p},
                )
        return VisualizationActionResult(ok=True, action=action, params=p)

    def get_todos(self) -> list[dict]:
        """Snapshot of the session's todo list for the todos panel.

        Returns whatever the last ``todo_write`` tool call
        published (in ``activeForm``-camelCase shape, matching
        the SDK payload). Empty list when the store is missing
        (legacy serialised session) or was never written.
        """
        store = getattr(self._session, "todo_store", None)
        if store is None:
            return []
        try:
            return store.snapshot()
        except Exception as exc:
            logger.debug("get_todos snapshot failed: %s", exc)
            return []

    def list_background_processes(self) -> list[dict]:
        """See :func:`backend.server_processes.list_background_processes`."""
        return server_processes.list_background_processes(self)

    def read_process_tail(self, pid: int, tail: int = 200) -> dict:
        """See :func:`backend.server_processes.read_process_tail`."""
        return server_processes.read_process_tail(self, pid, tail)

    async def stop_background_process(self, pid: int) -> dict:
        """See :func:`backend.server_processes.stop_background_process`."""
        return await server_processes.stop_background_process(self, pid)

    def cancel_run(self) -> None:
        """Cancel the currently running agent and kill any foreground process.

        Three mechanisms fire in order:
          1. Kill the active foreground shell subprocess (so a blocking
             ``run_shell_command`` returns immediately).
          2. Flag the run for cooperative cancel via Agno
             (``Agent.cancel_run``) — propagates to the team's main loop
             and any sub-agents that check the flag.
          3. Hard-cancel the asyncio task iterating ``run_message`` —
             unblocks any ``await`` that Agno's cooperative cancel
             can't reach (notably tool calls deep inside specialist
             sub-agents during a ``broadcast`` team).
        """
        from ember_code.core.tools.shell import cancel_foreground

        if cancel_foreground():
            logger.info("Killed foreground process on cancel")

        try:
            from agno.agent import Agent

            team = self._session.main_team
            run_id = getattr(team, "run_id", None)
            if run_id:
                Agent.cancel_run(run_id)
        except Exception as exc:
            logger.debug("Failed to cancel run: %s", exc)

        task = self._current_run_task
        if task and not task.done():
            logger.info("Cancelling run task %s", task.get_name())
            task.cancel()

    @property
    def processing(self) -> bool:
        return self._processing

    @property
    def session_id(self) -> str:
        return self._session.session_id

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def run_timeout(self) -> int:
        return self._settings.models.max_run_timeout

    @property
    def skill_names(self) -> list[str]:
        """Skill names for input autocomplete — FE needs these for the input handler."""
        return [s.name for s in self._session.skill_pool.list_skills()]

    def get_skill_pool(self):
        """Return the skill pool for input autocomplete."""
        return self._session.skill_pool

    async def get_mcp_server_details(self) -> list[dict]:
        """See :func:`backend.server_mcp.get_mcp_server_details`."""
        return await server_mcp.get_mcp_server_details(self)

    async def get_pending_messages(
        self, session_id: str
    ) -> "list[server_context.PendingMessage]":
        """See :func:`backend.server_context.get_pending_messages`."""
        return await server_context.get_pending_messages(self, session_id)

    def upload_attachment(
        self, filename: str, content_base64: str
    ) -> "server_files.UploadAttachmentResult":
        """See :func:`backend.server_files.upload_attachment`."""
        return server_files.upload_attachment(self, filename, content_base64)

    async def get_chat_history(self, session_id: str) -> list[dict]:
        """See :func:`backend.server_history.get_chat_history`."""
        return await server_history.get_chat_history(self, session_id)

    async def search_chat(self, session_id: str, query: str, limit: int = 50) -> list[dict]:
        """See :func:`backend.server_sessions.search_chat`."""
        return await server_sessions.search_chat(self, session_id, query, limit)

    async def truncate_history(
        self, session_id: str, run_id: str
    ) -> "server_context.TruncateHistoryResult":
        """See :func:`backend.server_context.truncate_history`."""
        return await server_context.truncate_history(self, session_id, run_id)

    def get_mcp_servers(self) -> list[dict]:
        """See :func:`backend.server_mcp.get_mcp_servers`."""
        return server_mcp.get_mcp_servers(self)

    async def mcp_connect(self, server_name: str) -> msg.Info:
        """See :func:`backend.server_mcp.mcp_connect`."""
        return await server_mcp.mcp_connect(self, server_name)

    async def mcp_disconnect(self, server_name: str) -> msg.Info:
        """See :func:`backend.server_mcp.mcp_disconnect`."""
        return await server_mcp.mcp_disconnect(self, server_name)

    # ── Agents ─────────────────────────────────────────────────────

    def get_agent_details(self) -> list[AgentInfo]:
        """See :func:`backend.server_panels.get_agent_details`."""
        return server_panels.get_agent_details(self)

    def promote_ephemeral_agent(self, name: str) -> msg.Info:
        """Save an ephemeral agent permanently (called from the panel)."""
        try:
            dest = self._session.pool.promote_ephemeral(name, self._session.project_dir)
        except (KeyError, ValueError, RuntimeError) as e:
            return msg.Info(text=str(e))
        return msg.Info(text=f"Promoted '{name}' to {dest}")

    def discard_ephemeral_agent(self, name: str) -> msg.Info:
        """Delete an ephemeral agent (called from the panel)."""
        try:
            self._session.pool.discard_ephemeral(name)
        except (KeyError, ValueError, RuntimeError) as e:
            return msg.Info(text=str(e))
        return msg.Info(text=f"Discarded ephemeral agent '{name}'.")

    # ── Skills ─────────────────────────────────────────────────────

    # ── Knowledge ──────────────────────────────────────────────────

    async def get_knowledge_status(self) -> KnowledgeStatus:
        """Status snapshot for the knowledge panel header."""
        status = await self._session.knowledge_mgr.status()
        return KnowledgeStatus(
            enabled=status.enabled,
            collection_name=status.collection_name,
            document_count=status.document_count,
            embedder=status.embedder,
        )

    async def knowledge_search(
        self, query: str
    ) -> "list[server_knowledge.KnowledgeHit]":
        """See :func:`backend.server_knowledge.knowledge_search`."""
        return await server_knowledge.knowledge_search(self, query)

    async def knowledge_add(self, source: str) -> msg.Info:
        """See :func:`backend.server_knowledge.knowledge_add`."""
        return await server_knowledge.knowledge_add(self, source)

    async def knowledge_list(self) -> "list[server_knowledge.KnowledgeListEntry]":
        """See :func:`backend.server_knowledge.knowledge_list`."""
        return await server_knowledge.knowledge_list(self)

    async def knowledge_get(self, entry_id: str) -> "server_knowledge.KnowledgeGetResult":
        """See :func:`backend.server_knowledge.knowledge_get`."""
        return await server_knowledge.knowledge_get(self, entry_id)

    def read_file(self, path: str) -> "server_files.ReadFileResult":
        """See :func:`backend.server_files.read_file`."""
        return server_files.read_file(self, path)

    def search_code(
        self, snippet: str, max_results: int = 20
    ) -> "server_search.SearchCodeResult":
        """See :func:`backend.server_search.search_code`."""
        return server_search.search_code(self, snippet, max_results)

    async def knowledge_remove(
        self, entry_id: str
    ) -> "server_knowledge.KnowledgeRemoveResult":
        """See :func:`backend.server_knowledge.knowledge_remove`."""
        return await server_knowledge.knowledge_remove(self, entry_id)

    # ── Hooks ──────────────────────────────────────────────────────

    def get_hooks_details(self) -> list[dict]:
        """See :func:`backend.server_panels.get_hooks_details`."""
        return server_panels.get_hooks_details(self)

    def reload_hooks_rpc(self) -> msg.Info:
        """See :func:`backend.server_panels.reload_hooks_rpc`."""
        return server_panels.reload_hooks_rpc(self)

    # ── CodeIndex ──────────────────────────────────────────────────

    async def codeindex_status(self) -> "server_codeindex.CodeIndexStatus":
        """See :func:`backend.server_codeindex.codeindex_status`."""
        return await server_codeindex.codeindex_status(self)

    async def codeindex_sync(
        self, sha: str | None
    ) -> "server_codeindex.CodeIndexSyncResult":
        """See :func:`backend.server_codeindex.codeindex_sync`."""
        return await server_codeindex.codeindex_sync(self, sha)

    async def codeindex_resync(
        self, sha: str | None
    ) -> "server_codeindex.CodeIndexSyncResult":
        """See :func:`backend.server_codeindex.codeindex_resync`."""
        return await server_codeindex.codeindex_resync(self, sha)

    async def codeindex_clean(self) -> "server_codeindex.CodeIndexCleanResult":
        """See :func:`backend.server_codeindex.codeindex_clean`."""
        return await server_codeindex.codeindex_clean(self)

    async def codeindex_head_breakdown(
        self,
    ) -> "server_codeindex.CodeIndexHeadBreakdown":
        """See :func:`backend.server_codeindex.codeindex_head_breakdown`."""
        return await server_codeindex.codeindex_head_breakdown(self)

    def codeindex_activity(self) -> list[dict]:
        """See :func:`backend.server_codeindex.codeindex_activity`."""
        return server_codeindex.codeindex_activity(self)

    def codeindex_install(self) -> "server_codeindex.CodeIndexInstallResult":
        """See :func:`backend.server_codeindex.codeindex_install`."""
        return server_codeindex.codeindex_install(self)

    # ── Skills ─────────────────────────────────────────────────────

    def get_skill_details(self) -> list[SkillInfo]:
        """See :func:`backend.server_panels.get_skill_details`."""
        return server_panels.get_skill_details(self)

    def get_output_styles(self) -> "server_panels.OutputStylesResult":
        """See :func:`backend.server_panels.get_output_styles`."""
        return server_panels.get_output_styles(self)

    def get_slash_commands(self) -> list[dict]:
        """See :func:`backend.server_panels.get_slash_commands`."""
        return server_panels.get_slash_commands(self)

    # ── Plugins ────────────────────────────────────────────────────

    def get_plugin_contents(self, name: str) -> PluginContents:
        """Detailed inventory of one installed plugin — what skills,
        agents, hooks, MCP servers, and custom tools it bundles, plus
        a short README excerpt if present. Powers the expandable
        plugin card in the panel.
        """
        loader = self._session.plugin_loader
        plugin = next(
            (p for p in loader.list_plugins() if p.name == name),
            None,
        )
        if plugin is None:
            return PluginContents(error=f"Plugin '{name}' not found")
        return _scan_plugin_dir(plugin.root_path, name=name)

    async def preview_plugin(
        self,
        source: str,
        branch: str | None = None,
        subdir: str | None = None,
    ) -> PluginContents:
        """See :func:`backend.server_plugin.preview_plugin`."""
        return await server_plugin.preview_plugin(self, source, branch, subdir)

    def get_plugin_details(self) -> list[PluginInfo]:
        """See :func:`backend.server_plugin.get_plugin_details`."""
        return server_plugin.get_plugin_details(self)

    def set_plugin_enabled(self, name: str, enabled: bool) -> msg.Info:
        """See :func:`backend.server_plugin.set_plugin_enabled`."""
        return server_plugin.set_plugin_enabled(self, name, enabled)

    def install_plugin(self, ref: str, install_ref: str | None = None) -> msg.Info:
        """See :func:`backend.server_plugin.install_plugin`."""
        return server_plugin.install_plugin(self, ref, install_ref)

    def update_plugin(self, name: str, install_ref: str | None = None) -> msg.Info:
        """See :func:`backend.server_plugin.update_plugin`."""
        return server_plugin.update_plugin(self, name, install_ref)

    def remove_plugin(self, name: str) -> msg.Info:
        """See :func:`backend.server_plugin.remove_plugin`."""
        return server_plugin.remove_plugin(self, name)

    def get_marketplaces(self) -> list[MarketplaceInfo]:
        """See :func:`backend.server_plugin.get_marketplaces`."""
        return server_plugin.get_marketplaces(self)

    def add_marketplace(self, url: str) -> msg.Info:
        """See :func:`backend.server_plugin.add_marketplace`."""
        return server_plugin.add_marketplace(self, url)

    def remove_marketplace(self, name: str) -> msg.Info:
        """See :func:`backend.server_plugin.remove_marketplace`."""
        return server_plugin.remove_marketplace(self, name)

    def refresh_marketplaces(self, name: str | None = None) -> msg.Info:
        """See :func:`backend.server_plugin.refresh_marketplaces`."""
        return server_plugin.refresh_marketplaces(self, name)

    async def fire_session_start_hook(self) -> None:
        """Fire the SessionStart hook."""
        from ember_code.core.hooks.events import HookEvent

        with contextlib.suppress(Exception):
            await self._session.hook_executor.execute(
                event=HookEvent.SESSION_START.value,
                payload={"session_id": self._session.session_id},
            )

    async def auto_sync_knowledge(self) -> str | None:
        """Auto-sync knowledge file on startup. Returns status message or None."""
        if self._session.knowledge is None:
            return None
        try:
            result = await self._session.knowledge_mgr.sync_from_file()
            if result:
                return f"Knowledge synced: {result}"
        except Exception as exc:
            logger.debug("knowledge sync_from_file failed (%s)", exc)
        return None

    async def execute_scheduled_task(self, description: str) -> str:
        """See :func:`backend.server_loop.execute_scheduled_task`."""
        return await server_loop.execute_scheduled_task(self, description)

    async def cancel_scheduled_task(self, task_id: str) -> msg.Info:
        """See :func:`backend.server_loop.cancel_scheduled_task`."""
        return await server_loop.cancel_scheduled_task(self, task_id)

    async def get_scheduled_tasks(self, include_done: bool = True) -> list:
        """See :func:`backend.server_loop.get_scheduled_tasks`."""
        return await server_loop.get_scheduled_tasks(self, include_done)

    def start_scheduler(
        self,
        on_task_started=None,
        on_task_completed=None,
    ) -> Any:
        """See :func:`backend.server_loop.start_scheduler`."""
        return server_loop.start_scheduler(self, on_task_started, on_task_completed)

    def toggle_verbose(self) -> bool:
        """Toggle verbose mode. Returns new state."""
        self._settings.display.show_routing = not self._settings.display.show_routing
        return self._settings.display.show_routing
