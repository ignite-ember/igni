"""Session core — wires up subsystems and delegates to coordinators.

:class:`Session` is a slim orchestrator: it constructs the
subsystem instances, composes the coordinator classes that own
their concern's state + behaviour, and exposes a thin public
surface that forwards to those coordinators.

Owned concerns (each has a class in this package):

* :class:`~.loop_ops.LoopController` — ``/loop`` state.
* :class:`~.plan_ops.PlanCoordinator` — approve / dismiss plans.
* :class:`~.state_ops.RuntimeModeCoordinator` — output-style +
  permission-mode flips.
* :class:`~.compaction.CompactionCoordinator` — auto + manual
  context compaction.
* :class:`~.startup.SessionStartupCoordinator` — background
  warmups + MCP first-connect.
* :class:`~.mcp_ops.McpLifecycleCoordinator` — plugin-driven MCP
  auto-connect / disconnect.
* :class:`~.message_handler.SessionMessageHandler` — the six-
  step headless message pipeline.
* :class:`~.cloud_catalog.CloudModelCatalog` — one-shot cloud
  model refresh.
* :class:`~.cloud_auth.SessionCloudAuth` — cloud credential
  swap + rebuild-team invariant.
* :class:`~.event_log.SessionEventLog` — append-only event log
  + monotonic seq counter.
* :class:`~.reminders.PendingReminderQueue` — asyncRewake hook
  buffer.
* :class:`~.mcp_resolver.MCPToolResolver` — mcp_tool hook
  target lookup.
* :class:`~.tool_hook_factory.ToolEventHookFactory` — ToolEventHook
  + PermissionEvaluator composition.
* :class:`~.learning_ops.SessionLearningManager` — learning
  context inject / extract.
* :class:`~.codeindex_availability.CodeIndexAvailabilityRefresher`
  — flip-detect + rebuild.
* :class:`~.identity.SessionIdentity` — session_id rotate /
  rebind invariant.
* :class:`~.run_debug.RunMessagesDebugDumper` — diagnostic
  dumps.
* :class:`~.plugin_reload.PluginReloadOrchestrator` — hot
  plugin/skill/agent/hook/MCP reload.

Session persistence and chat history are delegated entirely to
Agno's native ``db`` / ``session_id`` mechanism. The main team
and all its members receive the same ``db`` and ``session_id``,
so all turns are automatically persisted and restored.
"""

import contextlib
import getpass
import logging
import threading
import uuid
from pathlib import Path
from typing import Any

from agno.agent import Agent
from agno.compression.manager import CompressionManager

from ember_code.backend.schemas_codeindex_rpc import RefreshAvailabilityResult
from ember_code.backend.schemas_model import ModelSwitchResult
from ember_code.core.agents import AgentPool
from ember_code.core.auth.credentials import CloudCredentials
from ember_code.core.code_index import CodeIndex, CodeIndexSyncManager
from ember_code.core.config.models import ModelRegistry
from ember_code.core.config.permissions import PermissionGuard
from ember_code.core.config.settings import Settings
from ember_code.core.config.tool_permissions import ToolPermissions
from ember_code.core.guardrails.runner import GuardrailRunner
from ember_code.core.hooks.events import HookEvent
from ember_code.core.hooks.executor import HookExecutor
from ember_code.core.hooks.loader import HookLoader
from ember_code.core.hooks.tool_hook import ToolEventHook
from ember_code.core.init import ProjectInitializer
from ember_code.core.knowledge.manager import KnowledgeManager
from ember_code.core.learn import create_learning_machine  # noqa: F401 — test-patch target
from ember_code.core.loop import LoopProgressStore, LoopStore, LoopToolResult
from ember_code.core.lsp import LspServerManager, load_lsp_config
from ember_code.core.mcp.client import MCPClientManager
from ember_code.core.mcp.config import MCPConfigLoader
from ember_code.core.memory.manager import StorageManager
from ember_code.core.monitors import MonitorManager, load_monitor_config
from ember_code.core.output_styles import OutputStyle, discover_output_styles
from ember_code.core.plugins import PluginLoader, load_state
from ember_code.core.prompts import load_prompt
from ember_code.core.session.agent_builder import MainAgentBuilder
from ember_code.core.session.agent_factory import (
    create_guardrails,
    create_reasoning_tools,
)
from ember_code.core.session.broadcast import BroadcastBus
from ember_code.core.session.broadcast_schema import BroadcastEvent
from ember_code.core.session.cloud_auth import SessionCloudAuth
from ember_code.core.session.cloud_catalog import CloudModelCatalog
from ember_code.core.session.codeindex_availability import (
    CodeIndexAvailabilityRefresher,
)
from ember_code.core.session.compaction import CompactionCoordinator
from ember_code.core.session.event_log import SessionEventLog
from ember_code.core.session.event_log_schema import SessionEvent
from ember_code.core.session.identity import SessionIdentity
from ember_code.core.session.knowledge_ops import SessionKnowledgeManager
from ember_code.core.session.learning_ops import SessionLearningManager
from ember_code.core.session.loop_ops import LoopController
from ember_code.core.session.mcp_ops import McpLifecycleCoordinator, McpLifecycleDeps
from ember_code.core.session.mcp_resolver import MCPToolResolver
from ember_code.core.session.memory_ops import SessionMemoryManager
from ember_code.core.session.message_handler import SessionMessageHandler
from ember_code.core.session.persistence import SessionPersistence
from ember_code.core.session.plan_ops import PlanCoordinator
from ember_code.core.session.plugin_reload import PluginReloadOrchestrator
from ember_code.core.session.reminders import PendingReminderQueue
from ember_code.core.session.run_debug import RunMessagesDebugDumper
from ember_code.core.session.schemas import (
    CompactResult,
    ContextBreakdown,
    LoopAdvance,
    McpInitResult,
    McpServerStatus,
    MessageMedia,
    PlanDecisionResult,
    PluginReloadCounts,
)
from ember_code.core.session.startup import SessionStartupCoordinator
from ember_code.core.session.state_ops import RuntimeModeCoordinator
from ember_code.core.session.tool_hook_factory import ToolEventHookFactory
from ember_code.core.skills.loader import SkillPool
from ember_code.core.sub_agent_hitl import SubAgentHITLCoordinator
from ember_code.core.tools.plan import PlanDecision, PlanStore
from ember_code.core.tools.registry import ToolRegistry
from ember_code.core.tools.todo import TodoStore
from ember_code.core.utils.audit import AuditLogger
from ember_code.core.utils.context import ProjectMemoryBank, load_project_context
from ember_code.core.utils.display import DisplayManager
from ember_code.core.utils.response import extract_response_text  # noqa: F401 — test-patch target
from ember_code.core.utils.rules_index import RulesIndex
from ember_code.core.workspace import WorkspaceManager

logger = logging.getLogger(__name__)


