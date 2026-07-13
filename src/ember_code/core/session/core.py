"""Session core — wires up subsystems and handles messages."""

import asyncio
import contextlib
import getpass
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from agno.agent import Agent
from agno.compression.manager import CompressionManager
from pydantic import BaseModel

from ember_code.core.auth.credentials import CloudCredentials
from ember_code.core.code_index import CodeIndex, CodeIndexSyncManager
from ember_code.core.config.cloud_models import fetch_cloud_models, merge_into_registry
from ember_code.core.config.models import ModelRegistry
from ember_code.core.config.permission_eval import PermissionEvaluator
from ember_code.core.config.permissions import PermissionGuard
from ember_code.core.config.settings import Settings
from ember_code.core.config.tool_permissions import ToolPermissions
from ember_code.core.guardrails.runner import GuardrailRunner
from ember_code.core.hooks.events import HookEvent
from ember_code.core.hooks.executor import HookExecutor
from ember_code.core.hooks.loader import HookLoader
from ember_code.core.knowledge.manager import KnowledgeManager
from ember_code.core.lsp import LspServerManager, load_lsp_config
from ember_code.core.mcp.config import MCPConfigLoader
from ember_code.core.monitors import MonitorManager, load_monitor_config
from ember_code.core.output_styles import discover_output_styles
from ember_code.core.plugins import PluginLoader, load_state
from ember_code.core.sub_agent_hitl import SubAgentHITLCoordinator
from ember_code.core.tools.plan import PlanStore
from ember_code.core.tools.todo import TodoStore
from ember_code.core.utils.context import ensure_memory_dir
from ember_code.core.session.agent_factory import (
    create_guardrails,
    create_reasoning_tools,
)
from ember_code.core.session import broadcast as _broadcast_ops
from ember_code.core.session import compact_ops as _compact_ops
from ember_code.core.session.compact_ops import ContextBreakdown
from ember_code.core.session.loop_ops import LoopAdvance
from ember_code.core.session.plan_ops import PlanDecisionResult
from ember_code.core.session import loop_ops as _loop_ops
from ember_code.core.session import plan_ops as _plan_ops
from ember_code.core.session import startup_ops as _startup_ops
from ember_code.core.session import state_ops as _state_ops
from ember_code.core.session.mcp_ops import (
    auto_connect_mcps,
    disconnect_removed_mcps,
)

# Backwards-compat aliases — `test_session.py` patches
# `ember_code.core.session.core._create_reasoning_tools` /
# `_create_guardrails`. The factories moved to
# `session.agent_factory`; these aliases keep the test-patch
# targets stable so `_start_patches` continues to work.
_create_reasoning_tools = create_reasoning_tools
_create_guardrails = create_guardrails
from ember_code.core.session.event_log_schema import SessionEvent
from ember_code.core.hooks.tool_hook import ToolEventHook
from ember_code.core.init import initialize_project
from ember_code.core.learn import create_learning_machine
from ember_code.core.loop import (
    LoopProgressStore,
    LoopStore,
)
from ember_code.core.mcp.client import MCPClientManager
from ember_code.core.memory.manager import setup_db
from ember_code.core.pool import AgentPool
from ember_code.core.prompts import load_prompt
from ember_code.core.session.knowledge_ops import SessionKnowledgeManager
from ember_code.core.session.memory_ops import SessionMemoryManager
from ember_code.core.session.persistence import SessionPersistence
from ember_code.core.skills.loader import SkillPool
from ember_code.core.tools.registry import ToolRegistry
from ember_code.core.utils.audit import AuditLogger
from ember_code.core.utils.context import load_project_context
from ember_code.core.utils.display import print_error
from ember_code.core.utils.response import extract_response_text
from ember_code.core.utils.rules_index import RulesIndex
from ember_code.core.workspace import WorkspaceManager

logger = logging.getLogger(__name__)


class PluginReloadCounts(BaseModel):
    """Return shape for :meth:`Session.reload_plugins` — a summary
    of how many items were re-wired after the disk scan.

    Callers surface these to the user in a hot-reload confirmation
    ("Active now — N skill(s), M agent(s), K hook(s)"). Modelling
    the shape once here keeps every consumer type-safe (Rule 1)."""

    plugins: int
    skills: int
    agents: int
    hooks: int


def _log_run_messages_debug(team: Any) -> None:
    """Dump messages from the team's last run at DEBUG level.

    Used for diagnosing tool-result delivery issues — surfaces
    role / tool_call_id / tool_calls / compression state /
    from_history flag on every message, plus a 200-char preview
    of ``content``. Silent on any exception so an introspection
    hiccup can't break the response path.
    """
    try:
        rr = getattr(team, "run_response", None)
        if rr is None:
            logger.debug("RUN_MESSAGES: no run_response")
            return
        messages = getattr(rr, "messages", None)
        if not messages:
            logger.debug("RUN_MESSAGES: no messages in run_response")
            return
        logger.debug("RUN_MESSAGES: %d messages total", len(messages))
        for i, msg in enumerate(messages):
            role = getattr(msg, "role", "?")
            content = getattr(msg, "content", None)
            tool_calls = getattr(msg, "tool_calls", None)
            tool_call_id = getattr(msg, "tool_call_id", None)
            compressed = getattr(msg, "compressed_content", None)
            from_hist = getattr(msg, "from_history", False)

            content_str = str(content) if content is not None else "<None>"
            preview = content_str[:200]
            if len(content_str) > 200:
                preview += f"... ({len(content_str)} total)"

            extras = []
            if tool_call_id:
                extras.append(f"tcid={tool_call_id}")
            if tool_calls:
                names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
                extras.append(f"calls={names}")
            if compressed is not None:
                extras.append(f"COMPRESSED({len(str(compressed))}ch)")
            if from_hist:
                extras.append("HIST")

            logger.debug(
                "  MSG[%d] role=%-9s %s | %s",
                i,
                role,
                " ".join(extras),
                preview,
            )
    except Exception as e:
        logger.debug("RUN_MESSAGES: error: %s", e)


