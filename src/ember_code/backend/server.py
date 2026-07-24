"""Backend server — the composition root.

Owns the Session object and all Agno/AI logic. The FE never touches
Session directly — everything goes through protocol messages
routed via :mod:`ember_code.backend.rpc_router`.

Structural layout (post-refactor):

* :class:`BackendBootstrap` builds every long-lived collaborator
  (``Session``, stores, tracer) with all imports hoisted to module
  top — no more inline-import cycle workarounds.
* :class:`ControllerRegistry` builds the :class:`Controllers` bag
  eagerly — replaces the 17 duplicated lazy-init ``@property``
  blocks that previously lived here.
* :class:`RunController` owns the run pipeline (lock, current
  task, checkpoint, cancel).
* :class:`HitlController` owns pause-handling, stream-muxing,
  requirement sweeps.

Every FE-facing method on :class:`BackendServer` is a one-line
delegate into the appropriate controller. Legacy underscore-
prefixed seams (``_run_message_locked``, ``_handle_pause``,
``_close_model_http_client``, ``_periodic_checkpoint``, …) are
preserved as forwarders so tests that ``patch.object`` on them
continue to intercept without changes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agno.agent import Agent  # noqa: F401 — test-patch target

from ember_code.backend.backend_bootstrap import BackendBootstrap
from ember_code.backend.controller_registry import ControllerRegistry, Controllers
from ember_code.backend.hitl_controller import HitlController
from ember_code.backend.hitl_stream_mux import (
    HITLStreamMultiplexer,  # noqa: F401 — legacy re-export
)
from ember_code.backend.marketplace_controller import MarketplaceController
from ember_code.backend.model_switcher import ModelSwitcher
from ember_code.backend.pause_handler import PauseHandler  # noqa: F401 — legacy re-export
from ember_code.backend.plan_snapshot_builder import PlanSnapshotBuilder
from ember_code.backend.plugin_controller import PluginController
from ember_code.backend.plugin_schemas import PluginContents
from ember_code.backend.run_controller import RunController
from ember_code.backend.schemas_hitl import ToolCallArgs
from ember_code.backend.schemas_pause import PauseHandleResult
from ember_code.backend.schemas_run import CancelAgentRunResult, MediaAttachments
from ember_code.backend.schemas_sessions import AutoNameResult
from ember_code.backend.server_auth import AuthController
from ember_code.backend.server_codeindex import CodeIndexController
from ember_code.backend.server_context import ContextController
from ember_code.backend.server_files import FilesController
from ember_code.backend.server_history import ChatHistoryRebuilder
from ember_code.backend.server_knowledge import KnowledgeController
from ember_code.backend.server_lifecycle import LifecycleController
from ember_code.backend.server_loop import LoopController
from ember_code.backend.server_mcp import McpController
from ember_code.backend.server_panels import PanelsController
from ember_code.backend.server_processes import ProcessesController
from ember_code.backend.server_rehydrate import RehydrateController
from ember_code.backend.server_search import SearchController
from ember_code.backend.server_sessions import SessionsController
from ember_code.backend.team_wiring import TeamWiring
from ember_code.backend.visualization_action_bus import VisualizationActionBus
from ember_code.core.config.user_config_store import UserConfigStore
from ember_code.protocol import messages as msg

if TYPE_CHECKING:
    from ember_code.backend.schemas_codeindex_rpc import (
        CodeIndexActivityEntry,
        CodeIndexCleanResult,
        CodeIndexHeadBreakdown,
        CodeIndexInstallResult,
        CodeIndexStatus,
        CodeIndexSyncResult,
    )
    from ember_code.backend.schemas_context import PendingMessage, TruncateHistoryResult
    from ember_code.backend.schemas_history import ChatSearchHit
    from ember_code.backend.schemas_hitl import RunRequirement
    from ember_code.backend.schemas_knowledge import KnowledgeStatus
    from ember_code.backend.schemas_lifecycle import RehydrateOutcome
    from ember_code.backend.schemas_loop import LoopStatusSnapshot
    from ember_code.backend.schemas_mcp import (
        MCPServerSnapshot,
        MCPServerSummary,
        MCPToolToggleResult,
    )
    from ember_code.backend.schemas_panels import (
        DiscardEphemeralResult,
        HookEntryView,
        OutputStylesResult,
        PromoteEphemeralResult,
        SlashCommandEntry,
    )
    from ember_code.backend.schemas_plan import LatestPlanResult
    from ember_code.backend.schemas_rpc import CloudPlan, LoginResult
    from ember_code.backend.schemas_search import SearchCodeResult
    from ember_code.backend.schemas_visualization import VisualizationActionResult
    from ember_code.backend.server_files import (
        ReadFileResult,
        UploadAttachmentResult,
    )
    from ember_code.backend.server_knowledge import (
        KnowledgeGetResult,
        KnowledgeHit,
        KnowledgeListEntry,
        KnowledgeRemoveResult,
    )
    from ember_code.core.agents import AgentInfo
    from ember_code.core.config.settings import Settings
    from ember_code.core.config.tool_permissions import PermissionLevel
    from ember_code.core.plugins.models import MarketplaceInfo, PluginInfo
    from ember_code.core.session.loop_ops import LoopAdvance
    from ember_code.core.session.schemas import (
        McpInitResult,
        McpServerStatus,
        PlanDecisionResult,
    )
    from ember_code.core.skills import SkillPool
    from ember_code.core.skills.parser import SkillInfo

logger = logging.getLogger(__name__)


class BackendServer:
    """Composition root wrapping :class:`Session` and every
    sub-controller.

    :meth:`__init__` fires :class:`BackendBootstrap` to construct
    the shared collaborators, then :class:`ControllerRegistry` to
    build the :class:`Controllers` bag. Every FE-facing method is
    a one-liner into ``self.controllers.<name>``.

    Legacy underscore-prefixed methods (``_run_message_locked``,
    ``_handle_pause``, ``_close_model_http_client``, …) are kept
    as forwarders so tests that ``patch.object`` on them still
    intercept.
    """

    def __init__(
        self,
        settings: Settings,
        project_dir: Path | None = None,
        resume_session_id: str | None = None,
        additional_dirs: list[Path] | None = None,
    ):
        bootstrap = BackendBootstrap(
            settings=settings,
            project_dir=project_dir,
            resume_session_id=resume_session_id,
            additional_dirs=additional_dirs,
        )
        self._session = bootstrap.session
        self._settings = bootstrap.settings
        self._session_prefs = bootstrap.session_prefs
        self._hitl_store = bootstrap.hitl_store
        self._hitl_tracer = bootstrap.hitl_tracer
        self._pending_store = bootstrap.pending_store

        self.controllers: Controllers = ControllerRegistry.build(
            backend=self,
            session=self._session,
            settings=settings,
            hitl_store=self._hitl_store,
            pending_store=self._pending_store,
            session_prefs=self._session_prefs,
            user_config_store=bootstrap.user_config_store,
            hitl_tracer=self._hitl_tracer,
        )
        # Alias for the run pipeline attribute the LifecycleController
        # composes at construction — kept as a name on the server for
        # any legacy caller that reaches through ``server._runs``.
        self._runs: RunController = self.controllers.runs

    # ── Runtime attach (SessionOrchestrator uses this) ─────────────

    def attach_runtime(self, runtime: Any) -> None:
        """Called by :class:`SessionOrchestrator` after building the
        default :class:`SessionRuntime` and again per pool runtime.
        Exposed so :class:`MessageDispatcher` can reach the runtime
        via :attr:`runtime` without dunder gymnastics."""
        self._runtime = runtime

    @property
    def runtime(self) -> Any:
        # Partial-init tolerance — tests build ``BackendServer`` via
        # ``__new__`` and never attach.
        return getattr(self, "_runtime", None)

    @property
    def project_dir(self) -> Path:
        return self._session.project_dir

    # ── Controller accessors (thin properties over Controllers bag) ──
    #
    # Every accessor tolerates the ``__new__``-bypass test path — if
    # ``self.controllers`` was never built (test fixture) the property
    # falls back to a fresh single-shot controller against whatever
    # session/state attributes the test wired manually. This preserves
    # the pre-refactor lazy-init behaviour without duplicating the
    # construction on every property.

    def _controllers_or_partial(self) -> Controllers | None:
        return getattr(self, "controllers", None)

    @property
    def plugins(self) -> PluginController:
        """Plugin lifecycle controller."""
        ctrl = self._controllers_or_partial()
        if ctrl is not None:
            return ctrl.plugins
        cached = getattr(self, "_plugin_controller", None)
        if cached is None:
            cached = PluginController(self._session)
            self._plugin_controller = cached
        return cached

    @property
    def marketplaces(self) -> MarketplaceController:
        """Marketplace controller."""
        ctrl = self._controllers_or_partial()
        if ctrl is not None:
            return ctrl.marketplaces
        cached = getattr(self, "_marketplace_controller", None)
        if cached is None:
            cached = MarketplaceController(self._session)
            self._marketplace_controller = cached
        return cached

    @property
    def mcp(self) -> McpController:
        """MCP lifecycle controller."""
        ctrl = self._controllers_or_partial()
        if ctrl is not None:
            return ctrl.mcp
        cached = getattr(self, "_mcp_ctrl", None)
        if cached is None:
            cached = McpController(self._session)
            self._mcp_ctrl = cached
        return cached

    @property
    def hitl(self) -> HitlController:
        """HITL resolution + permission-rule controller."""
        ctrl = self._controllers_or_partial()
        if ctrl is not None:
            return ctrl.hitl
        cached = getattr(self, "_hitl_controller", None)
        if cached is None:
            cached = HitlController(
                session=self._session,
                store=self._hitl_store,
                stream_factory=self._stream_with_subagent_hitl,
            )
            self._hitl_controller = cached
        return cached

    @property
    def context(self) -> ContextController:
        """Context / status / compaction / learning controller."""
        ctrl = self._controllers_or_partial()
        if ctrl is not None:
            return ctrl.context
        cached = getattr(self, "_context_ctrl", None)
        if cached is None:
            cached = ContextController(
                session=getattr(self, "_session", None),
                settings=getattr(self, "_settings", None),
                pending_store=getattr(self, "_pending_store", None),
            )
            self._context_ctrl = cached
        return cached

    @property
    def auth(self) -> AuthController:
        """Cloud auth controller."""
        ctrl = self._controllers_or_partial()
        if ctrl is not None:
            return ctrl.auth
        cached = getattr(self, "_auth_ctrl", None)
        if cached is None:
            cached = AuthController(
                session=self._session,
                settings=self._settings,
                status_provider=self.get_status,
            )
            self._auth_ctrl = cached
        return cached

    @property
    def knowledge(self) -> KnowledgeController:
        """Knowledge-base controller."""
        ctrl = self._controllers_or_partial()
        if ctrl is not None:
            return ctrl.knowledge
        cached = getattr(self, "_knowledge_ctrl", None)
        if cached is None:
            cached = KnowledgeController(self._session)
            self._knowledge_ctrl = cached
        return cached

    @property
    def files(self) -> FilesController:
        """File-I/O controller."""
        ctrl = self._controllers_or_partial()
        if ctrl is not None:
            return ctrl.files
        cached = getattr(self, "_files_ctrl", None)
        if cached is None:
            cached = FilesController(self._session)
            self._files_ctrl = cached
        return cached

    @property
    def search(self) -> SearchController:
        """Composer-paste code-search controller."""
        ctrl = self._controllers_or_partial()
        if ctrl is not None:
            return ctrl.search
        cached = getattr(self, "_search_controller", None)
        if cached is None:
            cached = SearchController(self._session)
            self._search_controller = cached
        return cached

    @property
    def panels(self) -> PanelsController:
        """Panel-details controller."""
        ctrl = self._controllers_or_partial()
        if ctrl is not None:
            return ctrl.panels
        cached = getattr(self, "_panels_ctrl", None)
        if cached is None:
            cached = PanelsController(self._session)
            self._panels_ctrl = cached
        return cached

    @property
    def codeindex(self) -> CodeIndexController:
        """CodeIndex panel controller."""
        ctrl = self._controllers_or_partial()
        if ctrl is not None:
            return ctrl.codeindex
        cached = getattr(self, "_codeindex_ctrl", None)
        if cached is None:
            cached = CodeIndexController(self._session)
            self._codeindex_ctrl = cached
        return cached

    @property
    def loop(self) -> LoopController:
        """``/loop`` + scheduler controller."""
        ctrl = self._controllers_or_partial()
        if ctrl is not None:
            return ctrl.loop
        cached = getattr(self, "_loop_controller", None)
        if cached is None:
            cached = LoopController(
                session=self._session,
                settings=getattr(self, "_settings", None),
            )
            self._loop_controller = cached
        return cached

    @property
    def processes(self) -> ProcessesController:
        """Background-process watcher controller."""
        ctrl = self._controllers_or_partial()
        if ctrl is not None:
            return ctrl.processes
        cached = getattr(self, "_processes_ctrl", None)
        if cached is None:
            cached = ProcessesController()
            self._processes_ctrl = cached
        return cached

    @property
    def sessions(self) -> SessionsController:
        """Session lifecycle controller."""
        ctrl = self._controllers_or_partial()
        if ctrl is not None:
            return ctrl.sessions
        cached = getattr(self, "_sessions_ctrl", None)
        if cached is None:
            cached = SessionsController(
                session=self._session,
                chat_history_provider=self.get_chat_history,
            )
            self._sessions_ctrl = cached
        return cached

    @property
    def rehydrate(self) -> RehydrateController:
        """Boot-time state-recovery controller."""
        ctrl = self._controllers_or_partial()
        if ctrl is not None:
            return ctrl.rehydrate
        cached = getattr(self, "_rehydrate_ctrl", None)
        if cached is None:
            cached = RehydrateController(self._session)
            self._rehydrate_ctrl = cached
        return cached

    @property
    def lifecycle(self) -> LifecycleController:
        """Startup / shutdown / interrupted-run detection controller."""
        ctrl = self._controllers_or_partial()
        if ctrl is not None:
            return ctrl.lifecycle
        cached = getattr(self, "_lifecycle_ctrl", None)
        if cached is None:
            cached = LifecycleController(
                session=self._session,
                pending_store=getattr(self, "_pending_store", None),
                runs=getattr(self, "_runs", None),
                rehydrate=self.rehydrate,
                scheduler_stop=self.loop.scheduler.stop,
                backend=self,
            )
            self._lifecycle_ctrl = cached
        return cached

    @property
    def plan_snapshots(self) -> PlanSnapshotBuilder:
        """Plan / todos snapshot builder."""
        ctrl = self._controllers_or_partial()
        if ctrl is not None:
            return ctrl.plan_snapshots
        cached = getattr(self, "_plan_snapshots", None)
        if cached is None:
            cached = PlanSnapshotBuilder(self._session)
            self._plan_snapshots = cached
        return cached

    @property
    def viz_actions(self) -> VisualizationActionBus:
        """json-render visualization action bus."""
        ctrl = self._controllers_or_partial()
        if ctrl is not None:
            return ctrl.viz_actions
        cached = getattr(self, "_viz_actions", None)
        if cached is None:
            cached = VisualizationActionBus(self._session)
            self._viz_actions = cached
        return cached

    @property
    def team_wiring(self) -> TeamWiring:
        """Team hook + progress-callback wiring collaborator."""
        ctrl = self._controllers_or_partial()
        if ctrl is not None:
            return ctrl.team_wiring
        cached = getattr(self, "_team_wiring", None)
        if cached is None:
            cached = TeamWiring(self._session)
            self._team_wiring = cached
        return cached

    @property
    def model_switcher(self) -> ModelSwitcher:
        """Model-switch logic owner."""
        ctrl = self._controllers_or_partial()
        if ctrl is not None:
            return ctrl.model_switcher
        cached = getattr(self, "_model_switcher", None)
        if cached is None:
            cached = ModelSwitcher(
                session=self._session,
                session_prefs=self._session_prefs,
                user_config_store=UserConfigStore(),
            )
            self._model_switcher = cached
        return cached

    # ── Run pipeline exposure — properties forward to the pipeline ──
    #
    # ``_run_lock`` and ``_current_run_task`` used to be raw instance
    # attributes on :class:`BackendServer`. Ownership has moved to
    # :class:`RunController` (Pattern 1 — single owner of run phase +
    # task) but tests build partial ``__new__``-bypass instances that
    # set the fields directly (``server._run_lock = asyncio.Lock()``).
    # Getter routes to the pipeline when present; setter stores on
    # the instance so those test fixtures keep working. Once the
    # test fixtures are migrated, both wrappers can be deleted.

    @property
    def _run_lock(self) -> asyncio.Lock:
        """Compat alias for the outer serialization lock."""
        runs = getattr(self, "_runs", None)
        if runs is not None:
            return runs.run_lock
        return self.__dict__.get("_run_lock", asyncio.Lock())

    @_run_lock.setter
    def _run_lock(self, value: asyncio.Lock) -> None:
        """Compat setter — used by ``__new__``-bypass test fixtures."""
        self.__dict__["_run_lock"] = value

    @property
    def _current_run_task(self) -> asyncio.Task | None:
        """Compat alias for the currently-running ``asyncio.Task``."""
        runs = getattr(self, "_runs", None)
        if runs is not None:
            return runs.current_run_task
        return self.__dict__.get("_current_run_task")

    @_current_run_task.setter
    def _current_run_task(self, value: asyncio.Task | None) -> None:
        """Compat setter — route to the pipeline's private slot when
        the pipeline exists so tests + production stay in sync.
        Falls back to a raw ``__dict__`` write when the pipeline
        hasn't been wired up yet."""
        runs = getattr(self, "_runs", None)
        if runs is not None:
            runs._current_run_task = value
        else:
            self.__dict__["_current_run_task"] = value

    # ── Public seams for coordinators ──────────────────────────────

    async def approve_plan(self, run_id: str) -> PlanDecisionResult:
        """See :meth:`core.session.Session.approve_plan`."""
        return await self._session.approve_plan(run_id=run_id)

    async def dismiss_plan(self, run_id: str) -> PlanDecisionResult:
        """See :meth:`core.session.Session.dismiss_plan`."""
        return await self._session.dismiss_plan(run_id=run_id)

    def start_all_background_services(self) -> None:
        """Forward to :meth:`Session.start_all_background_services`."""
        self._session.start_all_background_services()

    def start_boot_background_services(self) -> None:
        """Forward to :meth:`Session.start_boot_background_services`."""
        self._session.start_boot_background_services()

    def register_broadcast(self, callback: Any) -> None:
        """Wire a ``(channel, payload) → None`` callback into the
        session's broadcast bus."""
        sess = getattr(self, "_session", None)
        if sess is None:
            return
        sess.broadcast_bus.register(callback)

    def consume_plan_research_flag(self) -> bool:
        """Get-and-reset the ``/plan``-armed flag.

        Reads through the raw attribute (rather than calling
        :meth:`Session.consume_plan_research_flag`) because tests
        wire ``_session`` as a MagicMock and any method call on
        one returns another MagicMock — truthy by default — which
        would spuriously arm the plan-research prefix.
        """
        sess = getattr(self, "_session", None)
        if sess is None:
            return False
        # ``is True`` guards against MagicMock's auto-spawn of missing
        # attrs (each returns a MagicMock which evaluates truthy).
        armed = getattr(sess, "_plan_research_armed", False) is True
        if armed:
            sess._plan_research_armed = False
        return armed

    # ── Lifecycle ──────────────────────────────────────────────────

    async def startup(self) -> None:
        """See :meth:`LifecycleController.startup`."""
        await self.lifecycle.startup()

    async def shutdown(self) -> None:
        """See :meth:`LifecycleController.shutdown`."""
        await self.lifecycle.shutdown()

    async def _detect_interrupted_run(self) -> None:
        """See :meth:`LifecycleController.detect_interrupted_run`.

        Kept as a method-level seam because
        ``tests/test_session_restart_round_trip.py`` +
        ``tests/test_plan_rpc_wiring.py`` bind ``AsyncMock`` here.
        """
        await self.lifecycle.detect_interrupted_run()

    async def _rehydrate_event_log(self) -> RehydrateOutcome:
        """See :meth:`RehydrateController.event_log`."""
        return await self.rehydrate.event_log()

    async def _rehydrate_orphan_processes(self) -> RehydrateOutcome:
        """See :meth:`RehydrateController.orphan_processes`."""
        return await self.rehydrate.orphan_processes()

    async def _rehydrate_plan_decisions(self) -> RehydrateOutcome:
        """See :meth:`RehydrateController.plan_decisions`."""
        return await self.rehydrate.plan_decisions()

    async def _rehydrate_todos(self) -> RehydrateOutcome:
        """See :meth:`RehydrateController.todos`."""
        return await self.rehydrate.todos()

    async def _rehydrate_plan_store(self) -> RehydrateOutcome:
        """See :meth:`RehydrateController.plan_store`."""
        return await self.rehydrate.plan_store()

    # ── Run a user message (streaming) ────────────────────────────

    async def run_message(
        self, text: str, media: dict[str, Any] | None = None
    ) -> AsyncIterator[msg.Message]:
        """Streaming entry point — delegates to
        :meth:`RunController.run_message`."""
        attachments = MediaAttachments.from_optional_dict(media)
        async for proto in self._runs.run_message(text, attachments):
            yield proto

    async def _run_message_locked(
        self, text: str, media: dict[str, Any] | None
    ) -> AsyncIterator[msg.Message]:
        """Legacy test-patch seam for the run body.

        Tests in ``test_streaming_done_unblock.py`` patch this
        method on the class to inject a stub run body. Preserved
        as a forwarder into :meth:`RunController.run_locked`.
        """
        async for proto in self._runs.run_locked(text, MediaAttachments.from_optional_dict(media)):
            yield proto

    async def _stream_with_subagent_hitl(
        self, team_stream: AsyncIterator[Any]
    ) -> AsyncIterator[msg.Message]:
        """Forward to :meth:`HitlController.stream_with_subagent`.

        Kept as a real method because tests
        ``patch.object(BackendServer, '_stream_with_subagent_hitl', ...)``
        on this seam. The controller-owned method is where the
        multiplexer lives now.
        """
        async for proto in self.hitl.stream_with_subagent(team_stream):
            yield proto

    def _build_subagent_run_paused(self, entries: list[RunRequirement]) -> msg.Message:
        """Forward to :meth:`HitlController.build_subagent_run_paused`."""
        return self.hitl.build_subagent_run_paused(entries)

    async def _periodic_checkpoint(self, team: Any, interval: float = 3.0) -> None:
        """Forward to :meth:`RunController.periodic_checkpoint`.

        ``__new__``-bypass fallback: when the pipeline is missing
        (``tests/test_crash_survival.py`` builds a bare server and
        calls this method directly with a stub checkpoint hook)
        drive a self-contained :class:`SessionCheckpointer` loop
        that routes back through ``self._checkpoint_session`` so
        the test-installed spy fires on every tick.
        """
        runs = getattr(self, "_runs", None)
        if runs is not None:
            await runs.periodic_checkpoint(team, interval)
            return
        from ember_code.backend.session_checkpointer import (  # noqa: PLC0415 — bypass-path only
            SessionCheckpointer,
        )

        checkpointer = SessionCheckpointer(team)
        await checkpointer.run_forever(
            interval=interval,
            checkpoint_hook=self._checkpoint_session,
        )

    async def _checkpoint_session(self, team: Any) -> None:
        """Forward to :meth:`RunController.checkpoint`.

        Kept as a real method so tests binding
        ``server._checkpoint_session = spy`` intercept the
        per-tick callback the pipeline routes back through
        (see :meth:`RunController._checkpoint_via_backend`).
        """
        runs = getattr(self, "_runs", None)
        if runs is not None:
            await runs.checkpoint(team)
            return
        from ember_code.backend.session_checkpointer import (  # noqa: PLC0415 — bypass-path only
            SessionCheckpointer,
        )

        await SessionCheckpointer(team).snapshot()

    def _drop_pending_for_run(self, run_id: str) -> None:
        """Forward to :meth:`HitlController.sweep_run`."""
        self.hitl.sweep_run(run_id)

    def _handle_pause(self, event: Any) -> PauseHandleResult:
        """Forward to :meth:`HitlController.handle_pause`."""
        return self.hitl.handle_pause(event)

    # ── HITL RPCs ─────────────────────────────────────────────────

    async def resolve_hitl_batch(
        self, decisions: list[msg.HITLDecision]
    ) -> AsyncIterator[msg.Message]:
        """See :meth:`HitlController.resolve_batch`."""
        async for proto in self.hitl.resolve_batch(decisions):
            yield proto

    async def resolve_hitl(
        self, requirement_id: str, action: str, choice: str = "once"
    ) -> AsyncIterator[msg.Message]:
        """See :meth:`HitlController.resolve_single`."""
        async for proto in self.hitl.resolve_single(requirement_id, action, choice):
            yield proto

    def check_permission(
        self, tool_name: str, func_name: str, tool_args: ToolCallArgs
    ) -> PermissionLevel:
        """See :meth:`HitlController.check_permission`."""
        return self.hitl.check_permission(tool_name, func_name, tool_args)

    def save_permission_rule(self, rule: str, level: PermissionLevel) -> None:
        """See :meth:`HitlController.save_permission_rule`."""
        self.hitl.save_permission_rule(rule, level)

    def _maybe_persist_choice(self, decision: msg.HITLDecision, req: RunRequirement) -> None:
        """Forward to :meth:`HitlController.maybe_persist_choice`.

        Discards the :class:`PersistChoiceResult` return so the
        method continues to satisfy the legacy no-return contract
        callers rely on. New code should call
        ``self.hitl.maybe_persist_choice`` directly for the typed
        result.
        """
        self.hitl.maybe_persist_choice(decision, req)

    # ── Command handling ──────────────────────────────────────────

    async def handle_command(self, text: str) -> msg.CommandResult:
        """Process a slash command via :class:`CommandHandler`.

        Returns a typed :class:`ember_code.backend.command_result.CommandResult`
        (a subclass of the wire :class:`msg.CommandResult`) directly.

        Late-imported so ``patch('...command_handler.CommandHandler')``
        in tests intercepts — the mock is checked at call time.
        """
        from ember_code.backend.command_handler import (
            CommandHandler,  # noqa: PLC0415 — mock-patch target
        )

        handler = CommandHandler(self._session)
        return await handler.handle(text)

    # ── Session management ────────────────────────────────────────

    async def list_sessions(self) -> msg.SessionListResult:
        """See :meth:`SessionsController.list_sessions`."""
        return await self.sessions.list_sessions()

    async def maybe_auto_name_session(self) -> AutoNameResult:
        """See :meth:`SessionsController.maybe_auto_name_session`."""
        return await self.sessions.maybe_auto_name_session()

    async def switch_session(self, session_id: str) -> msg.Info:
        """See :meth:`SessionsController.switch_session`."""
        return await self.sessions.switch_session(session_id)

    async def search_chat(
        self, session_id: str, query: str, limit: int = 50
    ) -> list[ChatSearchHit]:
        """See :meth:`SessionsController.search_chat`."""
        return await self.sessions.search_chat(session_id, query, limit)

    # ── MCP ───────────────────────────────────────────────────────

    async def ensure_mcp(self) -> McpInitResult:
        """See :meth:`McpController.ensure`."""
        return await self.mcp.ensure()

    async def toggle_mcp(self, server_name: str, connect: bool) -> msg.Info:
        """See :meth:`McpController.toggle`."""
        return await self.mcp.toggle(server_name, connect)

    def get_mcp_status(self) -> list[McpServerStatus]:
        """See :meth:`McpController.status`."""
        return self.mcp.status()

    def set_mcp_tool_enabled(self, server: str, tool: str, enabled: bool) -> MCPToolToggleResult:
        """See :meth:`McpController.set_tool_enabled`."""
        return self.mcp.set_tool_enabled(server, tool, enabled)

    async def get_mcp_server_details(self) -> list[MCPServerSnapshot]:
        """See :meth:`McpController.server_details`."""
        return await self.mcp.server_details()

    def get_mcp_servers(self) -> list[MCPServerSummary]:
        """See :meth:`McpController.servers`."""
        return self.mcp.servers()

    async def mcp_connect(self, server_name: str) -> msg.Info:
        """See :meth:`McpController.connect`."""
        return await self.mcp.connect(server_name)

    async def mcp_disconnect(self, server_name: str) -> msg.Info:
        """See :meth:`McpController.disconnect`."""
        return await self.mcp.disconnect(server_name)

    # ── Model switching ───────────────────────────────────────────

    def switch_model(self, model_name: str) -> msg.Info:
        """See :meth:`ModelSwitcher.switch`."""
        return self.model_switcher.switch(model_name)

    # ── Login / Logout ────────────────────────────────────────────

    async def login(self, on_status=None) -> LoginResult:
        """See :meth:`AuthController.login`."""
        return await self.auth.login(on_status)

    def reload_cloud_credentials(self) -> msg.StatusUpdate:
        """See :meth:`AuthController.reload_cloud_credentials`."""
        return self.auth.reload_cloud_credentials()

    def clear_cloud_credentials(self) -> msg.StatusUpdate:
        """See :meth:`AuthController.clear_cloud_credentials`."""
        return self.auth.clear_cloud_credentials()

    async def get_cloud_plan(self) -> CloudPlan | None:
        """See :meth:`AuthController.get_cloud_plan`."""
        return await self.auth.get_cloud_plan()

    # ── Context / status / compaction ─────────────────────────────

    def get_status(self) -> msg.StatusUpdate:
        """See :meth:`ContextController.get_status`."""
        return self.context.get_status()

    async def count_context_tokens(self) -> int:
        """See :meth:`ContextController.count_context_tokens`."""
        return await self.context.count_context_tokens()

    async def compact_if_needed(self, ctx_tokens: int, max_ctx: int) -> msg.SessionCleared | None:
        """See :meth:`ContextController.compact_if_needed`."""
        return await self.context.compact_if_needed(ctx_tokens, max_ctx)

    async def extract_learnings(self, user_msg: str, assistant_msg: str) -> None:
        """See :meth:`ContextController.extract_learnings`."""
        await self.context.extract_learnings(user_msg, assistant_msg)

    async def get_pending_messages(self, session_id: str) -> list[PendingMessage]:
        """See :meth:`ContextController.get_pending_messages`."""
        return await self.context.get_pending_messages(session_id)

    async def truncate_history(self, session_id: str, run_id: str) -> TruncateHistoryResult:
        """See :meth:`ContextController.truncate_history`."""
        return await self.context.truncate_history(session_id, run_id)

    # ── /loop continuation + scheduler ────────────────────────────

    async def pop_pending_loop_iteration(self) -> LoopAdvance | None:
        """Direct session call — see
        :meth:`LoopController.pop_pending_iteration`."""
        return await self._session.advance_loop()

    async def cancel_pending_loop(self) -> bool:
        """Direct session call — see
        :meth:`LoopController.cancel_pending`."""
        if self._session.loop_paused:
            return False
        return await self._session.cancel_loop()

    async def loop_pause(self) -> bool:
        """See :meth:`LoopController.pause`."""
        return await self._session.pause_loop()

    async def loop_resume(self) -> str:
        """See :meth:`LoopController.resume`."""
        prompt = await self._session.resume_loop()
        return prompt or ""

    async def loop_status(self) -> LoopStatusSnapshot:
        """See :meth:`LoopController.status`."""
        return await self.loop.status()

    async def execute_scheduled_task(self, description: str) -> str:
        """See :meth:`SchedulerController.execute`."""
        return await self.loop.scheduler.execute(description)

    async def cancel_scheduled_task(self, task_id: str) -> msg.Info:
        """See :meth:`SchedulerController.cancel`."""
        return await self.loop.scheduler.cancel(task_id)

    async def get_scheduled_tasks(self, include_done: bool = True) -> list:
        """See :meth:`SchedulerController.list_all`."""
        return await self.loop.scheduler.list_all(include_done)

    def start_scheduler(self, on_task_started=None, on_task_completed=None) -> Any:
        """See :meth:`SchedulerController.start`."""
        return self.loop.scheduler.start(on_task_started, on_task_completed)

    # ── Files / search / knowledge ────────────────────────────────

    def upload_attachment(self, filename: str, content_base64: str) -> UploadAttachmentResult:
        """See :meth:`FilesController.upload_attachment`."""
        return self.files.upload_attachment(filename, content_base64)

    def read_file(self, path: str) -> ReadFileResult:
        """See :meth:`FilesController.read_file`."""
        return self.files.read_file(path)

    def search_code(self, snippet: str, max_results: int = 20) -> SearchCodeResult:
        """See :meth:`SearchController.search_code`."""
        return self.search.search_code(snippet, max_results)

    async def get_knowledge_status(self) -> KnowledgeStatus:
        """See :meth:`KnowledgeController.status`."""
        return await self.knowledge.status()

    async def knowledge_search(self, query: str) -> list[KnowledgeHit]:
        """See :meth:`KnowledgeController.search`."""
        return await self.knowledge.search(query)

    async def knowledge_add(self, source: str) -> msg.Info:
        """See :meth:`KnowledgeController.add`."""
        return await self.knowledge.add(source)

    async def knowledge_list(self) -> list[KnowledgeListEntry]:
        """See :meth:`KnowledgeController.list`."""
        return await self.knowledge.list()

    async def knowledge_get(self, entry_id: str) -> KnowledgeGetResult:
        """See :meth:`KnowledgeController.get`."""
        return await self.knowledge.get(entry_id)

    async def knowledge_remove(self, entry_id: str) -> KnowledgeRemoveResult:
        """See :meth:`KnowledgeController.remove`."""
        return await self.knowledge.remove(entry_id)

    async def auto_sync_knowledge(self) -> str | None:
        """See :meth:`KnowledgeController.auto_sync`."""
        return await self.knowledge.auto_sync()

    # ── Chat history ──────────────────────────────────────────────

    async def get_chat_history(self, session_id: str) -> list[dict]:
        """Rebuild the FE's turn list.

        Dumps the discriminated-union ``ChatTurn`` list to
        ``list[dict]`` at this wire boundary so the RPC contract
        stays byte-identical (a strict ``ChatHistoryEntry`` cast
        here would collapse per-role fields to their defaults).
        The controller returns typed :class:`ChatTurn` objects.
        """
        controllers = getattr(self, "controllers", None)
        if controllers is not None:
            turns = await controllers.chat_history.rebuild(session_id)
        else:
            # ``__new__``-bypass fallback for test fixtures that
            # never ran BackendBootstrap.
            turns = await ChatHistoryRebuilder(session=self._session).rebuild(session_id)
        return [t.model_dump(mode="json") for t in turns]

    # ── Team-wiring ───────────────────────────────────────────────

    def wire_queue_hook(self, queue: list) -> None:
        """See :meth:`TeamWiring.wire_queue_hook`."""
        self.team_wiring.wire_queue_hook(queue)

    def wire_orchestrate_progress(self, callback) -> None:
        """See :meth:`TeamWiring.wire_orchestrate_progress`."""
        self.team_wiring.wire_orchestrate_progress(callback)

    # ── Run cancellation ──────────────────────────────────────────

    @staticmethod
    async def _close_model_http_client(team: Any) -> None:
        """Legacy staticmethod seam.

        Forwards to :meth:`RunController.close_model_http_client` —
        preserved as a static on :class:`BackendServer` because 5
        tests in ``test_backend_server.py`` call this without an
        instance.
        """
        await RunController.close_model_http_client(team)

    def cancel_agent_run(self, run_id: str) -> CancelAgentRunResult:
        """See :meth:`RunController.cancel_agent_run`."""
        runs = getattr(self, "_runs", None)
        if runs is not None:
            return runs.cancel_agent_run(run_id)
        if not run_id:
            return CancelAgentRunResult(ok=False, error="missing run_id")
        try:
            Agent.cancel_run(run_id)
            return CancelAgentRunResult(ok=True)
        except Exception as exc:  # noqa: BLE001 — surfaced in envelope
            return CancelAgentRunResult(ok=False, error=str(exc))

    def cancel_run(self) -> None:
        """See :meth:`RunController.cancel_run`.

        ``__new__``-bypass fallback: when the pipeline was never
        wired, drop to a minimal cancel that only touches session
        state (mirrors the old inline behaviour so
        ``test_cancel_run_no_crash_when_no_team`` keeps passing
        without wiring a controller).
        """
        runs = getattr(self, "_runs", None)
        if runs is not None:
            runs.cancel_run()
            return
        from ember_code.core.tools.process_supervisor_locator import (  # noqa: PLC0415 — bypass-path only
            supervisors,
        )

        if supervisors.default().cancel_foreground():
            logger.info("Killed foreground process on cancel")
        try:
            team = self._session.main_team
            run_id = getattr(team, "run_id", None)
            if run_id:
                Agent.cancel_run(run_id)
        except Exception as exc:
            logger.debug("Failed to cancel run: %s", exc)
        task = self.__dict__.get("_current_run_task")
        if task and not task.done():
            task.cancel()

    # ── Plan + todos + visualization ──────────────────────────────

    def get_latest_plan(self) -> LatestPlanResult:
        """See :meth:`PlanSnapshotBuilder.latest`."""
        return self.plan_snapshots.latest()

    def get_todos(self) -> list[dict]:
        """See :meth:`PlanSnapshotBuilder.todos`."""
        return self.plan_snapshots.todos()

    def dispatch_visualization_action(
        self, action: str, params: dict | None = None
    ) -> VisualizationActionResult:
        """See :meth:`VisualizationActionBus.dispatch`."""
        return self.viz_actions.dispatch(action, params)

    # ── Background process watcher ────────────────────────────────

    def list_background_processes(self) -> list[dict]:
        """See :meth:`ProcessesController.list`."""
        return [row.model_dump() for row in self.processes.list()]

    def read_process_tail(self, pid: int, tail: int = 200) -> dict:
        """See :meth:`ProcessesController.read_tail`."""
        return self.processes.read_tail(pid, tail).model_dump()

    async def stop_background_process(self, pid: int) -> dict:
        """See :meth:`ProcessesController.stop`."""
        result = await self.processes.stop(pid)
        return result.model_dump()

    # ── Read-only accessors ───────────────────────────────────────

    @property
    def processing(self) -> bool:
        """Wire-compatible with the previous ``_processing`` bool —
        forwards to :meth:`RunController.is_processing`."""
        runs = getattr(self, "_runs", None)
        return runs.is_processing() if runs is not None else False

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
        """Skill names for input autocomplete."""
        return self.panels.skill_names()

    def get_skill_pool(self) -> SkillPool:
        """Return the skill pool for input autocomplete."""
        return self.panels.skill_pool()

    # ── Panels ────────────────────────────────────────────────────

    def get_agent_details(self) -> list[AgentInfo]:
        """See :meth:`PanelsController.agent_details`."""
        return self.panels.agent_details()

    def promote_ephemeral_agent(self, name: str) -> PromoteEphemeralResult:
        """See :meth:`PanelsController.promote_ephemeral_agent`."""
        return self.panels.promote_ephemeral_agent(name)

    def discard_ephemeral_agent(self, name: str) -> DiscardEphemeralResult:
        """See :meth:`PanelsController.discard_ephemeral_agent`."""
        return self.panels.discard_ephemeral_agent(name)

    def get_hooks_details(self) -> list[HookEntryView]:
        """See :meth:`PanelsController.hooks_details`."""
        return self.panels.hooks_details()

    def reload_hooks_rpc(self) -> msg.Info:
        """See :meth:`PanelsController.reload_hooks`."""
        return self.panels.reload_hooks()

    def get_skill_details(self) -> list[SkillInfo]:
        """See :meth:`PanelsController.skill_details`."""
        return self.panels.skill_details()

    def get_output_styles(self) -> OutputStylesResult:
        """See :meth:`PanelsController.output_styles`."""
        return self.panels.output_styles()

    def get_slash_commands(self) -> list[SlashCommandEntry]:
        """See :meth:`PanelsController.slash_commands`."""
        return self.panels.slash_commands()

    # ── CodeIndex ─────────────────────────────────────────────────

    async def codeindex_status(self) -> CodeIndexStatus:
        """See :meth:`CodeIndexController.status`."""
        return await self.codeindex.status()

    async def codeindex_sync(self, sha: str | None) -> CodeIndexSyncResult:
        """See :meth:`CodeIndexController.sync`."""
        return await self.codeindex.sync(sha)

    async def codeindex_resync(self, sha: str | None) -> CodeIndexSyncResult:
        """See :meth:`CodeIndexController.resync`."""
        return await self.codeindex.resync(sha)

    async def codeindex_clean(self) -> CodeIndexCleanResult:
        """See :meth:`CodeIndexController.clean`."""
        return await self.codeindex.clean()

    async def codeindex_head_breakdown(self) -> CodeIndexHeadBreakdown:
        """See :meth:`CodeIndexController.head_breakdown`."""
        return await self.codeindex.head_breakdown()

    def codeindex_activity(self) -> list[CodeIndexActivityEntry]:
        """See :meth:`CodeIndexController.activity`."""
        return self.codeindex.activity()

    def codeindex_install(self) -> CodeIndexInstallResult:
        """See :meth:`CodeIndexController.install`."""
        return self.codeindex.install()

    # ── Plugins ───────────────────────────────────────────────────

    def get_plugin_contents(self, name: str) -> PluginContents:
        """Detailed inventory of one installed plugin."""
        loader = self._session.plugin_loader
        plugin = next(
            (p for p in loader.list_plugins() if p.name == name),
            None,
        )
        if plugin is None:
            return PluginContents(error=f"Plugin '{name}' not found")
        return PluginContents.from_directory(plugin.root_path, name=name)

    async def preview_plugin(
        self,
        source: str,
        branch: str | None = None,
        subdir: str | None = None,
    ) -> PluginContents:
        """See :meth:`PluginController.preview`."""
        return await self.plugins.preview(source, branch, subdir)

    def get_plugin_details(self) -> list[PluginInfo]:
        """See :meth:`PluginController.list_installed`."""
        return self.plugins.list_installed()

    def set_plugin_enabled(self, name: str, enabled: bool) -> msg.Info:
        """See :meth:`PluginController.set_enabled`."""
        return self.plugins.set_enabled(name, enabled)

    def install_plugin(self, ref: str, install_ref: str | None = None) -> msg.Info:
        """See :meth:`PluginController.install`."""
        return self.plugins.install(ref, install_ref)

    def update_plugin(self, name: str, install_ref: str | None = None) -> msg.Info:
        """See :meth:`PluginController.update`."""
        return self.plugins.update(name, install_ref)

    def remove_plugin(self, name: str) -> msg.Info:
        """See :meth:`PluginController.remove`."""
        return self.plugins.remove(name)

    def get_marketplaces(self) -> list[MarketplaceInfo]:
        """See :meth:`MarketplaceController.list_registered`."""
        return self.marketplaces.list_registered()

    def add_marketplace(self, url: str) -> msg.Info:
        """See :meth:`MarketplaceController.add`."""
        return self.marketplaces.add(url)

    def remove_marketplace(self, name: str) -> msg.Info:
        """See :meth:`MarketplaceController.remove`."""
        return self.marketplaces.remove(name)

    def refresh_marketplaces(self, name: str | None = None) -> msg.Info:
        """See :meth:`MarketplaceController.refresh`."""
        return self.marketplaces.refresh(name)

    # ── Hooks fire ────────────────────────────────────────────────

    async def fire_session_start_hook(self) -> None:
        """Forward to :meth:`Session.fire_session_start_hook`."""
        await self._session.fire_session_start_hook()

    # ── Display toggle ────────────────────────────────────────────

    def toggle_verbose(self) -> bool:
        """Forward to :meth:`DisplayConfig.toggle_show_routing`.

        Read-mutate-return that lives on the settings type, not
        here. Kept as a one-line delegate so ``rpc_router.py``'s
        ``server.toggle_verbose()`` call site stays unchanged.
        """
        return self._settings.display.toggle_show_routing()