def _log_run_messages_debug(team: Any) -> None:
    """Back-compat shim around :meth:`RunMessagesDebugDumper.dump_team`.

    Kept as a module-level function so
    ``patch("ember_code.core.session.core._log_run_messages_debug")``
    (used by older diagnostic tests) still intercepts. New code
    should call :meth:`RunMessagesDebugDumper.dump_team` directly.
    """
    RunMessagesDebugDumper.dump_team(team)


class Session:
    """Manages a single igni session with all subsystem integrations.

    Slim orchestrator: composes the coordinator classes and forwards
    public methods to them. Session persistence and chat history are
    delegated entirely to Agno's native ``db`` / ``session_id``
    mechanism — the main team and all its members receive the same
    ``db`` and ``session_id``, so all turns are automatically
    persisted and restored.
    """

    # Tools the main team ALWAYS gets — the shell-first core. Bash
    # handles search/find/list/read directly (``rg``, ``find``,
    # ``cat``, etc.); Edit/Write stay for surgical changes and new
    # files because shell-based alternatives (``sed -i``, here-doc
    # rewrites) are fragile. Grep/Glob/Read/LS toolkits intentionally
    # omitted — they overlapped with shell and confused the model
    # (v0.4.0 / commit 7e50705). See CLAUDE_CODE_PARITY.md row 22.
    _MAIN_CORE_TOOLS: tuple[str, ...] = (
        "Write",
        "Edit",
        "Bash",
        "Schedule",
        "NotebookEdit",
    )

    def __init__(
        self,
        settings: Settings,
        project_dir: Path | None = None,
        resume_session_id: str | None = None,
        additional_dirs: list[Path] | None = None,
        pre_knowledge: Any | None = None,
    ):
        self.settings = settings

        # Cloud model catalog first — every code path below may read
        # ``settings.models.default``, so refreshing it up front lets
        # a brand-new install reach a usable state right after login
        # without a hardcoded fallback name.
        self.cloud_catalog = CloudModelCatalog(settings)
        self.cloud_catalog.refresh()

        self.project_dir = project_dir or Path.cwd()
        self.workspace = WorkspaceManager(self.project_dir, additional_dirs)

        # Latched input-token count from the most recent completed
        # run. Read by ``get_status`` for the FE's ctx footer.
        self._last_input_tokens: int = 0

        self._init_loop_state()
        self._init_per_session_scratch()

        # ── First-run initialization (agents, skills, hooks, ember.md) ─
        ProjectInitializer.initialize(self.project_dir)

        # ── Storage (Agno AsyncBaseDb) ────────────────────────────────
        self.db = StorageManager.build_db(settings, project_dir=self.project_dir)

        self._init_knowledge(settings, pre_knowledge)

        # ── Permission Guard ─────────────────────────────────────────
        self.permission_guard = PermissionGuard(settings)

        # ── Audit Logger ─────────────────────────────────────────────
        self.audit = AuditLogger(settings)

        # ── Terminal display sink ────────────────────────────────────
        # Constructed early — before MCP init and all downstream
        # subsystems — so any startup path that surfaces status has a
        # live sink. Every caller reaches display through
        # ``session.display``; there is no module-level singleton.
        self.display = DisplayManager()

        self._init_plugins_output_styles_hooks(settings)

        self._init_project_context(settings)

        self._init_codeindex(settings)
        self._init_agent_and_skill_pools(settings)

        # ── Context window (for compaction threshold, capped by setting) ──
        self._context_window = min(
            ModelRegistry(settings).get_context_window(),
            settings.models.max_context_window,
        )

        # ── Cloud auth coordinator (owns creds + rebuild-team invariant) ─
        # Composed BEFORE ``main_team`` so the first agent build can
        # read ``self.cloud_auth.access_token``. ``rebuild_team`` is
        # a closure that reads ``self._rebuild_main_team`` at call
        # time — every credential change goes through the coordinator
        # so the "assign creds → rebuild team" order stays a single
        # invariant.
        self.cloud_auth = SessionCloudAuth(
            creds=CloudCredentials(settings.auth.credentials_file),
            server_url=settings.api_url,
            rebuild_team=lambda: self._rebuild_main_team(),
            catalog=self.cloud_catalog,
        )

        # ── Identity coordinator (owns session_id / user_id / named) ──
        # Constructed with the main-team + persistence refs so
        # id-rotation propagates via a single method call. Populated
        # here BEFORE persistence + main_team so the closures resolve
        # to the correct attributes after the block below fires.
        self.identity = SessionIdentity(
            session_id=resume_session_id or str(uuid.uuid4())[:8],
            session_named=bool(resume_session_id),
            user_id=getpass.getuser(),
            main_team_ref=lambda: getattr(self, "main_team", None),
            persistence_ref=lambda: getattr(self, "persistence", None),
        )

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
        self.pool.attach_knowledge_manager(self.knowledge_mgr if self.knowledge else None)

        # ── Learning coordinator (owns _learning + inject/extract) ──
        # Composed after ``persistence`` / ``memory_mgr`` so the
        # narrow-dep constructor has everything it needs. The three
        # closures let the manager tolerate ``main_team`` being
        # rebuilt (plugin-reload, compact, MCP-refresh) without
        # going stale. ``create_learning_machine`` is called from
        # THIS module's namespace so the long-standing test-patch
        # target ``ember_code.core.session.core.create_learning_machine``
        # continues to intercept. The typed :class:`LearnBootResult`
        # envelope is unwrapped here — the ``reason`` string flows
        # through to the manager so operators see WHY a machine was
        # not built.
        boot = create_learning_machine(settings, self.db)
        self.learning_mgr = SessionLearningManager(
            settings=settings,
            db=self.db,
            user_id_ref=lambda: self.user_id,
            session_id_ref=lambda: self.session_id,
            main_team_ref=lambda: self.main_team,
            learning=boot.machine,
            boot_reason=boot.reason,
        )

        # ── Coordinators ────────────────────────────────────────────
        # Every state-holding concern gets its own class. See the
        # module docstring for the map.
        self.plan = PlanCoordinator(self)
        self.mode = RuntimeModeCoordinator(self)
        self.compaction = CompactionCoordinator(self)
        self.startup = SessionStartupCoordinator(self)
        # Coordinator gets a narrow deps port, not the whole
        # Session. ``rebuild`` is a lambda (not a bound method) so
        # the coordinator re-reads ``self.rebuild_mcp`` at call
        # time — preserving the pre-refactor "late binding"
        # semantics in case the method is monkey-patched by tests.
        self.mcp_lifecycle = McpLifecycleCoordinator(
            McpLifecycleDeps(
                mcp_manager=self.mcp_manager,
                rebuild=lambda: self.rebuild_mcp(),
            )
        )
        self.plugin_reload_orchestrator = PluginReloadOrchestrator(
            project_dir=self.project_dir,
            mcp_manager=self.mcp_manager,
            plugin_loader_ref=lambda: self.plugin_loader,
            disabled_plugins_ref=lambda: self._disabled_plugins,
            rebuild_plugins_and_hooks=lambda: self._init_plugins_output_styles_hooks(self.settings),
            rebuild_agent_and_skill_pools=lambda: self._init_agent_and_skill_pools(self.settings),
            rebuild_main_team=self._rebuild_main_team,
            skill_pool_ref=lambda: self.skill_pool,
            agent_pool_ref=lambda: self.pool,
            hooks_map_ref=lambda: self.hooks_map,
            disconnect_removed_mcps=self._disconnect_removed_mcps,
            auto_connect_mcps=self._auto_connect_mcps,
        )

        # ── Availability refresher (codeindex flip → rebuild) ───────
        # ``build_main_agent`` is a lambda (not a bound method) so
        # the refresher re-reads ``self._build_main_agent`` at call
        # time — tests that monkey-patch the private name intercept
        # cleanly this way.
        self._codeindex_refresher = CodeIndexAvailabilityRefresher(
            settings=settings,
            project_dir=self.project_dir,
            code_index=self.code_index,
            code_index_sync=self.code_index_sync,
            pool_ref=lambda: self.pool,
            plugin_loader_ref=lambda: self.plugin_loader,
            disabled_plugins_ref=lambda: self._disabled_plugins,
            mcp_manager_ref=lambda: self.mcp_manager,
            build_main_agent=lambda: self._build_main_agent(),
            assign_main_team=self._assign_main_team,
            get_availability=lambda: self._codeindex_available,
            set_availability=self._set_codeindex_available,
        )

        # ── Main Agent (single agent with all tools + orchestration) ──
        self.main_team = self._build_main_agent()

        # Message-handler wired last so it can capture the live
        # ``handle_message`` context. ``team_ref`` is a closure over
        # ``self`` so it always reads the current ``main_team`` even
        # after compact/reload rebuilds swap it out.
        # ``extract_response_text`` is read from THIS module's
        # namespace so tests that patch
        # ``ember_code.core.session.core.extract_response_text``
        # still intercept — the handler calls back through the
        # injected reference on every turn.
        self._message_handler = SessionMessageHandler(
            hook_executor=self.hook_executor,
            audit=self.audit,
            display=self.display,
            guardrail_runner=self.guardrail_runner,
            team_ref=lambda: self.main_team,
            pending_reminders_drain=self._reminder_queue.drain,
            compact_hook=self.compact_if_needed,
            ensure_mcp=self.ensure_mcp,
            session_id=self.session_id,
            context_window=self._context_window,
            latch_input_tokens=self.latch_input_tokens,
            extract_response_text=lambda resp: extract_response_text(resp),
        )

    # ── __init__ helpers ────────────────────────────────────────

    def _init_per_session_scratch(self) -> None:
        """Set up the per-session scratch state populated by tools /
        the run loop.

        Composes :class:`PendingReminderQueue` +
        :class:`SessionEventLog` (the log's persister callback
        looks up ``self.persistence`` lazily via ``getattr`` so
        the coordinator survives the constructor's storage-then-
        managers ordering).
        """
        self.todo_store = TodoStore()
        self.plan_store = PlanStore()
        self._plan_mode_attempt: int = 0
        ProjectMemoryBank(self.project_dir).ensure()
        self.output_styles: dict[str, OutputStyle] = {}
        self._active_output_style: str = ""
        # Owns the FE push-channel fan-out (callback list +
        # post-run deferral queue). Composed at construction so
        # every downstream method — including callers that build
        # Session via ``__new__`` — can rely on the attribute
        # existing.
        self.broadcast_bus = BroadcastBus()
        # asyncRewake hook buffer (one class instead of three
        # scattered fields).
        self._reminder_queue = PendingReminderQueue()
        # Event log coordinator — persister ref is a closure so
        # ``self.persistence`` can be composed later in
        # ``__init__`` without needing to re-wire the log.
        self.event_log_store = SessionEventLog(
            persist_ref=lambda: getattr(self, "persistence", None)
        )

    def _init_project_context(self, settings: Settings) -> None:
        """Load top-level project instructions + construct
        :class:`RulesIndex`. Subdirectory rules are lazily
        discovered by :class:`ToolEventHook` when the agent
        touches a file in those areas.
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
        """Construct the :class:`LoopController` and its Sqlite-backed
        stores.
        """
        self.loop_store = LoopStore(project_dir=self.project_dir)
        self.loop_progress_store = LoopProgressStore(project_dir=self.project_dir)
        self.loop = LoopController(self.loop_store)

    def _init_codeindex(self, settings: Settings) -> None:
        """Construct :class:`CodeIndex` + :class:`CodeIndexSyncManager`
        eagerly and compute the ``_codeindex_available`` flag.
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
        """
        self.mcp_manager = MCPClientManager(self.project_dir)
        # Session-scoped ``{server: reason}`` cache. Written by
        # :meth:`record_mcp_result` (called from
        # :class:`~ember_code.core.session.startup.mcp.McpInitPhase`
        # + :class:`~ember_code.backend.server_mcp.McpController`)
        # and read by :meth:`get_mcp_status` /
        # :class:`~ember_code.backend.schemas_mcp.MCPServerSnapshot`.
        # Replaces the pre-refactor ``mcp_manager.get_error`` side
        # channel with a session-owned dict that connect calls
        # populate at Result time.
        self.mcp_failures: dict[str, str] = {}
        self.plugin_loader.apply_to_mcp(
            MCPConfigLoader(self.project_dir),
            self.mcp_manager.configs,
            disabled=self._disabled_plugins,
        )

    def record_mcp_result(self, name: str, result: object | None) -> None:
        """Cache a connect Result's failure reason (or clear it).

        Called from every site that invokes ``mcp_manager.connect``
        so :class:`~ember_code.backend.schemas_mcp.MCPServerSnapshot`
        and the ``/mcp`` status command can render the failure
        without asking the manager for post-hoc error state.

        * ``result`` is an
          :class:`~ember_code.core.mcp.MCPConnectResult` — success
          clears the cached failure, failure records ``reason``.
        * ``result is None`` — the caller disconnected the server;
          clears any stale failure so the panel shows "disconnected"
          rather than the last connect error.
        """
        if result is None:
            self.mcp_failures.pop(name, None)
            return
        if getattr(result, "ok", False):
            self.mcp_failures.pop(name, None)
        else:
            self.mcp_failures[name] = getattr(result, "reason", "") or ""

    def _init_knowledge(self, settings: Settings, pre_knowledge: Any | None) -> None:
        """Wire up the Chroma-backed knowledge index (if enabled)."""
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
        """Construct :class:`LspServerManager` (lazy) +
        :class:`MonitorManager` (eager) from the current plugin set.
        """
        lsp_plugin_roots = self.plugin_loader.collect_lsp_roots(
            disabled=self._disabled_plugins,
        )
        lsp_configs = load_lsp_config(
            self.project_dir,
            plugin_roots=lsp_plugin_roots,
        )
        self.lsp_manager = LspServerManager(lsp_configs, self.project_dir)

        monitor_plugin_roots = self.plugin_loader.collect_monitor_roots(
            disabled=self._disabled_plugins,
        )
        monitor_configs = load_monitor_config(
            self.project_dir,
            plugin_roots=monitor_plugin_roots,
        )
        self.monitor_manager = MonitorManager(monitor_configs, self.project_dir)

    def _init_plugins_output_styles_hooks(self, settings: Settings) -> None:
        """Set up plugins → output-styles → hooks in fixed order.

        Composes :class:`MCPToolResolver` +
        :class:`ToolEventHookFactory` so the tool-event-hook
        assembly and the ``mcp_tool`` hook resolver live on
        dedicated classes rather than as free methods on Session.
        """
        # ── Plugin discovery ────────────────────────────────────────
        self.plugin_state = load_state(settings.storage.data_dir)
        self.plugin_loader = PluginLoader()
        self.plugin_loader.load_all(self.project_dir)
        managed_plugins = {p.name for p in self.plugin_loader.list_plugins() if p.is_managed}
        self._disabled_plugins = set(self.plugin_state.disabled) - managed_plugins

        # ── Output-style discovery ──────────────────────────────────
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
        load_result = self._hook_loader.load()
        self._hook_registry = load_result.registry
        # ``hooks_map`` remains the raw dict for backward compat with
        # ``executor.hooks``, backend/panels/hooks_panel.py,
        # backend/schemas_hooks.py, and interactive_loop.py — all of
        # which iterate it as a dict. Identity-preserving: mutating
        # via ``hooks_map`` reflects in the registry, and vice-versa.
        self.hooks_map = self._hook_registry.raw
        # Plugins prepend per event so project hooks still run last.
        plugin_result = self.plugin_loader.apply_to_hooks(
            self._hook_loader,
            self._hook_registry,
            disabled=self._disabled_plugins,
        )
        for warning in [*load_result.warnings, *plugin_result.warnings]:
            logger.warning(
                "hook load warning [%s] from %s: %s",
                warning.kind,
                warning.source,
                warning.detail,
            )
        # Reminder queue is composed in ``_init_per_session_scratch``
        # so it survives a hook-executor rebuild (``reload_hooks``);
        # a stray reminder from the previous incarnation is dropped
        # by the reset below.
        if hasattr(self, "_reminder_queue"):
            self._reminder_queue.replace([])
        else:
            self._reminder_queue = PendingReminderQueue()
        # MCP-tool resolver — used by ``mcp_tool``-type hooks.
        self._mcp_resolver_obj = MCPToolResolver(
            mcp_manager_ref=lambda: getattr(self, "mcp_manager", None)
        )
        self.hook_executor = HookExecutor(
            self.hooks_map,
            mcp_resolver=self._mcp_resolver_obj.resolve,
            rewake_callback=self._reminder_queue.queue,
        )
        # Rebuild the tool-hook factory so it points at the new
        # executor. The factory caches the ``PermissionEvaluator``
        # so a mode flip performed via :class:`RuntimeModeCoordinator`
        # survives a ``reload_hooks``.
        self._tool_hook_factory = ToolEventHookFactory(
            settings=settings,
            rules_index=getattr(self, "rules_index", None)
            or RulesIndex(self.project_dir, read_claude_md=settings.rules.cross_tool_support),
            project_dir=self.project_dir,
            hook_executor_ref=lambda: self.hook_executor,
            session_id_ref=lambda: self.session_id,
        )

    # ── Cloud auth accessors (forward to SessionCloudAuth) ─────

    @property
    def _cloud(self) -> CloudCredentials:
        """Legacy accessor — reads through
        :attr:`SessionCloudAuth.credentials`. Kept as an attribute
        rather than a property in the coordinator so tests that
        seed ``session._cloud`` directly keep working.
        """
        return self.cloud_auth.credentials

    @_cloud.setter
    def _cloud(self, value: CloudCredentials) -> None:
        """Compat setter — reroutes writes through
        :meth:`SessionCloudAuth.replace` so the "assign creds →
        rebuild team" invariant runs on legacy code paths too."""
        # Direct field write on the coordinator (no team rebuild)
        # — preserves the pre-refactor behaviour where callers
        # who set ``session._cloud`` manually did NOT trigger a
        # rebuild.
        self.cloud_auth._creds = value

    @property
    def _cloud_server_url(self) -> str:
        return self.cloud_auth.server_url

    @property
    def cloud_connected(self) -> bool:
        """Whether the session is authenticated with Ember Cloud."""
        return self.cloud_auth.connected

    @property
    def cloud_org_id(self) -> str | None:
        """The organization ID from the Ember Cloud JWT."""
        return self.cloud_auth.org_id

    @property
    def cloud_org_name(self) -> str | None:
        """The organization display name from the Ember Cloud JWT."""
        return self.cloud_auth.org_name

    def replace_cloud_credentials(self, creds: CloudCredentials) -> None:
        """Delegate to :meth:`SessionCloudAuth.replace`."""
        self.cloud_auth.replace(creds)

    def clear_cloud_credentials(self) -> None:
        """Delegate to :meth:`SessionCloudAuth.clear`."""
        self.cloud_auth.clear()

    def refresh_cloud_models(self) -> int:
        """Delegate to :meth:`SessionCloudAuth.refresh_models`."""
        return self.cloud_auth.refresh_models()

    # ── Plugin hot-reload ──────────────────────────────────────

    def _rebuild_main_team(self) -> None:
        """Assign a freshly-built main team to ``self.main_team``."""
        self.main_team = self._build_main_agent()

    def rebuild_main_team(self) -> None:
        """Public wrapper over :meth:`_rebuild_main_team`."""
        self._rebuild_main_team()

    def _assign_main_team(self, team: Any) -> None:
        """Assignment sink used by
        :class:`CodeIndexAvailabilityRefresher` so the refresher
        can install a freshly-built team without reaching into
        ``session.main_team`` by name."""
        self.main_team = team

    def _set_codeindex_available(self, value: bool) -> None:
        """Setter used by :class:`CodeIndexAvailabilityRefresher`
        so the refresher writes through a named method rather than
        via bare-attribute assignment on the session."""
        self._codeindex_available = value

    def set_default_model(self, model_name: str) -> ModelSwitchResult:
        """Validate ``model_name`` against the registry, swap the
        default, and rebuild the main team. Returns a Pattern-3
        :class:`ModelSwitchResult` envelope so callers stop try/
        except-ing on unknown models.
        """
        registry = self.settings.models.registry
        if model_name not in registry:
            return ModelSwitchResult(
                ok=False,
                model_name=model_name,
                available=sorted(registry.keys()),
            )
        self.settings.models.default = model_name
        self._rebuild_main_team()
        return ModelSwitchResult(ok=True, model_name=model_name)

    def set_plan_research_armed(self, armed: bool) -> None:
        """Arm / disarm the plan-mode researcher nudge for the next
        turn.
        """
        self._plan_research_armed = armed

    def consume_plan_research_flag(self) -> bool:
        """Get-and-reset the ``/plan``-armed flag.

        Moves the flag reset off of ``BackendServer`` where it was
        reaching through to a private Session attribute.

        ``is True`` because mocked sessions in tests use
        ``MagicMock`` which auto-spawns missing attrs as MagicMock
        instances — those evaluate truthy and would wrap every
        test message.
        """
        armed = getattr(self, "_plan_research_armed", False) is True
        if armed:
            self._plan_research_armed = False
        return armed

    def start_all_background_services(self) -> None:
        """Kick off knowledge + codeindex background services.

        Idempotent-safe — each subsystem's ``start_*_background``
        entry is guarded against a double-start.
        """
        self.start_knowledge_background()
        self.start_codeindex_background()

    def start_boot_background_services(self) -> None:
        """Superset of :meth:`start_all_background_services` used by
        the boot runtime only — also refreshes plugin marketplace
        catalogs.
        """
        self.start_all_background_services()
        self.start_marketplace_refresh_background()

    async def fire_session_start_hook(self) -> None:
        """Fire the ``SessionStart`` hook.

        Kept on ``Session`` (rather than server.py) because the
        payload is Session state (``session_id`` +
        ``hook_executor``) — server.py used to import ``HookEvent``
        inline solely so this method could live there.

        Best-effort: a hook loader error mustn't gate startup. The
        try/except mirrors the pre-refactor
        ``contextlib.suppress(Exception)`` on the server side.
        """
        try:
            await self.hook_executor.execute(
                event=HookEvent.SESSION_START.value,
                payload={"session_id": self.session_id},
            )
        except Exception as exc:
            logger.debug("session_start hook fire raised: %s", exc)

    def reload_hooks(self) -> int:
        """Reload hooks from settings files. Returns the number of
        hooks loaded.
        """
        load_result = self._hook_loader.load()
        self._hook_registry = load_result.registry
        self.hooks_map = self._hook_registry.raw
        for warning in load_result.warnings:
            logger.warning(
                "hook reload warning [%s] from %s: %s",
                warning.kind,
                warning.source,
                warning.detail,
            )
        # Reset the reminder queue (dropping any stale entries from
        # the pre-reload incarnation) and rebuild the executor.
        self._reminder_queue.replace([])
        self.hook_executor = HookExecutor(
            self.hooks_map,
            mcp_resolver=self._mcp_resolver_obj.resolve,
            rewake_callback=self._reminder_queue.queue,
        )
        # Recreate tool event hook on the team
        tool_event_hook = self._create_tool_event_hook()
        if self.main_team:
            existing = self.main_team.tool_hooks or []
            self.main_team.tool_hooks = [h for h in existing if not isinstance(h, ToolEventHook)]
            self.main_team.tool_hooks.append(tool_event_hook)
        return self._hook_registry.total_hooks

    def reload_plugins(self) -> PluginReloadCounts:
        """Hot-reload plugin contents from disk — no session restart."""
        return self.plugin_reload_orchestrator.reload()

    async def _disconnect_removed_mcps(self, names: set[str]) -> None:
        """Thin async wrapper — delegates to
        :meth:`McpLifecycleCoordinator.disconnect`."""
        await self.mcp_lifecycle.disconnect(names)

    async def _auto_connect_mcps(self, names: set[str]) -> None:
        """Thin async wrapper — delegates to
        :meth:`McpLifecycleCoordinator.connect`."""
        await self.mcp_lifecycle.connect(names)

    # ── Main Agent setup ────────────────────────────────────────

    def _resolve_main_tool_names(self, registry: "ToolRegistry") -> list[str]:
        """Compose the main team's toolkit, honouring per-session
        flags (web permissions, CodeIndex availability).
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
        if self._codeindex_available:
            tool_names.append("CodeIndex")
        return tool_names

    def _build_main_agent(self) -> Agent:
        """Construct the main :class:`Agent` via
        :class:`MainAgentBuilder`.
        """
        return MainAgentBuilder(
            self,
            agent_cls=Agent,
            registry_cls=ToolRegistry,
            permissions_cls=ToolPermissions,
            compression_cls=CompressionManager,
            model_registry_cls=ModelRegistry,
            reasoning_factory=create_reasoning_tools,
            guardrails_factory=create_guardrails,
            prompt_loader=load_prompt,
        ).build()

    # ── Public accessors consumed by the agent-builder sub-package ──

    @property
    def cloud_access_token(self) -> str | None:
        """The Ember Cloud access token (``None`` when logged out)."""
        return self.cloud_auth.access_token

    @property
    def cloud_server_url(self) -> str:
        """The Ember Cloud API root URL used by cloud-routed tools."""
        return self.cloud_auth.server_url

    @property
    def codeindex_available(self) -> bool:
        """Whether a populated CodeIndex exists for the current HEAD."""
        return self._codeindex_available

    @property
    def active_output_style(self) -> str:
        """Name of the currently-active output style (or empty string)."""
        return self._active_output_style

    def set_active_output_style(self, name: str) -> None:
        """Write-side of :attr:`active_output_style`."""
        self._active_output_style = name

    @property
    def disabled_plugins(self) -> set[str]:
        """Plugin names currently disabled by ``plugin_state``."""
        return self._disabled_plugins

    @property
    def plugin_data_dir(self) -> str:
        """Root directory for the plugin registry / installer / state.

        Named seam so slash commands and controllers stop reaching
        through ``session.settings.storage.data_dir`` (a three-level
        Demeter chain). Read fresh from settings on every access, so
        a mid-session settings reload is picked up rather than
        snapshotted at command entry.
        """
        return self.settings.storage.data_dir

    @property
    def learning(self) -> Any:
        """The Agno :class:`LearningMachine` for this session, or
        ``None`` when learning is disabled. Forwards to
        :attr:`SessionLearningManager.machine`."""
        return self.learning_mgr.machine

    @property
    def _learning(self) -> Any:
        """Compat alias — some legacy code paths / tests read
        ``session._learning`` directly. Forwards to
        :attr:`SessionLearningManager.machine`."""
        return self.learning_mgr.machine

    @_learning.setter
    def _learning(self, value: Any) -> None:
        """Compat setter — reroutes writes to the coordinator's
        internal ``_learning`` field. Tests that seed a stub
        learning machine keep working.
        """
        self.learning_mgr._learning = value

    @property
    def learning_machine(self) -> Any:
        """Effective Learning Machine for user-facing recall
        commands. Forwards to
        :attr:`SessionLearningManager.effective_machine`."""
        return self.learning_mgr.effective_machine

    @property
    def knowledge_error(self) -> str | None:
        """Human-readable error string from the last knowledge
        base initialisation attempt.
        """
        return getattr(self, "_knowledge_error", None)

    @property
    def _mcp_initialized(self) -> bool:
        """Compat shim — reads through to the
        :class:`SessionStartupCoordinator`.
        """
        return self._startup_coord().mcp_initialized

    @_mcp_initialized.setter
    def _mcp_initialized(self, value: bool) -> None:
        self._startup_coord().mcp_initialized = value

    def tool_event_hook(self) -> ToolEventHook:
        """Public wrapper for the ``ToolEventHook`` factory."""
        return self._create_tool_event_hook()

    def resolve_main_tool_names(self, registry: "ToolRegistry") -> list[str]:
        """Public wrapper for the main toolkit-name composer."""
        return self._resolve_main_tool_names(registry)

    def build_agent_catalog(self) -> str:
        """Public wrapper for the specialist-agent catalog builder."""
        return self._build_agent_catalog()

    def latch_input_tokens(self, n: int) -> None:
        """Public setter for the last-run input-token count."""
        self._last_input_tokens = n

    # ── Identity delegation ────────────────────────────────────

    @property
    def session_id(self) -> str:
        """The active session id. Forwards to
        :attr:`SessionIdentity.session_id`.
        """
        return self.identity.session_id

    @session_id.setter
    def session_id(self, value: str) -> None:
        """Compat setter — tests that seed ``session.session_id``
        directly keep working. Note: this does NOT propagate to
        the main team / persistence; use :meth:`rotate_id` for
        the full three-attribute rotation.
        """
        self.identity._session_id = value

    @property
    def session_named(self) -> bool:
        """Whether the session was resumed or renamed. Forwards
        to :attr:`SessionIdentity.session_named`.
        """
        return self.identity.session_named

    @session_named.setter
    def session_named(self, value: bool) -> None:
        """Compat setter — see :attr:`session_id`."""
        self.identity._session_named = value

    @property
    def user_id(self) -> str:
        """The active user id. Forwards to
        :attr:`SessionIdentity.user_id`.
        """
        return self.identity.user_id

    def rotate_id(self, new_id: str) -> None:
        """Delegate to :meth:`SessionIdentity.rotate`."""
        self.identity.rotate(new_id)

    async def rebind_identity(self, session_id: str) -> None:
        """Delegate to :meth:`SessionIdentity.rebind`."""
        await self.identity.rebind(session_id)

    @property
    def last_input_tokens(self) -> int:
        """Read the latched input-token count from the most recent
        completed run."""
        return self._last_input_tokens

    # ── Learning delegation ────────────────────────────────────

    async def inject_learnings(self) -> None:
        """Delegate to :meth:`SessionLearningManager.inject`."""
        await self.learning_mgr.inject()

    async def _inject_learnings(self) -> None:
        """Compat alias — delegate to :meth:`inject_learnings`."""
        await self.learning_mgr.inject()

    async def extract_learnings(self, user_msg: str, assistant_msg: str) -> None:
        """Delegate to :meth:`SessionLearningManager.extract`."""
        await self.learning_mgr.extract(user_msg, assistant_msg)

    @property
    def permission_mode_value(self) -> str:
        """Wire-safe string for the current permission-mode.

        Coerces both plain-string and Enum-shaped ``mode``
        attributes to a well-known string. Falls back to
        ``"default"`` for non-string results (test fixtures with
        MagicMock ``.mode.value``).
        """
        evaluator = getattr(self, "permission_evaluator", None)
        raw_mode = getattr(evaluator, "mode", None)
        raw_val = getattr(raw_mode, "value", raw_mode)
        return raw_val if isinstance(raw_val, str) else "default"

    def _build_agent_catalog(self) -> str:
        """Build a text catalog of specialist agents for the system prompt."""
        lines = []
        for defn in self.pool.list_agents():
            tools_str = ", ".join(defn.tools) if defn.tools else "none"
            lines.append(f"- **{defn.name}**: {defn.description} (tools: {tools_str})")
        return "\n".join(lines)

    # ── Reminder-queue compat surface ──────────────────────────

    def _ensure_reminder_queue(self) -> PendingReminderQueue:
        """Lazily materialise :attr:`_reminder_queue`.

        Bare-Session test stubs (``Session.__new__(Session)``)
        skip ``__init__`` so the queue may not exist yet — the
        first read / write reaches through this helper so the
        compat surface never trips on ``AttributeError``.
        """
        queue = getattr(self, "_reminder_queue", None)
        if not isinstance(queue, PendingReminderQueue):
            queue = PendingReminderQueue()
            self._reminder_queue = queue
        return queue

    @property
    def _pending_reminders(self) -> list[str]:
        """Compat alias — reads the live buffer from the
        :class:`PendingReminderQueue`. Existing tests seed via
        ``session._pending_reminders.append(...)`` or set the list
        wholesale via ``session._pending_reminders = []`` — both
        idioms keep working.
        """
        return self._ensure_reminder_queue().pending

    @_pending_reminders.setter
    def _pending_reminders(self, value: list[str]) -> None:
        self._ensure_reminder_queue().replace(value)

    def _queue_rewake(self, text: str) -> None:
        """Delegate to :meth:`PendingReminderQueue.queue`."""
        self._ensure_reminder_queue().queue(text)

    def _drain_pending_reminders(self) -> list[str]:
        """Delegate to :meth:`PendingReminderQueue.drain`."""
        return self._ensure_reminder_queue().drain()

    def _mcp_resolver(self, server: str, tool: str) -> Any | None:
        """Delegate to :meth:`MCPToolResolver.resolve`.

        Kept as a bound method (with the ``_`` prefix) so
        existing test fixtures that reach for
        ``session._mcp_resolver(server, tool)`` keep working.
        Materialises a transient :class:`MCPToolResolver` for
        bare-Session test stubs so they don't need to compose
        ``_mcp_resolver_obj`` by hand.
        """
        resolver = getattr(self, "_mcp_resolver_obj", None)
        if resolver is None:
            resolver = MCPToolResolver(mcp_manager_ref=lambda: getattr(self, "mcp_manager", None))
            self._mcp_resolver_obj = resolver
        return resolver.resolve(server, tool)

    def _create_tool_event_hook(self) -> ToolEventHook:
        """Delegate to :meth:`ToolEventHookFactory.create`.

        Additionally writes the cached
        :class:`PermissionEvaluator` onto
        ``session.permission_evaluator`` for the legacy attribute
        accessors that reach through the session.
        """
        self._tool_hook_factory.evaluator_for_session(self)
        return self._tool_hook_factory.create()

    # ── Runtime-mode delegation (output style + permission mode) ──

    def _mode_coord(self) -> RuntimeModeCoordinator:
        """Return the :class:`RuntimeModeCoordinator` for this session.

        Materialises a transient coordinator on demand for
        bare-Session test stubs (``Session.__new__(Session)`` +
        manual attribute wiring) that don't run ``__init__``.
        """
        coord = getattr(self, "mode", None)
        if not isinstance(coord, RuntimeModeCoordinator):
            coord = RuntimeModeCoordinator(self)
            self.mode = coord
        return coord

    def set_output_style(self, name: str) -> str:
        """Delegate to :meth:`RuntimeModeCoordinator.set_output_style`."""
        return self._mode_coord().set_output_style(name)

    def set_permission_mode(self, mode: str) -> str:
        """Delegate to :meth:`RuntimeModeCoordinator.set_permission_mode`."""
        return self._mode_coord().set_permission_mode(mode)

    # ── Plan decision delegation ────────────────────────────────

    def _plan_coord(self) -> PlanCoordinator:
        """Return the :class:`PlanCoordinator` for this session."""
        coord = getattr(self, "plan", None)
        if not isinstance(coord, PlanCoordinator):
            coord = PlanCoordinator(self)
            self.plan = coord
        return coord

    async def approve_plan(self, run_id: str) -> PlanDecisionResult:
        """Delegate to :meth:`PlanCoordinator.approve`."""
        return await self._plan_coord().approve(run_id)

    async def dismiss_plan(self, run_id: str) -> PlanDecisionResult:
        """Delegate to :meth:`PlanCoordinator.dismiss`."""
        return await self._plan_coord().dismiss(run_id)

    async def _record_plan_decision(
        self, run_id: str, decision: str, *, flip_mode: bool
    ) -> PlanDecisionResult:
        """Delegate to :meth:`PlanCoordinator._record`."""
        return await self._plan_coord()._record(run_id, PlanDecision(decision), flip_mode=flip_mode)

    # ── Broadcast facade ───────────────────────────────────────

    def register_broadcast_callback(self, callback) -> None:
        """Register a ``(channel, payload) -> None`` subscriber on
        the broadcast bus.
        """
        self.broadcast_bus.register(callback)

    async def append_event(
        self,
        event_type: str,
        payload: dict,
        run_id: str = "",
    ) -> None:
        """Delegate to :meth:`SessionEventLog.append`."""
        await self._ensure_event_log_store().append(event_type, payload, run_id)

    def restore_event_log(self, events: list[SessionEvent]) -> None:
        """Delegate to :meth:`SessionEventLog.restore`."""
        self._ensure_event_log_store().restore(events)

    def _ensure_event_log_store(self) -> SessionEventLog:
        """Lazily materialise :attr:`event_log_store`.

        Bare-Session test stubs bypass ``__init__``; the first
        read / write on the compat surface reaches through this
        helper so the store attribute always exists.
        """
        store = getattr(self, "event_log_store", None)
        if not isinstance(store, SessionEventLog):
            store = SessionEventLog(persist_ref=lambda: getattr(self, "persistence", None))
            self.event_log_store = store
        return store

    @property
    def event_log(self) -> list[SessionEvent]:
        """Live reference to the in-memory event log.

        Kept as a live-mutable list (not a snapshot) so callers
        that historically wrote to ``session.event_log`` — e.g.
        test fixtures that seed rows directly — keep working.
        """
        return self._ensure_event_log_store().events

    @event_log.setter
    def event_log(self, value: list[SessionEvent]) -> None:
        """Compat setter — mirrors the legacy fixture pattern."""
        self._ensure_event_log_store().events = value

    @property
    def _event_seq(self) -> int:
        """Compat alias — reads the seq counter from the
        coordinator. Tests that inspect the counter directly keep
        working.
        """
        return self._ensure_event_log_store().seq

    @_event_seq.setter
    def _event_seq(self, value: int) -> None:
        """Compat setter — seeds the seq counter for fixtures."""
        self._ensure_event_log_store()._seq = value

    def broadcast(self, channel: str, payload: dict) -> None:
        """Fire an event through the broadcast bus."""
        self.broadcast_bus.emit(BroadcastEvent(channel=channel, payload=payload))

    def broadcast_event(self, event: BroadcastEvent) -> None:
        """Typed-event companion to :meth:`broadcast`."""
        self.broadcast_bus.emit(event)

    def queue_post_run_broadcast(self, channel: str, payload: dict) -> None:
        """Defer a broadcast until the current run finishes."""
        self.broadcast_bus.queue_post_run(BroadcastEvent(channel=channel, payload=payload))

    def drain_post_run_broadcasts(self, run_id: str | None = None) -> None:
        """Flush the post-run broadcast queue."""
        self.broadcast_bus.drain_post_run(run_id)

    # ── Loop-state proxies + delegation (thin wrappers over LoopController) ──

    @property
    def pending_loop_prompt(self) -> str | None:
        """The active iteration prompt, or ``None`` when idle."""
        return self.loop.pending_loop_prompt

    @property
    def loop_iteration_index(self) -> int:
        """1-based counter of iterations dispatched."""
        return self.loop.loop_iteration_index

    @property
    def loop_iterations_remaining(self) -> int:
        """Safety-net budget remaining."""
        return self.loop.loop_iterations_remaining

    @property
    def loop_run_id(self) -> str | None:
        """UUID scoping :class:`LoopProgressStore` writes."""
        return self.loop.loop_run_id

    @property
    def loop_cap_explicit(self) -> bool:
        """Whether the user typed ``/loop N <prompt>``."""
        return self.loop.loop_cap_explicit

    @property
    def loop_paused(self) -> bool:
        """Whether the loop is paused waiting for
        :meth:`resume_loop`.
        """
        return self.loop.paused

    async def load_persisted_loop_state(self) -> None:
        """Delegate to :meth:`LoopController.load_persisted_loop_state`."""
        await self.loop.load_persisted_loop_state()

    async def start_loop(
        self,
        prompt: str,
        max_iter: int,
        *,
        immediate: bool,
        cap_explicit: bool,
    ) -> str:
        """Delegate to :meth:`LoopController.start_loop`."""
        return await self.loop.start_loop(
            prompt, max_iter, immediate=immediate, cap_explicit=cap_explicit
        )

    async def advance_loop(self) -> LoopAdvance | None:
        """Delegate to :meth:`LoopController.advance_loop`."""
        return await self.loop.advance_loop()

    async def cancel_loop(self) -> bool:
        """Delegate to :meth:`LoopController.cancel_loop`."""
        return await self.loop.cancel_loop()

    async def pause_loop(self) -> bool:
        """Delegate to :meth:`LoopController.pause_loop`."""
        return await self.loop.pause_loop()

    async def resume_loop(self) -> str | None:
        """Delegate to :meth:`LoopController.resume_loop`."""
        return await self.loop.resume_loop()

    async def _persist_loop_state(self) -> None:
        """Delegate to :meth:`LoopController.persist_loop_state`."""
        await self.loop.persist_loop_state()

    # ── Loop tool-facing delegators (used by LoopTools) ────────
    #
    # Each of these forwards to the matching ``*_from_tool``
    # method on :class:`LoopController`. ``set_announced_total_from_tool``
    # additionally threads the session's :class:`LoopProgressStore`
    # through — the controller doesn't own the progress store
    # (it's a peer on the session), so wiring them together
    # happens here at the composition root.

    async def start_loop_from_tool(self, prompt: str, max_iterations: int) -> LoopToolResult:
        """Delegate to :meth:`LoopController.start_loop_from_tool`."""
        return await self.loop.start_loop_from_tool(prompt, max_iterations)

    async def stop_loop_from_tool(self) -> LoopToolResult:
        """Delegate to :meth:`LoopController.stop_loop_from_tool`."""
        return await self.loop.stop_loop_from_tool()

    async def set_announced_total_from_tool(self, total: int) -> LoopToolResult:
        """Delegate to :meth:`LoopController.set_announced_total_from_tool`.

        Threads the session-owned :class:`LoopProgressStore` through
        so the controller can persist without reaching upward.
        """
        return await self.loop.set_announced_total_from_tool(total, self.loop_progress_store)

    async def resume_loop_from_tool(self) -> LoopToolResult:
        """Delegate to :meth:`LoopController.resume_loop_from_tool`."""
        return await self.loop.resume_loop_from_tool()

    def loop_status_from_tool(self) -> LoopToolResult:
        """Delegate to :meth:`LoopController.loop_status_from_tool`."""
        return self.loop.loop_status_from_tool()

    # ── Knowledge / codeindex / marketplace / MCP startup ──────

    def _startup_coord(self) -> SessionStartupCoordinator:
        """Return the :class:`SessionStartupCoordinator`."""
        coord = getattr(self, "startup", None)
        if not isinstance(coord, SessionStartupCoordinator):
            coord = SessionStartupCoordinator(self)
            self.startup = coord
        return coord

    def start_knowledge_background(self) -> None:
        """Delegate to
        :meth:`SessionStartupCoordinator.start_knowledge_background`."""
        self._startup_coord().start_knowledge_background()

    async def _ensure_knowledge(self) -> None:
        """Delegate to
        :meth:`SessionStartupCoordinator.ensure_knowledge_started`."""
        await self._startup_coord().ensure_knowledge_started()

    def start_codeindex_background(self) -> None:
        """Delegate to
        :meth:`SessionStartupCoordinator.start_codeindex_background`."""
        self._startup_coord().start_codeindex_background()

    def start_marketplace_refresh_background(self) -> None:
        """Delegate to
        :meth:`SessionStartupCoordinator.start_marketplace_refresh_background`."""
        self._startup_coord().start_marketplace_refresh_background()

    async def ensure_mcp(self) -> McpInitResult:
        """Delegate to :meth:`SessionStartupCoordinator.ensure_mcp`."""
        return await self._startup_coord().ensure_mcp()

    def rebuild_mcp(self) -> None:
        """Delegate to :meth:`SessionStartupCoordinator.rebuild_mcp`."""
        self._startup_coord().rebuild_mcp()

    def refresh_codeindex_availability(self) -> RefreshAvailabilityResult:
        """Delegate to
        :meth:`CodeIndexAvailabilityRefresher.refresh`.

        Bare-Session test stubs may not have composed the
        refresher (they set ``session.code_index`` etc. by hand);
        materialise a transient one from the current session
        attributes so those fixtures keep working. Attribute
        errors during the lazy build are folded into an
        ``ok=False`` envelope — matches the pre-refactor
        bare-except behaviour.
        """
        refresher = getattr(self, "_codeindex_refresher", None)
        if refresher is None:
            try:
                refresher = CodeIndexAvailabilityRefresher(
                    settings=self.settings,
                    project_dir=getattr(self, "project_dir", None),
                    code_index=getattr(self, "code_index", None),
                    code_index_sync=getattr(self, "code_index_sync", None),
                    pool_ref=lambda: getattr(self, "pool", None),
                    plugin_loader_ref=lambda: getattr(self, "plugin_loader", None),
                    disabled_plugins_ref=lambda: getattr(self, "_disabled_plugins", set()),
                    mcp_manager_ref=lambda: getattr(self, "mcp_manager", None),
                    build_main_agent=lambda: self._build_main_agent(),
                    assign_main_team=self._assign_main_team,
                    get_availability=lambda: getattr(self, "_codeindex_available", False),
                    set_availability=self._set_codeindex_available,
                )
            except Exception as exc:  # noqa: BLE001 — lazy-build safety envelope
                logger.debug("refresh_codeindex_availability lazy-build failed (%s)", exc)
                return RefreshAvailabilityResult(ok=False, changed=False, error=str(exc))
            self._codeindex_refresher = refresher
        return refresher.refresh()

    # ── MCP status ─────────────────────────────────────────────

    def get_mcp_status(self) -> list[McpServerStatus]:
        """Return typed rows of ``(name, connected)`` for every
        configured MCP server.
        """
        available = set(self.mcp_manager.list_servers())
        connected = set(self.mcp_manager.list_connected())
        return [McpServerStatus(name=name, connected=name in connected) for name in available]

    # ── Dynamic context compaction ─────────────────────────────

    async def compact_if_needed(self, input_tokens: int, context_window: int) -> bool:
        """Delegate to :meth:`CompactionCoordinator.compact_if_needed`."""
        return await self.compaction.compact_if_needed(input_tokens, context_window)

    async def force_compact(self) -> CompactResult:
        """Delegate to :meth:`CompactionCoordinator.force_compact`."""
        return await self.compaction.force_compact()

    async def context_breakdown(self) -> ContextBreakdown:
        """Delegate to :meth:`CompactionCoordinator.context_breakdown`."""
        return await self.compaction.context_breakdown()

    # ── Debug logging ─────────────────────────────────────────

    def _log_run_messages(self) -> None:
        """Dump the team's last run at DEBUG level via
        :meth:`RunMessagesDebugDumper.dump_team`."""
        RunMessagesDebugDumper.dump_team(self.main_team)

    # ── Message handling (headless path) ──────────────────────

    async def handle_message(
        self,
        message: str,
        *,
        media: MessageMedia | None = None,
        **media_kwargs: Any,
    ) -> str:
        """Handle a single user message and return the response.

        Accepts either the typed :class:`MessageMedia` model
        (preferred, Rule 1) OR legacy ``**media_kwargs`` (images,
        audio, videos, files). During the transition both forms
        are accepted; new callers should pass ``media=``.

        Delegates the six-step pipeline to
        :class:`SessionMessageHandler`; a diagnostic dump fires
        via :class:`RunMessagesDebugDumper` between the model
        response and the Stop-hook retry loop.
        """
        # Compose the effective kwargs: an explicit ``media`` wins,
        # anything else falls back to the raw kwargs shape Agno's
        # ``arun`` already accepts.
        effective_kwargs: dict[str, Any] = media.to_kwargs() if media is not None else media_kwargs
        result = await self._message_handler.handle(message, **effective_kwargs)
        # Debug dumper is a side-observer — always safe to fire
        # after the pipeline returns. Route through the module-level
        # shim so ``patch("...core._log_run_messages_debug")`` in
        # tests still intercepts.
        with contextlib.suppress(Exception):
            _log_run_messages_debug(self.main_team)
        return result
