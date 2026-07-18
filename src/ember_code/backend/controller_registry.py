"""Central registry for every ``BackendServer`` sub-controller.

Extracted from :mod:`ember_code.backend.server` — the previous
composition-root file carried 17 near-identical lazy-init
``@property`` blocks (one per controller, ~15 LoC each) with a
uniform ``getattr(self, '_ctrl', None) or construct`` shape.
That's a class-shaped concern hiding as a copy-paste in
``BackendServer``.

The refactor:

* :class:`Controllers` — a plain attribute-holder class with one
  slot per controller (mcp / hitl / context / auth / …). Built
  eagerly by :meth:`ControllerRegistry.build`.
* :class:`ControllerRegistry` — the builder. Constructs the full
  :class:`Controllers` bag for a production ``BackendServer`` and
  exposes :meth:`for_partial_init` — the ONE tolerance path for
  ``__new__``-bypass test fixtures. Any controller whose
  constructor tolerates missing state (``ContextController``,
  ``LoopController``, …) is still built; the tests get identical
  behaviour without every property re-implementing the
  ``getattr`` guard.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ember_code.backend.hitl_controller import HitlController
from ember_code.backend.hitl_tracer import HITLTracer
from ember_code.backend.marketplace_controller import MarketplaceController
from ember_code.backend.model_switcher import ModelSwitcher
from ember_code.backend.pending_requirements_store import PendingRequirementsStore
from ember_code.backend.plan_snapshot_builder import PlanSnapshotBuilder
from ember_code.backend.plugin_controller import PluginController
from ember_code.backend.run_controller import RunController
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

if TYPE_CHECKING:

    from ember_code.backend.server import BackendServer
    from ember_code.core.config.settings import Settings
    from ember_code.core.session import Session
    from ember_code.core.session.pending_messages import PendingMessageStore
    from ember_code.core.session.session_preferences import SessionPreferencesStore


class Controllers:
    """Typed attribute bag — one slot per sub-controller.

    Kept as a plain class (not a Pydantic model) because most of
    the values are opaque controller objects with their own lazy
    subsystems that would fight Pydantic's field validation.

    Consumers (``BackendServer`` facades) access members by name:
    ``self.controllers.mcp``, ``self.controllers.hitl``, etc.
    """

    def __init__(
        self,
        *,
        mcp: McpController,
        hitl: HitlController,
        context: ContextController,
        auth: AuthController,
        knowledge: KnowledgeController,
        files: FilesController,
        search: SearchController,
        panels: PanelsController,
        codeindex: CodeIndexController,
        loop: LoopController,
        processes: ProcessesController,
        sessions: SessionsController,
        rehydrate: RehydrateController,
        lifecycle: LifecycleController,
        plan_snapshots: PlanSnapshotBuilder,
        viz_actions: VisualizationActionBus,
        team_wiring: TeamWiring,
        model_switcher: ModelSwitcher,
        plugins: PluginController,
        marketplaces: MarketplaceController,
        runs: RunController,
        chat_history: ChatHistoryRebuilder,
    ) -> None:
        self.mcp = mcp
        self.hitl = hitl
        self.context = context
        self.auth = auth
        self.knowledge = knowledge
        self.files = files
        self.search = search
        self.panels = panels
        self.codeindex = codeindex
        self.loop = loop
        self.processes = processes
        self.sessions = sessions
        self.rehydrate = rehydrate
        self.lifecycle = lifecycle
        self.plan_snapshots = plan_snapshots
        self.viz_actions = viz_actions
        self.team_wiring = team_wiring
        self.model_switcher = model_switcher
        self.plugins = plugins
        self.marketplaces = marketplaces
        self.runs = runs
        self.chat_history = chat_history


class ControllerRegistry:
    """Builds the :class:`Controllers` bag for a ``BackendServer``.

    Production path uses :meth:`build`; the test-bypass path uses
    :meth:`for_partial_init` — a single classmethod replaces the
    17 duplicated ``getattr`` guards that used to live on
    ``BackendServer``'s @property blocks.
    """

    @classmethod
    def build(
        cls,
        backend: BackendServer,
        session: Session,
        settings: Settings,
        hitl_store: PendingRequirementsStore,
        pending_store: PendingMessageStore,
        session_prefs: SessionPreferencesStore,
        user_config_store: Any,
        hitl_tracer: HITLTracer,
    ) -> Controllers:
        """Full production wiring — every controller built eagerly."""
        # ── Foundation controllers (session-only deps) ──────────────
        mcp = McpController(session)
        knowledge = KnowledgeController(session)
        files = FilesController(session)
        search = SearchController(session)
        panels = PanelsController(session)
        codeindex = CodeIndexController(session)
        processes = ProcessesController()
        rehydrate = RehydrateController(session)
        plan_snapshots = PlanSnapshotBuilder(session)
        viz_actions = VisualizationActionBus(session)
        team_wiring = TeamWiring(session)
        plugins = PluginController(session)
        marketplaces = MarketplaceController(session)

        # HITL — the controller now owns the pause_handler + tracer.
        hitl = HitlController(
            session=session,
            store=hitl_store,
            tracer=hitl_tracer,
        )

        # RunController owns the run pipeline (lock, task, checkpoint).
        runs = RunController(
            backend=backend,
            session=session,
            pending_store=pending_store,
        )

        # Context needs pending_store for the crash-survival RPC.
        context = ContextController(
            session=session,
            settings=settings,
            pending_store=pending_store,
        )

        # Sessions needs a chat-history provider — call through the
        # backend's public wire method so the wire dumping continues
        # to happen at the same boundary.
        sessions = SessionsController(
            session=session,
            chat_history_provider=backend.get_chat_history,
        )

        # Auth needs a status provider — call through the backend's
        # public wire method (context.get_status behind the facade).
        auth = AuthController(
            session=session,
            settings=settings,
            status_provider=backend.get_status,
        )

        # Loop reads settings for the pump.
        loop = LoopController(
            session=session,
            settings=settings,
        )

        # Lifecycle composes multiple concerns (rehydrate + runs +
        # scheduler + backend).
        lifecycle = LifecycleController(
            session=session,
            pending_store=pending_store,
            runs=runs,
            rehydrate=rehydrate,
            scheduler_stop=loop.scheduler.stop,
            backend=backend,
        )

        # ModelSwitcher used to inline-import UserConfigStore inside
        # its property. That import is now hoisted to the bootstrap
        # so the class boundary carries the dependency explicitly.
        model_switcher = ModelSwitcher(
            session=session,
            session_prefs=session_prefs,
            user_config_store=user_config_store,
        )

        chat_history = ChatHistoryRebuilder(session=session)

        return Controllers(
            mcp=mcp,
            hitl=hitl,
            context=context,
            auth=auth,
            knowledge=knowledge,
            files=files,
            search=search,
            panels=panels,
            codeindex=codeindex,
            loop=loop,
            processes=processes,
            sessions=sessions,
            rehydrate=rehydrate,
            lifecycle=lifecycle,
            plan_snapshots=plan_snapshots,
            viz_actions=viz_actions,
            team_wiring=team_wiring,
            model_switcher=model_switcher,
            plugins=plugins,
            marketplaces=marketplaces,
            runs=runs,
            chat_history=chat_history,
        )