class Session:
    """Manages a single igni session with all subsystem integrations.

    Session persistence and chat history are delegated entirely to Agno's
    native ``db`` / ``session_id`` mechanism.  The main team and all its
    members receive the same ``db`` and ``session_id``, so all turns are
    automatically persisted and restored.
    """

    def __init__(
        self,
        settings: Settings,
        project_dir: Path | None = None,
        resume_session_id: str | None = None,
        additional_dirs: list[Path] | None = None,
        pre_knowledge: Any | None = None,
    ):
        self.settings = settings

        # Merge models discovered in the Ember Cloud key pool into the
        # local registry. Runs on session start so the first ``/model``
        # invocation already sees fresh values; the picker callers
        # (text-mode + TUI) also re-fetch on open so adding a key on
        # the portal is reflected without restarting the CLI.
        self.refresh_cloud_models()

        self.project_dir = project_dir or Path.cwd()
        self.workspace = WorkspaceManager(self.project_dir, additional_dirs)
        self.session_id = resume_session_id or str(uuid.uuid4())[:8]
        self.session_named = bool(resume_session_id)
        self.user_id = getpass.getuser()

        self._init_loop_state()
        self._init_per_session_scratch()

        # ── First-run initialization (agents, skills, hooks, ember.md) ─
        initialize_project(self.project_dir)

        # ── Storage (Agno AsyncBaseDb) ────────────────────────────────
        self.db = setup_db(settings, project_dir=self.project_dir)

        self._init_knowledge(settings, pre_knowledge)

        # ── Permission Guard ─────────────────────────────────────────
        self.permission_guard = PermissionGuard(settings)

        # ── Audit Logger ─────────────────────────────────────────────
        self.audit = AuditLogger(settings)

        self._init_plugins_output_styles_hooks(settings)

        self._init_project_context(settings)

        self._init_codeindex(settings)
        self._init_agent_and_skill_pools(settings)

        # ── Context window (for compaction threshold, capped by setting) ──
        self._context_window = min(
            ModelRegistry(settings).get_context_window(),
            settings.models.max_context_window,
        )

        # ── Learning (Agno LearningMachine) ─────────────────────────
        self._learning = create_learning_machine(settings, self.db)

        # ── Ember Cloud auth (cloud-routed models + status indicator) ─
        self._cloud = CloudCredentials(settings.auth.credentials_file)
        self._cloud_server_url = settings.api_url

        self._init_mcp_client_manager()
        self._init_lsp_and_monitors()

        # ── Guardrails ───────────────────────────────────────────────
        self.guardrail_runner = GuardrailRunner(settings)

        # ── Sub-agent HITL bridge ────────────────────────────────────
        # Sub-agents spawned by the orchestrator emit RunPausedEvents
        # inside the parent's tool execution; without this coordinator
        # the pauses are lost and tool calls return empty. See
        # core/sub_agent_hitl.py.
        self.sub_agent_hitl = SubAgentHITLCoordinator()

        # ── Delegated managers ───────────────────────────────────────
        self.persistence = SessionPersistence(self.db, self.session_id)
        self.memory_mgr = SessionMemoryManager(self.db, settings, self.user_id)
        self.knowledge_mgr = SessionKnowledgeManager(self.knowledge, settings, self.project_dir)
        # Share knowledge_mgr with the pool so all sub-agents get the toolkit.
        self.pool._knowledge_mgr = self.knowledge_mgr if self.knowledge else None

        # ── Main Agent (single agent with all tools + orchestration) ──
        self.main_team = self._build_main_agent()

    def _init_per_session_scratch(self) -> None:
        """Set up the per-session scratch state populated by tools /
        the run loop:

        * :class:`TodoStore` — ``todo_write`` tool's list (CC's
          ``TodoWrite`` parity).
        * :class:`PlanStore` — ``exit_plan_mode`` submissions (row
          50).
        * Append-only event log — BE events Agno's message history
          doesn't capture (visualizer specs, etc.). Each entry is
          a typed :class:`SessionEvent` (Rule 1). Persisted to
          ``session_data.event_log`` on every append.
        * ``_plan_mode_attempt`` — validator counter that resets
          on ``enter_plan_mode`` and caps thin ``exit_plan_mode``
          submissions.
        * Output-style placeholders — real values land in
          :meth:`_init_plugins_output_styles_hooks`; the
          placeholders here prevent AttributeError on anything
          between this init and that one that touches them.
        * Broadcast callback list + post-run broadcast queue —
          FE push channels for plan cards / mode badges. Empty
          when no transport is wired (headless mode).

        Also pre-creates the per-project memory directory so the
        agent's first ``save_file`` doesn't fail on "parent
        doesn't exist".
        """
        self.todo_store = TodoStore()
        self.plan_store = PlanStore()
        self.event_log: list[SessionEvent] = []
        self._event_seq: int = 0
        self._plan_mode_attempt: int = 0
        ensure_memory_dir(self.project_dir)
        self.output_styles: dict = {}
        self._active_output_style: str = ""
        self._broadcast_callbacks: list = []
        self._pending_post_run_broadcasts: list[tuple[str, dict]] = []

    def _init_project_context(self, settings: Settings) -> None:
        """Load top-level project instructions + construct the
        :class:`RulesIndex`.

        Top-level context (``ember.md`` / ``CLAUDE.md`` at the
        project root) is loaded eagerly here so it can be baked
        into the main-agent's system prompt.

        Subdirectory rules (``ember.md`` deeper in the tree) are
        NOT pre-loaded — they're discovered lazily by
        :class:`ToolEventHook` when the agent actually touches a
        file in those areas. This keeps the system prompt small
        for repos with many service folders while still
        delivering scoped rules at the moment they become
        relevant.
        """
        self.project_instructions = load_project_context(
            self.project_dir,
            settings.context.project_file,
            read_claude_md=settings.rules.cross_tool_support,
        )
        self.rules_index = RulesIndex(
            self.project_dir,
            read_claude_md=settings.rules.cross_tool_support,
        )

    def _init_loop_state(self) -> None:
        """Initialize the six ``/loop`` fields to their fresh-boot
        defaults plus construct the two stores.

        These are memory-side mirrors of the persisted
        ``loop_state`` row. All mutations go through ``start_loop``
        / ``advance_loop`` / ``cancel_loop`` so the row stays in
        lockstep. On session restart, ``load_persisted_loop_state``
        (called from ``BackendServer.startup``) hydrates the fields
        from the row (if any) and flips ``loop_paused=True`` so the
        FE renders the "R resume" hint instead of auto-advancing.

        Field semantics:

        * ``pending_loop_prompt`` — active iteration text, or ``None``
          when no loop is running.
        * ``loop_iteration_index`` — 1-based counter of iterations
          already dispatched.
        * ``loop_iterations_remaining`` — safety-net budget for
          future iterations.
        * ``loop_run_id`` — uuid4 minted per fresh start; keys the
          :class:`LoopProgressStore` rows so a new run can't see
          the previous run's progress.
        * ``loop_cap_explicit`` — True when the user supplied
          ``/loop N <prompt>``; the panel then renders ``N / M``
          and we terminate at the cap. False → safety-net cap.
        * ``loop_paused`` — dormant (persisted, not firing) vs.
          actively pumping. Guards the FE's cancel-on-non-/loop
          check so typing after a restart doesn't wipe a resumable
          loop.
        """
        self.pending_loop_prompt: str | None = None
        self.loop_iteration_index: int = 0
        self.loop_iterations_remaining: int = 0
        self.loop_run_id: str | None = None
        self.loop_cap_explicit: bool = False
        self.loop_paused: bool = False
        self.loop_store = LoopStore(project_dir=self.project_dir)
        self.loop_progress_store = LoopProgressStore(project_dir=self.project_dir)

    def _init_codeindex(self, settings: Settings) -> None:
        """Construct :class:`CodeIndex` + :class:`CodeIndexSyncManager`
        eagerly and compute the ``_codeindex_available`` flag.

        Runs BEFORE :meth:`_init_agent_and_skill_pools` because the
        pool consults the flag to pick CodeIndex-first prompt
        variants (``<name>.codeindex.md`` vs ``<name>.md``). The
        main-agent prompt loader uses the same flag. Deriving it
        once here avoids re-computing "does HEAD have a populated
        chroma?" in multiple places later in the boot sequence.
        """
        self.code_index = CodeIndex(project=self.project_dir, data_dir=settings.storage.data_dir)
        self.code_index_sync = CodeIndexSyncManager.from_settings(
            settings, project_dir=self.project_dir, code_index=self.code_index
        )
        _head_sha = self.code_index_sync.current_sha()
        self._codeindex_available = bool(_head_sha and self.code_index.has_commit(_head_sha))

    def _init_mcp_client_manager(self) -> None:
        """Construct :class:`MCPClientManager` and merge in
        plugin-bundled MCP configs.

        Plugin-bundled MCP servers merge into
        ``mcp_manager.configs`` with names prefixed
        ``<plugin>:<server>`` — they're available for
        ``connect()`` like any other server, and the panel
        surfaces them grouped under the plugin's name.
        ``_mcp_initialized`` starts False; the first
        :meth:`ensure_mcp` flips it to True and connects any
        configured servers marked ``auto_connect=True``.
        """
        self.mcp_manager = MCPClientManager(self.project_dir)
        self.plugin_loader.apply_to_mcp(
            MCPConfigLoader(self.project_dir),
            self.mcp_manager.configs,
            disabled=self._disabled_plugins,
        )
        self._mcp_initialized = False

    def _init_knowledge(self, settings: Settings, pre_knowledge: Any | None) -> None:
        """Wire up the Chroma-backed knowledge index (if enabled).

        Three paths:

        1. ``pre_knowledge`` explicit → use as-is (skips re-load;
           used by CLI callers who already built the index for
           model warmup).
        2. ``settings.knowledge.enabled`` → construct via
           :class:`KnowledgeManager` (cheap — the embedder is a
           shared singleton, no model download here).
        3. Otherwise → ``self.knowledge = None``.

        ``_knowledge_ready`` is set immediately (construction is
        eager), so callers polling it don't block on session
        boot. ``_knowledge_error`` stays ``None`` until a
        background operation flips it.
        """
        self._knowledge_error: str | None = None
        self._knowledge_ready = threading.Event()
        self._knowledge_ready.set()
        if pre_knowledge is not None:
            self.knowledge = pre_knowledge
            logger.info("Knowledge: using pre-loaded instance")
        elif settings.knowledge.enabled:
            self.knowledge = KnowledgeManager(
                settings, project_dir=self.project_dir
            ).create_knowledge()
        else:
            self.knowledge = None
            logger.info("Knowledge: disabled in settings")

    def _init_agent_and_skill_pools(self, settings: Settings) -> None:
        """Construct :class:`AgentPool` + :class:`SkillPool` from the
        current plugin set.

        Agent pool is built EMPTY of MCP tools — MCP connects
        asynchronously post-``startup``, and the pool is rebuilt
        with real MCP clients then. The initial ``build_agents``
        gives us usable Agents that the main team can construct
        against, so the session is functional before MCP is ready.

        Both pools:

        1. Construct a fresh instance.
        2. Load disk definitions.
        3. Apply plugin contributions (filtered by disabled set).

        The agent pool additionally optionally initialises ephemeral
        agents (when ``orchestration.generate_ephemeral`` is set)
        and calls ``build_agents``. Shared: ``self.db`` +
        ``self.broadcast`` are threaded into the pool so paused
        sub-agent runs land in the session's store and
        broadcast-emitting tools reach attached clients.
        """
        self.pool = AgentPool(db=self.db, broadcast=self.broadcast)
        self.pool.load_definitions(
            settings, self.project_dir, codeindex_available=self._codeindex_available
        )
        self.plugin_loader.apply_to_agents(self.pool, disabled=self._disabled_plugins)
        if settings.orchestration.generate_ephemeral:
            self.pool.init_ephemeral(
                self.project_dir, settings.orchestration.max_ephemeral_per_session
            )
        self.pool.build_agents()

        self.skill_pool = SkillPool()
        self.skill_pool.load_all(self.project_dir, settings.skills.cross_tool_support)
        self.plugin_loader.apply_to_skills(self.skill_pool, disabled=self._disabled_plugins)

    def _init_lsp_and_monitors(self) -> None:
        """Construct :class:`LspServerManager` + :class:`MonitorManager`
        from the current plugin set.

        Both managers scan the same "enabled plugin roots" but
        launch differently:

        * LSP is **lazy** — each server's ``start()`` fires on the
          first ``lsp_query``. The manager exists even when zero
          servers are configured so callers can call
          ``list_servers()`` without a None-guard.
        * Monitors are **eager** — the whole point is they're
          already running by the time the agent asks. Construction
          here is cheap; ``start_all`` is called from the session
          entrypoint once the event loop is ready.
        """
        # LSP servers.
        lsp_plugin_roots = self.plugin_loader.collect_lsp_roots(
            disabled=self._disabled_plugins,
        )
        lsp_configs = load_lsp_config(
            self.project_dir,
            plugin_roots=lsp_plugin_roots,
        )
        self.lsp_manager = LspServerManager(lsp_configs, self.project_dir)

        # Plugin monitors.
        monitor_plugin_roots = self.plugin_loader.collect_monitor_roots(
            disabled=self._disabled_plugins,
        )
        monitor_configs = load_monitor_config(
            self.project_dir,
            plugin_roots=monitor_plugin_roots,
        )
        self.monitor_manager = MonitorManager(monitor_configs, self.project_dir)

    def _init_plugins_output_styles_hooks(self, settings: Settings) -> None:
        """Set up the three interlocking subsystems that need to
        run in a fixed order:

        1. **Plugin discovery** first — plugins contribute hooks
           AND output styles, so both later steps need
           ``self.plugin_loader`` populated. Managed plugins are
           always enabled (org-enforced), so the disabled set is
           the user's ``plugins.json`` minus that guardrail.
        2. **Output-style discovery** next (independent of hooks
           but reads plugin roots for the "plugin tier"). Active
           style defaults to ``"default"`` when present, else the
           first alphabetically, else ``""`` (none configured).
        3. **Hooks** last — merge project hooks with plugin
           contributions, then construct the executor. The
           ``asyncRewake`` code-2 path fires ``_queue_rewake``, so
           the queue is set up here (canonical typed declaration).

        Called from ``__init__``; also idempotently rebuildable
        via ``reload_plugins`` (which drops + reruns the whole
        block on hot-reload).
        """
        # ── Plugin discovery ────────────────────────────────────────
        self.plugin_state = load_state(settings.storage.data_dir)
        self.plugin_loader = PluginLoader()
        self.plugin_loader.load_all(self.project_dir)
        managed_plugins = {p.name for p in self.plugin_loader.list_plugins() if p.is_managed}
        self._disabled_plugins = set(self.plugin_state.disabled) - managed_plugins

        # ── Output-style discovery (row 52) ─────────────────────────
        plugin_style_roots = [
            (p.root_path, p.name)
            for p in self.plugin_loader.list_plugins()
            if p.name not in self._disabled_plugins
        ]
        self.output_styles = discover_output_styles(
            self.project_dir,
            plugin_roots=plugin_style_roots,
            read_claude=settings.rules.cross_tool_support,
        )
        if "default" in self.output_styles:
            self._active_output_style = "default"
        elif self.output_styles:
            self._active_output_style = sorted(self.output_styles)[0]

        # ── Hooks ────────────────────────────────────────────────────
        self._hook_loader = HookLoader(
            self.project_dir, cross_tool_support=settings.hooks.cross_tool_support
        )
        self.hooks_map = self._hook_loader.load()
        # Plugins prepend per event so project hooks still run last.
        self.plugin_loader.apply_to_hooks(
            self._hook_loader,
            self.hooks_map,
            disabled=self._disabled_plugins,
        )
        # ``asyncRewake`` hooks (code-2 exit) fire ``_queue_rewake``.
        # The queue is the CANONICAL typed declaration — later
        # re-inits in ``_maybe_reinit_executor`` branches use bare
        # assignment so mypy doesn't complain about redefinition.
        self._pending_reminders: list[str] = []
        self.hook_executor = HookExecutor(
            self.hooks_map,
            mcp_resolver=self._mcp_resolver,
            rewake_callback=self._queue_rewake,
        )

    @property
    def cloud_connected(self) -> bool:
        """Whether the session is authenticated with Ember Cloud."""
        return self._cloud.is_authenticated

    @property
    def cloud_org_id(self) -> str | None:
        """The organization ID from the Ember Cloud JWT."""
        return self._cloud.org_id

    @property
    def cloud_org_name(self) -> str | None:
        """The organization display name from the Ember Cloud JWT."""
        return self._cloud.org_name

    def refresh_cloud_models(self) -> int:
        """Best-effort: fetch the cloud key pool's catalogue and merge
        into ``settings.models.registry``. Returns the number of newly
        added entries.

        Silently no-ops when:
        * the user isn't logged in (no cloud token),
        * ``api_url`` is unreachable / times out / non-200,
        * any other transport error.

        Safe to call multiple times — same-name entries are skipped so
        a user-edited registry survives, and re-fetches are idempotent.
        Never blocks more than ``_FETCH_TIMEOUT_SECONDS`` on the
        network — kept tight because the picker invokes this on open.
        """
        token = CloudCredentials(self.settings.auth.credentials_file).access_token
        if not token:
            return 0
        models = fetch_cloud_models(self.settings.api_url, token)
        if not models:
            return 0
        added = merge_into_registry(self.settings.models.registry, models, self.settings.api_url)
        if added:
            logger.info("Merged %d cloud model(s) into the local registry", added)
        # Auto-pick the first entry as the default if nothing else
        # has set it. Lets a brand-new install reach a usable state
        # right after login without a hardcoded fallback name —
        # whatever the server returns first is the choice.
        if not self.settings.models.default and self.settings.models.registry:
            self.settings.models.default = next(iter(self.settings.models.registry))
            logger.info(
                "Auto-selected default model from cloud discovery: %s",
                self.settings.models.default,
            )
        return added

    # ── /loop state helpers ──────────────────────────────────────
    # Implementation lives in ``session/loop_ops.py`` — each method
    # here is a thin wrapper around the module-level function that
    # takes ``self`` as an explicit session argument. Keeps existing
    # call sites (slash command, LoopTools, run_controller's
    # ``_check_loop_continuation``) unchanged.

    async def load_persisted_loop_state(self) -> None:
        """See :func:`session.loop_ops.load_persisted_loop_state`."""
        await _loop_ops.load_persisted_loop_state(self)

    async def start_loop(
        self,
        prompt: str,
        max_iter: int,
        *,
        immediate: bool,
        cap_explicit: bool,
    ) -> str:
        """See :func:`session.loop_ops.start_loop`."""
        return await _loop_ops.start_loop(
            self, prompt, max_iter, immediate=immediate, cap_explicit=cap_explicit
        )

    async def advance_loop(self) -> LoopAdvance | None:
        """See :func:`session.loop_ops.advance_loop`."""
        return await _loop_ops.advance_loop(self)

    async def cancel_loop(self) -> bool:
        """See :func:`session.loop_ops.cancel_loop`."""
        return await _loop_ops.cancel_loop(self)

    async def pause_loop(self) -> bool:
        """See :func:`session.loop_ops.pause_loop`."""
        return await _loop_ops.pause_loop(self)

    async def resume_loop(self) -> str | None:
        """See :func:`session.loop_ops.resume_loop`."""
        return await _loop_ops.resume_loop(self)

    async def _persist_loop_state(self) -> None:
        """See :func:`session.loop_ops._persist_loop_state`."""
        await _loop_ops._persist_loop_state(self)

    def reload_hooks(self) -> int:
        """Reload hooks from settings files. Returns the number of hooks loaded."""
        self.hooks_map = self._hook_loader.load()
        # ``asyncRewake`` hooks fire ``_queue_rewake`` from
        # background tasks when they exit with code 2. Initialise
        # the queue here so the executor's callback always has a
        # destination, regardless of which __init__ branch built
        # the executor. Re-init here (without annotation — the
        # canonical typed declaration is at the top of __init__)
        # so a branch that skipped the top path still has an empty
        # queue instead of a missing attribute.
        self._pending_reminders = []
        self.hook_executor = HookExecutor(
            self.hooks_map,
            mcp_resolver=self._mcp_resolver,
            rewake_callback=self._queue_rewake,
        )
        # Recreate tool event hook on the team
        tool_event_hook = self._create_tool_event_hook()
        if self.main_team:
            # Replace any existing ToolEventHook in the team's tool_hooks
            existing = self.main_team.tool_hooks or []
            self.main_team.tool_hooks = [h for h in existing if not isinstance(h, ToolEventHook)]
            self.main_team.tool_hooks.append(tool_event_hook)
        count = sum(len(hl) for hl in self.hooks_map.values())
        return count

    def reload_plugins(self) -> PluginReloadCounts:
        """Hot-reload plugin contents from disk — no session restart.

        Re-scans every plugin root (``~/.ember/plugins``, ``~/.claude/plugins``,
        and project-local equivalents) and re-applies each enabled
        plugin's bundled contents to the four wiring points:

        * **Hooks** — rebuilt via ``_hook_loader`` then merged.
        * **Skills** — fresh :class:`SkillPool` reload from disk.
        * **Agents** — fresh :class:`AgentPool` rebuilt; ``main_team``
          is rebuilt at the end so the new agents are attached.
        * **MCP server configs** — merged into ``mcp_manager.configs``.
          Connections aren't auto-started — the user can ``/mcp
          connect`` to bring the new servers online (or restart for
          auto-connect behavior).

        The main team rebuild happens at the end so any new tools
        contributed by ``<plugin>/tools/*.py`` are picked up by the
        live agent on its next message.

        Safe to call mid-session because slash commands (which are
        the only callers today) only run when ``_processing`` is
        false; rebuilding the team during an in-flight agent run
        would otherwise drop streaming state.

        Returns a count dict for the caller's chat confirmation:
        ``{"plugins", "skills", "agents", "hooks"}``.
        """
        # Full plugin / output-style / hooks / pool re-init —
        # matches the constructor's ordering exactly so a hot-
        # reload produces the same end-state as a fresh session
        # boot. As a nice side-effect, this refreshes
        # ``output_styles`` too (which the pre-DRY code missed).
        # The old agent pool's runtime state (active runs) lives
        # on Agno's shared ``db``, so re-assigning ``self.pool``
        # is safe.
        self._init_plugins_output_styles_hooks(self.settings)
        self._init_agent_and_skill_pools(self.settings)

        self._reapply_plugin_mcp_configs()

        # Rebuild the main team so newly-bundled custom tools
        # (``<plugin>/tools/*.py``) and the refreshed agent pool are
        # visible to the live agent.
        self.main_team = self._build_main_agent()

        return PluginReloadCounts(
            plugins=len(self.plugin_loader.list_plugins()),
            skills=len(self.skill_pool.list_skills()),
            agents=len(self.pool.list_agents()),
            hooks=sum(len(hl) for hl in self.hooks_map.values()),
        )

    def _reapply_plugin_mcp_configs(self) -> None:
        """Sync ``mcp_manager.configs`` with the current enabled-plugin
        set, disconnecting removed servers + auto-connecting added
        ones in the background.

        MCP is symmetric in both directions. Enabling a plugin
        wires its servers in + auto-connects them; disabling a
        plugin wires them OUT + disconnects them. Without the
        disable side, a user who turns off a plugin sees its
        skills/agents/hooks disappear but the MCP server keeps
        running and showing up in ``/mcp`` — confusing state.

        ``apply_to_mcp`` only adds (first-wins); for the disable
        case we need to *remove* stale entries first. Algorithm:

        1. Identify every config currently in ``configs`` whose
           name prefix matches a known plugin (i.e. was added by a
           previous ``apply_to_mcp``). User-configured servers (no
           plugin prefix) stay untouched.
        2. Wipe those plugin-contributed entries.
        3. Re-apply with the *current* disabled set — only enabled
           plugins re-add their configs.
        4. Diff the snapshot: added = present after, missing
           before; removed = present before, missing after.
        5. Disconnect removed servers (auto-handles the "disable
           plugin → kill its MCP" case). The "what if two plugins
           use the same server" concern is naturally handled by
           the ``<plugin>:<server>`` naming — each plugin's
           contribution is independently addressable.
        6. Auto-connect added servers in the background.
        """
        plugin_name_prefixes = tuple(f"{p.name}:" for p in self.plugin_loader.list_plugins())
        previously_plugin_owned = {
            name
            for name in self.mcp_manager.configs
            if any(name.startswith(p) for p in plugin_name_prefixes)
        }
        for name in previously_plugin_owned:
            self.mcp_manager.configs.pop(name, None)
        self.plugin_loader.apply_to_mcp(
            MCPConfigLoader(self.project_dir),
            self.mcp_manager.configs,
            disabled=self._disabled_plugins,
        )
        now_plugin_owned = {
            name
            for name in self.mcp_manager.configs
            if any(name.startswith(p) for p in plugin_name_prefixes)
        }
        added_mcp_names = now_plugin_owned - previously_plugin_owned
        removed_mcp_names = previously_plugin_owned - now_plugin_owned

        if removed_mcp_names:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._disconnect_removed_mcps(removed_mcp_names))
            except RuntimeError:
                logger.debug(
                    "No running loop — skipping MCP disconnect for: %s",
                    sorted(removed_mcp_names),
                )

        if added_mcp_names:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._auto_connect_mcps(added_mcp_names))
            except RuntimeError:
                logger.debug(
                    "Skipping MCP auto-connect (no running loop); use /mcp connect to start: %s",
                    sorted(added_mcp_names),
                )

    async def _disconnect_removed_mcps(self, names: set[str]) -> None:
        """Thin wrapper around
        :func:`session.mcp_ops.disconnect_removed_mcps` — kept as a
        method so `create_task(self._disconnect_removed_mcps(...))`
        call sites don't need a rewrite."""
        await disconnect_removed_mcps(self, names)

    async def _auto_connect_mcps(self, names: set[str]) -> None:
        """Thin wrapper around
        :func:`session.mcp_ops.auto_connect_mcps` — kept as a
        method so `create_task(self._auto_connect_mcps(...))` call
        sites don't need a rewrite."""
        await auto_connect_mcps(self, names)

    # ── Main Agent setup ────────────────────────────────────────────

    # Tools the main team ALWAYS gets — the shell-first core. Bash
    # handles search/find/list/read directly (``rg``, ``find``,
    # ``cat``, etc.); Edit/Write stay for surgical changes and new
    # files because shell-based alternatives (``sed -i``, here-doc
    # rewrites) are fragile. Grep/Glob/Read/LS toolkits intentionally
    # omitted — they overlapped with shell and confused the model
    # (v0.4.0 / commit 7e50705). See CLAUDE_CODE_PARITY.md row 22.
    #
    # Implications worth knowing about:
    # * The main team has NO ``Read`` tool. Hook matchers targeting
    #   ``read_file`` will never fire on the main team — use
    #   ``run_shell_command`` instead.
    # * Sub-agents CAN opt into Read/Grep/Glob via their frontmatter
    #   ``tools:`` allowlist (see ``.ember/agents/<name>.md``).
    # * Granular permissions on Read (e.g. ``deny: Read(.env)``) are
    #   ineffective at this layer — ``.env`` protection comes from
    #   ``ToolEventHook``'s Bash-command parsing instead.
    _MAIN_CORE_TOOLS: tuple[str, ...] = (
        "Write",
        "Edit",
        "Bash",
        "Schedule",
        "NotebookEdit",
    )

    def _resolve_main_tool_names(self, registry: "ToolRegistry") -> list[str]:
        """Compose the main team's toolkit, honouring per-session
        flags (web permissions, CodeIndex availability). Extracted
        from ``_build_main_agent`` so the shell-first composition
        can be pinned by a unit test without spinning up a full
        agent — see ``tests/test_session.py``.
        """
        tool_names: list[str] = list(self._MAIN_CORE_TOOLS)
        web_allowed = self.settings.permissions.web_search != "deny"
        fetch_allowed = self.settings.permissions.web_fetch != "deny"
        if web_allowed:
            try:
                registry.resolve(["WebSearch"])
                tool_names.append("WebSearch")
            except (ImportError, ValueError):
                pass
        if fetch_allowed:
            try:
                registry.resolve(["WebFetch"])
                tool_names.append("WebFetch")
            except (ImportError, ValueError):
                pass
        # CodeIndex tools are only exposed when there's a usable
        # local chroma index for the current git HEAD. Without
        # one, ``codeindex_search`` would return empty results and
        # waste a tool slot in the agent's catalog — hide it
        # entirely. The ``self._codeindex_available`` flag was set
        # in ``__init__`` before ``pool.load_definitions`` ran (so
        # the pool could pick the right ``<name>.codeindex.md``
        # vs ``<name>.md`` variant per agent).
        if self._codeindex_available:
            tool_names.append("CodeIndex")
        return tool_names

    def _build_main_agent(self) -> Agent:
        """See :func:`session.agent_builder.build_main_agent`."""
        from ember_code.core.session.agent_builder import build_main_agent

        return build_main_agent(self)

    async def _inject_learnings(self) -> None:
        """Inject learning context into the main agent's instructions."""
        if self._learning is None:
            return
        if self._learning.model is None:
            self._learning.model = ModelRegistry(self.settings).get_model()
        if self._learning.db is None:
            self._learning.db = self.db
        try:
            ctx = await self._learning.abuild_context(
                user_id=self.user_id, session_id=self.session_id
            )
            if ctx and self.main_team.instructions:
                # Remove old learning context and add fresh
                self.main_team.instructions = [
                    i
                    for i in self.main_team.instructions
                    if not i.startswith("## What I Know About You")
                    and not i.startswith("## User Profile")
                ]
                self.main_team.instructions.append(ctx)
        except Exception:
            pass

    def _build_agent_catalog(self) -> str:
        """Build a text catalog of specialist agents for the system prompt."""
        lines = []
        for defn in self.pool.list_agents():
            tools_str = ", ".join(defn.tools) if defn.tools else "none"
            lines.append(f"- **{defn.name}**: {defn.description} (tools: {tools_str})")
        return "\n".join(lines)

    def _queue_rewake(self, text: str) -> None:
        """``asyncRewake`` hooks call this from background tasks
        when they finish with exit-2. The text is buffered until
        the next ``handle_message`` turn, where it's drained and
        prepended as a system reminder so the agent sees it on
        the next reasoning step (we can't interrupt an in-flight
        response).
        """
        if not text:
            return
        # asyncio is single-threaded — no lock needed; appends are
        # atomic from concurrent ``asyncio.create_task`` background
        # hooks.
        self._pending_reminders.append(text)

    def _mcp_resolver(self, server: str, tool: str) -> Any | None:
        """Resolver passed to ``HookExecutor`` so ``mcp_tool``-type
        hooks can invoke MCP server tools without the executor
        knowing about the MCP manager directly.

        Resolved at hook-fire time (not at executor construction)
        so this works even though ``__init__`` builds the executor
        BEFORE ``mcp_manager`` is populated — the closure looks up
        the manager dynamically. Reaches into the manager's
        ``_clients`` dict; if MCP gains a public ``call_tool`` API
        later, swap this body to call it.
        """
        mgr = getattr(self, "mcp_manager", None)
        if mgr is None:
            return None
        client = getattr(mgr, "_clients", {}).get(server)
        if client is None:
            return None
        return (getattr(client, "functions", None) or {}).get(tool)

    def _create_tool_event_hook(self) -> ToolEventHook:
        """Create a ToolEventHook for tool event hooks and protected path enforcement."""
        # Build the 6-step permission evaluator from settings. Empty
        # rule arrays + ``default`` mode keep the evaluator a no-op
        # for users who haven't opted in — the existing protected_
        # paths/blocked_commands enforcement still runs alongside.
        # We cache the evaluator on ``self`` so the runtime
        # plan-mode toggle (`/plan`) and the agent's
        # ``exit_plan_mode`` tool can mutate ``evaluator.mode``
        # without rebuilding the hook chain.
        self.permission_evaluator = PermissionEvaluator.from_strings(
            mode=self.settings.permissions.mode,
            deny=self.settings.permissions.deny,
            ask=self.settings.permissions.ask,
            allow=self.settings.permissions.allow,
        )
        return ToolEventHook(
            executor=self.hook_executor,
            session_id=self.session_id,
            protected_paths=self.settings.safety.protected_paths,
            blocked_commands=self.settings.safety.blocked_commands,
            rules_index=self.rules_index,
            project_dir=self.project_dir,
            permission_evaluator=self.permission_evaluator,
        )

    def set_output_style(self, name: str) -> str:
        """See :func:`session.state_ops.set_output_style`."""
        return _state_ops.set_output_style(self, name)

    def set_permission_mode(self, mode: str) -> str:
        """See :func:`session.state_ops.set_permission_mode`."""
        return _state_ops.set_permission_mode(self, mode)

    async def approve_plan(self, run_id: str) -> PlanDecisionResult:
        """See :func:`session.plan_ops.approve_plan`."""
        return await _plan_ops.approve_plan(self, run_id)

    async def dismiss_plan(self, run_id: str) -> PlanDecisionResult:
        """See :func:`session.plan_ops.dismiss_plan`."""
        return await _plan_ops.dismiss_plan(self, run_id)

    async def _record_plan_decision(
        self, run_id: str, decision: str, *, flip_mode: bool
    ) -> PlanDecisionResult:
        """See :func:`session.plan_ops._record_plan_decision`."""
        return await _plan_ops._record_plan_decision(self, run_id, decision, flip_mode=flip_mode)

    def register_broadcast_callback(self, callback) -> None:
        """See :func:`session.broadcast.register_broadcast_callback`."""
        _broadcast_ops.register_broadcast_callback(self, callback)

    async def append_event(
        self,
        event_type: str,
        payload: dict,
        run_id: str = "",
    ) -> None:
        """Record ``(type, payload)`` on this session's event log
        and persist immediately.

        Backing storage for a reload-time replay: the FE can call
        the ``get_session_events`` RPC after ``get_chat_history``
        to reconstruct state the message log doesn't capture
        (finalized visualizer specs, etc.).

        ``seq`` is a per-session monotonic counter — the FE relies
        on it, not timestamps, for replay ordering (wall-clock is
        subject to clock skew and per-event timing collisions).

        Best-effort persistence: DB write failures log and return
        so the in-memory log and any downstream broadcast still
        reach attached clients — only restart-recovery is
        sacrificed. Same policy as ``save_todos`` /
        ``save_plan_decisions``.
        """
        self._event_seq += 1
        # Every event is validated at the construction boundary
        # (Rule 1 — no raw dicts for structured data). Stored
        # in-memory as :class:`SessionEvent`; wire is
        # ``model_dump()`` at the persistence boundary.
        event = SessionEvent.build(
            seq=self._event_seq,
            event_type=event_type,
            payload=payload,
            run_id=run_id,
        )
        self.event_log.append(event)
        persistence = getattr(self, "persistence", None)
        if persistence is not None:
            try:
                await persistence.save_event_log([e.model_dump() for e in self.event_log])
            except Exception as exc:
                logger.debug("event_log persist failed: %s", exc)

    def broadcast(self, channel: str, payload: dict) -> None:
        """See :func:`session.broadcast.broadcast`."""
        _broadcast_ops.broadcast(self, channel, payload)

    def queue_post_run_broadcast(self, channel: str, payload: dict) -> None:
        """See :func:`session.broadcast.queue_post_run_broadcast`."""
        _broadcast_ops.queue_post_run_broadcast(self, channel, payload)

    def drain_post_run_broadcasts(self, run_id: str | None = None) -> None:
        """See :func:`session.broadcast.drain_post_run_broadcasts`."""
        _broadcast_ops.drain_post_run_broadcasts(self, run_id)

    # ── Knowledge warmup ────────────────────────────────────────────

    def start_knowledge_background(self) -> None:
        """See :func:`session.startup_ops.start_knowledge_background`."""
        _startup_ops.start_knowledge_background(self)

    async def _ensure_knowledge(self) -> None:
        """See :func:`session.startup_ops.ensure_knowledge_started`."""
        await _startup_ops.ensure_knowledge_started(self)

    def start_codeindex_background(self) -> None:
        """See :func:`session.startup_ops.start_codeindex_background`."""
        _startup_ops.start_codeindex_background(self)

    def start_marketplace_refresh_background(self) -> None:
        """See :func:`session.startup_ops.start_marketplace_refresh_background`."""
        _startup_ops.start_marketplace_refresh_background(self)

    # ── MCP initialization (async, runs once) ──────────────────────

    async def ensure_mcp(self) -> None:
        """See :func:`session.startup_ops.ensure_mcp`."""
        await _startup_ops.ensure_mcp(self)

    def rebuild_mcp(self) -> None:
        """See :func:`session.startup_ops.rebuild_mcp`."""
        _startup_ops.rebuild_mcp(self)
    def refresh_codeindex_availability(self) -> bool:
        """Re-derive ``_codeindex_available`` from the current chroma
        state and rebuild the agent pool + main team if the flag flipped.

        Without this, the main agent and every specialist keep the
        prompt variant chosen at session ``__init__`` time. After a
        ``/codeindex resync`` (or any sync that transitions HEAD from
        unindexed → indexed) the chroma now has data but the agent's
        system prompt still says *"CodeIndex isn't active"*; the
        agent then refuses to use the ``codeindex_query`` /
        ``codeindex_tree`` tools and tells the user to set things up.
        Called after every successful sync so the prompt always
        matches reality.

        Returns ``True`` if a rebuild happened.
        """
        head = self.code_index_sync.current_sha()
        new_avail = bool(head and self.code_index.has_commit(head))
        if new_avail == self._codeindex_available:
            return False

        self._codeindex_available = new_avail
        # Reload definitions so the pool picks the right
        # ``<name>.codeindex.md`` vs ``<name>.md`` prompt variant per
        # specialist. ``load_definitions`` only upserts when an
        # entry's priority is *strictly greater* than what's already
        # there, so calling it twice with the same sources is a noop.
        # Clear first to force a true reload; ``clear_definitions``
        # preserves ephemerals so user-created agents survive.
        self.pool.clear_definitions(preserve_ephemeral=True)
        self.pool.load_definitions(self.settings, self.project_dir, codeindex_available=new_avail)
        self.plugin_loader.apply_to_agents(self.pool, disabled=self._disabled_plugins)
        # Rebuild Agent objects, preserving current MCP wiring.
        connected = self.mcp_manager.list_connected()
        clients = {name: self.mcp_manager._clients[name] for name in connected}
        self.pool.build_agents(mcp_clients=clients if clients else None)
        # Main team's prompt also flips between ``main_agent.md`` and
        # ``main_agent.codeindex.md`` — rebuild it.
        self.main_team = self._build_main_agent()
        logger.info(
            "codeindex_available → %s; rebuilt agent pool + main team",
            new_avail,
        )
        return True

    # ── MCP status ─────────────────────────────────────────────────

    def get_mcp_status(self) -> list[tuple[str, bool]]:
        """Return list of (server_name, connected) for configured MCP servers."""
        available = set(self.mcp_manager.list_servers())
        connected = set(self.mcp_manager.list_connected())
        return [(name, name in connected) for name in available]

    # ── Dynamic context compaction ─────────────────────────────────

    async def _compact(self) -> str | None:
        """See :func:`session.compact_ops.compact`."""
        return await _compact_ops.compact(self)

    async def _fallback_summarise(self, agno_session):
        """See :func:`session.compact_ops._fallback_summarise`."""
        return await _compact_ops._fallback_summarise(self, agno_session)

    async def compact_if_needed(self, input_tokens: int, context_window: int) -> bool:
        """See :func:`session.compact_ops.compact_if_needed`."""
        return await _compact_ops.compact_if_needed(self, input_tokens, context_window)

    async def force_compact(self) -> tuple[str, str]:
        """See :func:`session.compact_ops.force_compact`."""
        return await _compact_ops.force_compact(self)

    # ── Context breakdown ─────────────────────────────────────────────

    async def context_breakdown(self) -> ContextBreakdown:
        """See :func:`session.compact_ops.context_breakdown`."""
        return await _compact_ops.context_breakdown(self)

    # ── Debug logging ─────────────────────────────────────────────────

    def _log_run_messages(self) -> None:
        """See :func:`_log_run_messages_debug`."""
        _log_run_messages_debug(self.main_team)

    # ── Message handling (headless path) ──────────────────────────────

    async def _handle_run_failure(self, exc: Exception) -> str:
        """Common failure path for `handle_message`: audit log,
        StopFailure hook fire (observation-only — plugins like
        crash reporters / alerting react here without having to
        scrape audit logs), formatted error string return.

        The StopFailure hook mirrors the Stop hook on the happy
        path — same payload shape, same non-blocking semantics —
        so plugins can observe both success and failure with a
        single subscription pair.
        """
        error_msg = f"Error handling message: {exc}"
        print_error(error_msg)

        self.audit.log(
            session_id=self.session_id,
            agent_name="session",
            tool_name="main_team",
            status="error",
            details={"error": str(exc)},
        )

        with contextlib.suppress(Exception):
            await self.hook_executor.execute(
                event=HookEvent.STOP_FAILURE.value,
                payload={
                    "session_id": self.session_id,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
        return error_msg

    async def _retry_on_stop_hook_block(self, response_text: str) -> str:
        """Fire the ``Stop`` hook up to 3 times; feed rejection
        messages back to the agent to re-generate the response.

        A Stop hook that returns ``should_continue=False`` treats
        the agent's response as unacceptable — its ``message``
        becomes a critique the agent should address. We re-run
        the agent with that critique as a system message, then
        fire the hook again on the new response. Bounded at 3
        attempts so a persistently-rejecting hook doesn't loop
        forever. On the third failure we accept the response
        (the hook can still deny at a later stage if it's a
        hard-block invariant).
        """
        for _stop_attempt in range(3):
            stop_result = await self.hook_executor.execute(
                event=HookEvent.STOP.value,
                payload={
                    "session_id": self.session_id,
                    "response": response_text[:500],
                },
            )
            if stop_result.should_continue:
                break
            feedback = stop_result.message or "Response blocked by Stop hook."
            system_msg = (
                f"[SYSTEM] Your previous response was rejected by a Stop hook: "
                f"{feedback}\nPlease revise your response to address this issue."
            )
            response = await self.main_team.arun(system_msg, stream=False)
            response_text = extract_response_text(response)
        return response_text

    async def _check_user_prompt_hook(self, message: str) -> str | None:
        """Fire the ``UserPromptSubmit`` hook. Returns the blocked
        message when the hook denies, ``None`` when the turn should
        proceed. Blocked turns emit an audit entry so a policy denial
        is traceable — otherwise the user sees "blocked" with no
        record of why."""
        hook_result = await self.hook_executor.execute(
            event=HookEvent.USER_PROMPT_SUBMIT.value,
            payload={"message": message, "session_id": self.session_id},
        )
        if hook_result.should_continue:
            return None
        blocked_msg = hook_result.message or "Blocked by UserPromptSubmit hook."
        self.audit.log(
            session_id=self.session_id,
            agent_name="session",
            tool_name="user_prompt",
            status="BLOCKED",
            details={"reason": blocked_msg},
        )
        return blocked_msg

    async def _guardrail_prefix(self, message: str) -> str:
        """Run guardrail checks and produce a warning prefix (empty
        when disabled or all clean). Guardrails inform, don't block —
        the prefix is prepended to the effective message so the model
        sees the caveat before the user text.
        """
        if not self.guardrail_runner.enabled:
            return ""
        gr_results = await self.guardrail_runner.check(message)
        if not gr_results:
            return ""
        warnings = "; ".join(r.message for r in gr_results)
        logger.info("Guardrails triggered: %s", warnings)
        return (
            f"[GUARDRAIL WARNING] The following issues were detected in "
            f"the user message: {warnings}\n"
            f"Please be cautious and do not repeat or use any flagged content.\n\n"
        )

    def _build_effective_message(self, message: str, guardrail_prefix: str) -> str:
        """Assemble the message the model actually sees: any queued
        ``asyncRewake`` reminders (drained one-shot), a
        ``<system-context>`` datetime hint, and the guardrail prefix
        if any."""
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
        reminders_block = ""
        if self._pending_reminders:
            joined = "\n".join(self._pending_reminders)
            self._pending_reminders.clear()
            reminders_block = f"<system-reminder>{joined}</system-reminder>\n"
        effective = (
            f"{reminders_block}"
            f"<system-context>Current datetime: {timestamp}</system-context>\n{message}"
        )
        if guardrail_prefix:
            effective = guardrail_prefix + effective
        return effective

    async def handle_message(self, message: str, **media_kwargs) -> str:
        """Handle a single user message and return the response.

        Accepts optional media keyword arguments (images, audio, videos, files)
        which are forwarded directly to team.arun().
        """
        await self.ensure_mcp()

        blocked = await self._check_user_prompt_hook(message)
        if blocked is not None:
            return blocked

        guardrail_prefix = await self._guardrail_prefix(message)
        effective_message = self._build_effective_message(message, guardrail_prefix)

        try:
            response = await self.main_team.arun(effective_message, stream=False, **media_kwargs)
            self._log_run_messages()
            response_text = extract_response_text(response)

            self.audit.log(
                session_id=self.session_id,
                agent_name="ember",
                tool_name="main_team",
                status="success",
            )

            response_text = await self._retry_on_stop_hook_block(response_text)

            # Compact history if approaching context limit.
            metrics = getattr(getattr(self.main_team, "run_response", None), "metrics", None)
            if metrics:
                input_tokens = getattr(metrics, "input_tokens", 0) or 0
                await self.compact_if_needed(input_tokens, self._context_window)

            return response_text

        except Exception as e:
            return await self._handle_run_failure(e)
