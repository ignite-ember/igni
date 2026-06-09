"""Session core — wires up subsystems and handles messages."""

import asyncio
import getpass
import logging
import threading
import uuid
from pathlib import Path
from typing import Any

from agno.agent import Agent
from agno.compression.manager import CompressionManager

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
from ember_code.core.init import initialize_project
from ember_code.core.learn import create_learning_machine
from ember_code.core.loop import (
    LOOP_DEFAULT_MAX_ITERATIONS,
    LOOP_HARD_CAP,
    LoopProgressStore,
    LoopState,
    LoopStore,
    wrap_iteration_prompt,
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
from ember_code.core.utils.display import print_error, print_info
from ember_code.core.utils.response import extract_response_text
from ember_code.core.workspace import WorkspaceManager

logger = logging.getLogger(__name__)


class Session:
    """Manages a single Ember Code session with all subsystem integrations.

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

        # ── /loop state (persisted to project-local SQLite) ──────────
        # When the user invokes ``/loop <prompt>``, the same prompt is
        # automatically re-fired as the next turn after the agent
        # finishes — until ``/loop stop``, any non-``/loop`` user
        # input, or the safety cap. The three fields below mirror
        # the ``loop_state`` row; mutations go through ``start_loop``
        # / ``advance_loop`` / ``cancel_loop`` so the row stays in
        # lockstep with memory.
        #
        # ``loop_run_id`` is a uuid4 minted on every fresh start;
        # ``LoopProgressStore`` keys progress rows by this id so a
        # new loop run can't accidentally see the previous run's
        # progress entries. ``None`` when no loop is active.
        #
        # The fields are initialized to defaults here; the persisted
        # row (if any) is hydrated by ``load_persisted_loop_state``
        # after construction (async, so the BackendServer awaits it
        # in its startup hook).
        self.pending_loop_prompt: str | None = None
        self.loop_iteration_index: int = 0
        self.loop_iterations_remaining: int = 0
        self.loop_run_id: str | None = None
        # Whether the user supplied an explicit iteration cap
        # (``/loop N <prompt>``). When False, the cap is just a
        # safety net — we auto-extend on cap-hit and don't display
        # a "total" to the user. When True, the cap is also the
        # intended total — we terminate at it and show ``N / M``.
        # Persisted alongside the other loop fields.
        self.loop_cap_explicit: bool = False
        # ``loop_paused`` distinguishes a *dormant* loop (loaded from
        # ``state.db`` on startup, no iterations firing) from an
        # *actively pumping* one. Two consequences:
        #
        # 1. The FE's cancel-on-non-/loop guard skips a paused loop
        #    — otherwise typing literally anything after restart
        #    would destroy the very state the user might want to
        #    continue.
        # 2. The panel renders a different badge + key hint
        #    (``paused`` + ``R resume``) so the user knows the
        #    loop is waiting for explicit revival.
        #
        # Set True by ``load_persisted_loop_state`` whenever it
        # finds a row; flipped False by every helper that fires or
        # creates an iteration (``start_loop``, ``advance_loop``,
        # ``resume_loop``) and by ``cancel_loop``.
        self.loop_paused: bool = False
        self.loop_store = LoopStore(project_dir=self.project_dir)
        self.loop_progress_store = LoopProgressStore(project_dir=self.project_dir)

        # ── First-run initialization (agents, skills, hooks, ember.md) ─
        initialize_project(self.project_dir)

        # ── Storage (Agno AsyncBaseDb) ────────────────────────────────
        self.db = setup_db(settings, project_dir=self.project_dir)

        # ── Knowledge (Chroma-backed) ─────────────────────────────────
        # Construction of ``KnowledgeIndex`` is cheap (no model load —
        # the embedder is the shared singleton). We initialize eagerly
        # and treat ``_knowledge_ready`` as already set.
        self._knowledge_error: str | None = None
        self._knowledge_ready = threading.Event()
        self._knowledge_ready.set()
        if pre_knowledge is not None:
            self.knowledge = pre_knowledge
            logger.info("Knowledge: using pre-loaded instance")
        elif settings.knowledge.enabled:
            from ember_code.core.knowledge.manager import KnowledgeManager

            self.knowledge = KnowledgeManager(
                settings, project_dir=self.project_dir
            ).create_knowledge()
        else:
            self.knowledge = None
            logger.info("Knowledge: disabled in settings")

        # ── Permission Guard ─────────────────────────────────────────
        self.permission_guard = PermissionGuard(settings)

        # ── Audit Logger ─────────────────────────────────────────────
        self.audit = AuditLogger(settings)

        # ── Plugin discovery (before hooks — plugins contribute hooks) ─
        # Scans ~/.claude/plugins, ~/.ember/plugins, <project>/.claude/plugins,
        # <project>/.ember/plugins. Plugins disabled in plugins.json are
        # discovered (so the panel can show them) but their bundled
        # contents are skipped at apply time.
        from ember_code.core.plugins import PluginLoader, load_state

        self.plugin_state = load_state(settings.storage.data_dir)
        self.plugin_loader = PluginLoader()
        self.plugin_loader.load_all(self.project_dir)
        self._disabled_plugins = set(self.plugin_state.disabled)

        # ── Hooks ────────────────────────────────────────────────────
        self._hook_loader = HookLoader(
            self.project_dir, cross_tool_support=settings.hooks.cross_tool_support
        )
        self.hooks_map = self._hook_loader.load()
        # Plugin hooks merge in *before* the executor is constructed
        # so plugin behavior is in effect from the very first event.
        # Plugins prepend per event so project hooks still run last.
        self.plugin_loader.apply_to_hooks(
            self._hook_loader,
            self.hooks_map,
            disabled=self._disabled_plugins,
        )
        self.hook_executor = HookExecutor(self.hooks_map)

        # ── Project Context ──────────────────────────────────────────
        self.project_instructions = load_project_context(
            self.project_dir,
            settings.context.project_file,
            read_claude_md=settings.rules.cross_tool_support,
        )

        # ── CodeIndex availability check (eager — pool needs the flag) ─
        # Construct CodeIndex + sync-manager here so we can determine
        # whether a populated chroma index exists for the current HEAD
        # *before* loading agent definitions. The pool uses the flag to
        # pick CodeIndex-first prompt variants (``<name>.codeindex.md``)
        # over the plain ``<name>.md`` ones; the main-agent prompt
        # loader uses the same flag to pick ``main_agent.codeindex.md``.
        # Doing this once here avoids re-deriving the same fact in
        # multiple places later in __init__.
        self.code_index = CodeIndex(project=self.project_dir, data_dir=settings.storage.data_dir)
        self.code_index_sync = CodeIndexSyncManager.from_settings(
            settings, project_dir=self.project_dir, code_index=self.code_index
        )
        _head_sha = self.code_index_sync.current_sha()
        self._codeindex_available = bool(_head_sha and self.code_index.has_commit(_head_sha))

        # ── Agent Pool (definitions only — agents built after MCP connects) ─
        # Share the session's SQLite ``db`` so paused sub-agent runs land
        # in the same store as the main team's runs. Agno's
        # ``acontinue_run`` looks up the run by ``(run_id, session_id)``
        # in the agent's db; without one HITL resume fails with
        # "No runs found for run ID …".
        self.pool = AgentPool(db=self.db)
        self.pool.load_definitions(
            settings, self.project_dir, codeindex_available=self._codeindex_available
        )
        self.plugin_loader.apply_to_agents(self.pool, disabled=self._disabled_plugins)
        if settings.orchestration.generate_ephemeral:
            self.pool.init_ephemeral(
                self.project_dir, settings.orchestration.max_ephemeral_per_session
            )
        self.pool.build_agents()  # initial build without MCP

        # ── Skill Pool ───────────────────────────────────────────────
        self.skill_pool = SkillPool()
        self.skill_pool.load_all(self.project_dir, settings.skills.cross_tool_support)
        self.plugin_loader.apply_to_skills(self.skill_pool, disabled=self._disabled_plugins)

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

        # ── MCP Client Manager (user-configured servers only) ────────
        self.mcp_manager = MCPClientManager(self.project_dir)
        # Plugin-bundled servers merge into ``mcp_manager.configs`` with
        # names prefixed ``<plugin>:<server>``. They're available for
        # ``connect()`` like any other server; the panel surfaces them
        # under the plugin's name.
        from ember_code.core.mcp.config import MCPConfigLoader as _MCPConfigLoader

        self.plugin_loader.apply_to_mcp(
            _MCPConfigLoader(self.project_dir),
            self.mcp_manager.configs,
            disabled=self._disabled_plugins,
        )
        self._mcp_initialized = False

        # ── Guardrails ───────────────────────────────────────────────
        self.guardrail_runner = GuardrailRunner(settings)

        # ── Sub-agent HITL bridge ────────────────────────────────────
        # Sub-agents spawned by the orchestrator emit RunPausedEvents
        # inside the parent's tool execution; without this coordinator
        # the pauses are lost and tool calls return empty. See
        # core/sub_agent_hitl.py.
        from ember_code.core.sub_agent_hitl import SubAgentHITLCoordinator

        self.sub_agent_hitl = SubAgentHITLCoordinator()

        # ── Delegated managers ───────────────────────────────────────
        self.persistence = SessionPersistence(self.db, self.session_id)
        self.memory_mgr = SessionMemoryManager(self.db, settings, self.user_id)
        self.knowledge_mgr = SessionKnowledgeManager(self.knowledge, settings, self.project_dir)
        # Share knowledge_mgr with the pool so all sub-agents get the toolkit.
        self.pool._knowledge_mgr = self.knowledge_mgr if self.knowledge else None

        # ── Main Agent (single agent with all tools + orchestration) ──
        self.main_team = self._build_main_agent()

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
        from ember_code.core.auth.credentials import CloudCredentials
        from ember_code.core.config.cloud_models import fetch_cloud_models, merge_into_registry

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

    async def load_persisted_loop_state(self) -> None:
        """Hydrate the in-memory loop fields from the ``loop_state`` row.

        Called by :py:meth:`BackendServer.startup` after the session
        is constructed. If the CLI was killed mid-loop, this is what
        restores the prompt + counters so the panel shows the
        interrupted state. The loop is left in the *paused* state —
        no iteration fires until the user explicitly resumes (via
        ``/loop resume``, the panel's ``R`` key, or the agent tool).
        Idempotent — safe to call multiple times.
        """
        state = await self.loop_store.load()
        if state is None:
            return
        self.loop_run_id = state.run_id
        self.pending_loop_prompt = state.prompt
        self.loop_iteration_index = state.iteration_index
        self.loop_iterations_remaining = state.iterations_remaining
        self.loop_cap_explicit = state.cap_explicit
        self.loop_paused = True

    async def start_loop(
        self,
        prompt: str,
        max_iter: int,
        *,
        immediate: bool,
        cap_explicit: bool,
    ) -> str:
        """Mint a new ``/loop`` and persist it.

        ``immediate=True`` is the slash-command path: iteration 1
        fires *now* via the ``run_prompt`` action, so the counters
        start at ``index=1, remaining=max-1``.

        ``immediate=False`` is the agent-tool path
        (:class:`LoopTools.loop_start`): iteration 1 fires on the
        *next* idle cycle via :py:meth:`advance_loop`, so the
        counters start at ``index=0, remaining=max`` and the first
        advance bumps them to ``index=1, remaining=max-1``.

        ``cap_explicit`` distinguishes the two semantic meanings of
        ``max_iter``:

        * ``True``  — the user (or agent) explicitly asked for N
          iterations. N is both the safety bound AND the intended
          total. Termination at the cap is normal.
        * ``False`` — ``max_iter`` is just a safety-net batch size
          (``LOOP_DEFAULT_MAX_ITERATIONS``). On cap-hit
          :py:meth:`advance_loop` auto-extends instead of
          terminating; we stop only at ``LOOP_HARD_CAP``.

        Returns the freshly-minted ``run_id`` so the caller can
        scope :py:class:`LoopProgressStore` writes to it. Caller
        is responsible for clearing any pre-existing loop (via
        :py:meth:`cancel_loop`) before calling this — overwriting
        a live loop would orphan its progress rows.
        """
        self.loop_run_id = str(uuid.uuid4())
        self.pending_loop_prompt = prompt
        self.loop_cap_explicit = cap_explicit
        # A freshly started loop is always pumping — clear any
        # leftover paused flag from a previous restart.
        self.loop_paused = False
        if immediate:
            self.loop_iteration_index = 1
            self.loop_iterations_remaining = max_iter - 1
        else:
            self.loop_iteration_index = 0
            self.loop_iterations_remaining = max_iter
        await self._persist_loop_state()
        return self.loop_run_id

    async def advance_loop(self) -> dict | None:
        """Pop the next iteration descriptor and persist the new
        counters.

        Returns ``None`` when no loop is active. Returns
        ``{"completed": True, "total_iterations": N}`` when the cap
        was just hit (state is cleared as part of this call so the
        next call returns ``None``). Otherwise returns
        ``{"prompt", "iteration", "remaining"}`` — the descriptor
        the FE feeds into :py:meth:`_run` for the next iteration.
        """
        if self.pending_loop_prompt is None:
            return None
        # Paused loops don't auto-advance — the FE polls
        # ``_check_loop_continuation`` after every idle, and without
        # this guard a paused loop would immediately advance and
        # fire iteration N+1, defeating both the cap-reached pause
        # and the on-error pause.
        if self.loop_paused:
            return None
        if self.loop_iterations_remaining <= 0:
            # Explicit caps still terminate at the user's N — the
            # user said "exactly N", honour it.
            if self.loop_cap_explicit:
                total = self.loop_iteration_index
                await self.cancel_loop()
                return {"completed": True, "total_iterations": total}
            # Implicit caps: extend the safety net by another batch,
            # OR pause at the hard ceiling. Pausing (vs.
            # terminating) at ``LOOP_HARD_CAP`` lets the user
            # ``/loop resume`` to keep going past 200 — the cap is
            # there to catch a runaway, not to silently cut off
            # legitimate long-running work.
            if self.loop_iteration_index >= LOOP_HARD_CAP:
                await self.pause_loop()
                return {
                    "safety_cap_paused": True,
                    "iteration": self.loop_iteration_index,
                }
            self.loop_iterations_remaining = min(
                LOOP_DEFAULT_MAX_ITERATIONS,
                LOOP_HARD_CAP - self.loop_iteration_index,
            )
            self._auto_extended_this_advance = True
        # An advance means an iteration is firing — the loop is by
        # definition pumping now, even if it was paused a moment ago.
        self.loop_paused = False
        self.loop_iterations_remaining -= 1
        self.loop_iteration_index += 1
        await self._persist_loop_state()
        # Wrap the prompt for the agent — see
        # ``core/loop/prompt.py`` for the rationale. The
        # ``pending_loop_prompt`` field stays unwrapped (it's what
        # the panel and the chat display); only the FE-bound
        # ``prompt`` field gets the meta-instruction tag. The
        # ``display_prompt`` field carries the unwrapped string so
        # ``run_controller`` can show it in chat while feeding the
        # wrapped form to the agent.
        # ``total`` is only meaningful when the user explicitly
        # capped the run; otherwise we send no total to the agent so
        # it doesn't try to pace itself against a fake number.
        cap = (
            self.loop_iteration_index + self.loop_iterations_remaining
            if self.loop_cap_explicit
            else None
        )
        wrapped = wrap_iteration_prompt(self.pending_loop_prompt, self.loop_iteration_index, cap)
        result = {
            "prompt": wrapped,
            "display_prompt": self.pending_loop_prompt,
            "iteration": self.loop_iteration_index,
            "remaining": self.loop_iterations_remaining,
            # The FE uses this to decide whether the "N remaining
            # after this one" half of the iteration banner is
            # meaningful (it is only for explicit caps).
            "cap_explicit": self.loop_cap_explicit,
        }
        # Surface a one-shot signal to the FE when this advance just
        # auto-extended past the safety net — the FE renders an
        # info line so the user knows the system kept going past
        # the original batch.
        if getattr(self, "_auto_extended_this_advance", False):
            result["auto_extended"] = True
            self._auto_extended_this_advance = False
        return result

    async def cancel_loop(self) -> bool:
        """Clear ``/loop`` state both in memory and on disk.

        Returns whether anything was active before the call —
        callers use this to decide whether to surface a "loop
        cancelled" message vs. silently no-op'ing on a stray
        cancel. Progress rows for the cancelled ``run_id`` are
        *kept* — the user can clear them via the agent tool if
        they want a clean slate.
        """
        if self.pending_loop_prompt is None:
            return False
        self.pending_loop_prompt = None
        self.loop_iteration_index = 0
        self.loop_iterations_remaining = 0
        self.loop_run_id = None
        self.loop_paused = False
        self.loop_cap_explicit = False
        await self.loop_store.clear()
        return True

    async def pause_loop(self) -> bool:
        """Flip the active loop to paused without advancing the counter.

        Two callers:

        * :py:meth:`advance_loop` when an *implicit* loop hits the
          ``LOOP_HARD_CAP`` ceiling — instead of terminating, we
          pause and let the user decide via ``/loop resume`` /
          ``/loop stop``.
        * The FE's ``_check_loop_continuation`` when an iteration's
          ``_run`` raises (429 from the model API, network error,
          tool failure, etc.) — pausing without advancing means a
          subsequent ``/loop resume`` re-fires the *failed*
          iteration N, giving the user a clean retry surface.

        Returns False when no loop is active (nothing to pause).
        Idempotent — re-pausing an already-paused loop is a no-op.
        """
        if self.pending_loop_prompt is None:
            return False
        self.loop_paused = True
        await self._persist_loop_state()
        return True

    async def resume_loop(self) -> str | None:
        """Unpause an interrupted ``/loop`` and return its prompt.

        Returns:
            The persisted prompt so the caller can fire ``_run(prompt)``
            and re-run the iteration that was in flight when the
            CLI died. ``None`` when there's no loop to resume, or
            when the loop is already pumping (not paused) — caller
            surfaces an appropriate message.
        """
        if self.pending_loop_prompt is None:
            return None
        if not self.loop_paused:
            return None
        self.loop_paused = False
        await self._persist_loop_state()
        # Wrap so the resumed iteration carries the same
        # autonomous-loop instructions every other iteration does.
        # ``total`` is only sent when the user explicitly capped the
        # run; otherwise the wrapper omits it (same contract as
        # ``advance_loop``).
        cap = (
            self.loop_iteration_index + self.loop_iterations_remaining
            if self.loop_cap_explicit
            else None
        )
        return wrap_iteration_prompt(self.pending_loop_prompt, self.loop_iteration_index, cap)

    async def _persist_loop_state(self) -> None:
        """Write the current loop fields to the ``loop_state`` row.

        Helper called by :py:meth:`start_loop` and
        :py:meth:`advance_loop`. ``cancel_loop`` uses
        ``loop_store.clear`` directly instead — saving an "empty"
        state would leave a stale row around.
        """
        if self.pending_loop_prompt is None or self.loop_run_id is None:
            return
        await self.loop_store.save(
            LoopState(
                run_id=self.loop_run_id,
                prompt=self.pending_loop_prompt,
                iteration_index=self.loop_iteration_index,
                iterations_remaining=self.loop_iterations_remaining,
                cap_explicit=self.loop_cap_explicit,
            )
        )

    def reload_hooks(self) -> int:
        """Reload hooks from settings files. Returns the number of hooks loaded."""
        self.hooks_map = self._hook_loader.load()
        self.hook_executor = HookExecutor(self.hooks_map)
        # Recreate tool event hook on the team
        tool_event_hook = self._create_tool_event_hook()
        if self.main_team:
            # Replace any existing ToolEventHook in the team's tool_hooks
            existing = self.main_team.tool_hooks or []
            self.main_team.tool_hooks = [h for h in existing if not isinstance(h, ToolEventHook)]
            self.main_team.tool_hooks.append(tool_event_hook)
        count = sum(len(hl) for hl in self.hooks_map.values())
        return count

    def reload_plugins(self) -> dict[str, int]:
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
        from ember_code.core.mcp.config import MCPConfigLoader
        from ember_code.core.plugins import PluginLoader, load_state

        # Re-read disabled set from ``plugins.json`` — the user may
        # have toggled enable/disable while we were running.
        self.plugin_state = load_state(self.settings.storage.data_dir)
        self._disabled_plugins = set(self.plugin_state.disabled)

        # Rescan the four plugin roots.
        self.plugin_loader = PluginLoader()
        self.plugin_loader.load_all(self.project_dir)

        # Hooks — settings files first, plugins prepend onto them so
        # project hooks still run last (matches the constructor's
        # ordering exactly).
        self.hooks_map = self._hook_loader.load()
        self.plugin_loader.apply_to_hooks(
            self._hook_loader,
            self.hooks_map,
            disabled=self._disabled_plugins,
        )
        self.hook_executor = HookExecutor(self.hooks_map)

        # Skills — fresh pool from disk plus plugin contributions.
        self.skill_pool = SkillPool()
        self.skill_pool.load_all(self.project_dir, self.settings.skills.cross_tool_support)
        self.plugin_loader.apply_to_skills(self.skill_pool, disabled=self._disabled_plugins)

        # Agents — new pool, load disk definitions, apply plugin
        # contributions, rebuild. The old pool's runtime state
        # (active runs) lives on Agno's db, which we share, so it's
        # safe to swap.
        new_pool = AgentPool(db=self.db)
        new_pool.load_definitions(
            self.settings,
            self.project_dir,
            codeindex_available=self._codeindex_available,
        )
        self.plugin_loader.apply_to_agents(new_pool, disabled=self._disabled_plugins)
        if self.settings.orchestration.generate_ephemeral:
            new_pool.init_ephemeral(
                self.project_dir,
                self.settings.orchestration.max_ephemeral_per_session,
            )
        new_pool.build_agents()
        self.pool = new_pool

        # MCP — symmetric in both directions. Enabling a plugin
        # wires its servers in + auto-connects them; disabling
        # a plugin wires them OUT + disconnects them. Without the
        # disable side, a user who turns off a plugin sees its
        # skills/agents/hooks disappear but the MCP server keeps
        # running and showing up in ``/mcp`` — confusing state.
        #
        # ``apply_to_mcp`` only adds (first-wins); for the
        # disable case we need to *remove* stale entries first.
        # Algorithm:
        #
        # 1. Identify every config currently in ``configs`` whose
        #    name prefix matches a known plugin (i.e. was added
        #    by a previous ``apply_to_mcp``). User-configured
        #    servers (no plugin prefix) stay untouched.
        # 2. Wipe those plugin-contributed entries.
        # 3. Re-apply with the *current* disabled set — only
        #    enabled plugins re-add their configs.
        # 4. Diff the snapshot: added = present after, missing
        #    before; removed = present before, missing after.
        # 5. Disconnect removed servers (auto-handles the
        #    "disable plugin → kill its MCP" case). The user's
        #    "what if two plugins use the same server" concern is
        #    naturally handled by the ``<plugin>:<server>``
        #    naming — each plugin's contribution is independently
        #    addressable, so disabling one plugin only affects
        #    its own entries.
        # 6. Auto-connect added servers in the background.
        plugin_name_prefixes = tuple(f"{p.name}:" for p in self.plugin_loader.list_plugins())
        previously_plugin_owned = {
            name
            for name in self.mcp_manager.configs
            if any(name.startswith(p) for p in plugin_name_prefixes)
        }
        # Step 2 — wipe.
        for name in previously_plugin_owned:
            self.mcp_manager.configs.pop(name, None)
        # Step 3 — re-add for currently-enabled plugins.
        self.plugin_loader.apply_to_mcp(
            MCPConfigLoader(self.project_dir),
            self.mcp_manager.configs,
            disabled=self._disabled_plugins,
        )
        # Step 4 — diff.
        now_plugin_owned = {
            name
            for name in self.mcp_manager.configs
            if any(name.startswith(p) for p in plugin_name_prefixes)
        }
        added_mcp_names = now_plugin_owned - previously_plugin_owned
        removed_mcp_names = previously_plugin_owned - now_plugin_owned

        # Step 5 — disconnect what's gone, in the background.
        if removed_mcp_names:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._disconnect_removed_mcps(removed_mcp_names))
            except RuntimeError:
                logger.debug(
                    "No running loop — skipping MCP disconnect for: %s",
                    sorted(removed_mcp_names),
                )

        # Step 6 — auto-connect new ones, in the background.
        if added_mcp_names:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._auto_connect_mcps(added_mcp_names))
            except RuntimeError:
                logger.debug(
                    "Skipping MCP auto-connect (no running loop); use /mcp connect to start: %s",
                    sorted(added_mcp_names),
                )

        # Rebuild the main team so newly-bundled custom tools
        # (``<plugin>/tools/*.py``) and the refreshed agent pool are
        # visible to the live agent.
        self.main_team = self._build_main_agent()

        return {
            "plugins": len(self.plugin_loader.list_plugins()),
            "skills": len(self.skill_pool.list_skills()),
            "agents": len(self.pool.list_agents()),
            "hooks": sum(len(hl) for hl in self.hooks_map.values()),
        }

    async def _disconnect_removed_mcps(self, names: set[str]) -> None:
        """Disconnect MCP servers whose owning plugin was just
        disabled or removed.

        Symmetric counterpart to :py:meth:`_auto_connect_mcps`.
        Iterated sequentially so any final tool-call cleanup gets
        flushed cleanly per server. After all disconnects we
        rebuild the main team so the agent's tool surface drops
        the removed tools — without this, ``mcp_manager._clients``
        would lose the entry but the live ``Agent`` instance
        would still hold references, leading to "I can call tool
        X" hallucinations against a server that's gone.
        """
        logger.info(
            "Auto-disconnect: stopping %d MCP server(s): %s",
            len(names),
            sorted(names),
        )
        any_disconnected = False
        for name in sorted(names):
            try:
                ok = await self.mcp_manager.disconnect_one(name)
                if ok:
                    logger.info("Auto-disconnect: '%s' stopped", name)
                    any_disconnected = True
                else:
                    # Wasn't in ``_clients`` — typical if the
                    # server failed to connect earlier. The config
                    # removal still happened upstream; nothing
                    # else to do.
                    logger.info(
                        "Auto-disconnect: '%s' wasn't connected — no-op",
                        name,
                    )
            except Exception:
                logger.warning(
                    "Auto-disconnect of MCP server '%s' failed",
                    name,
                    exc_info=True,
                )

        # Rebuild even on a no-op stop set IF something was
        # actually live — same rationale as the auto-connect
        # path's rebuild. The team needs the new (smaller) tool
        # surface attached or the model will still try to call
        # the disconnected server's tools.
        if any_disconnected:
            logger.info("Auto-disconnect: rebuilding main team to drop stale MCP tools")
            self.rebuild_mcp()
            logger.info("Auto-disconnect: main team rebuilt")

    async def _auto_connect_mcps(self, names: set[str]) -> None:
        """Connect newly-contributed MCP servers in the background.

        Iterated sequentially (not in parallel) because the first-
        use approval prompt is a modal UI element — firing N of
        them at once would stack permission dialogs.

        After each successful connect we rebuild the main team so
        the live agent picks up the new MCP tools on its very next
        message — closing the race where ``ensure_mcp``'s
        once-per-session gate already flipped True and a
        plugin-install added a server that never gets attached to
        the agent. Failures are logged; the user can inspect them
        via ``/mcp``.
        """
        logger.info("Auto-connect: starting %d MCP server(s): %s", len(names), sorted(names))
        any_connected = False
        for name in sorted(names):
            try:
                t0 = asyncio.get_event_loop().time()
                client = await self.mcp_manager.connect(name)
                elapsed = asyncio.get_event_loop().time() - t0
                if client:
                    tool_count = len(getattr(client, "functions", None) or {})
                    logger.info(
                        "Auto-connect: '%s' connected in %.2fs (%d tool(s))",
                        name,
                        elapsed,
                        tool_count,
                    )
                    any_connected = True
                else:
                    logger.info(
                        "Auto-connect: '%s' not connected after %.2fs "
                        "(user denied, policy block, empty tools, or transport error)",
                        name,
                        elapsed,
                    )
            except Exception:
                logger.warning("Auto-connect of MCP server '%s' failed", name, exc_info=True)

        # Even one new client warrants a team rebuild — without this
        # the freshly-connected tools are visible in ``mcp_manager._clients``
        # but absent from the agent's tool surface. The model would
        # then quite reasonably say "I don't have access" because
        # the next message's Agent instance was built before the
        # client appeared.
        if any_connected:
            logger.info("Auto-connect: rebuilding main team to attach new MCP tools")
            self.rebuild_mcp()
            logger.info("Auto-connect: main team rebuilt")

    # ── Main Agent setup ────────────────────────────────────────────

    def _build_main_agent(self) -> Agent:
        """Build the main agent with all tools and orchestration capability.

        A single agent handles everything directly. When it needs a
        specialist, it calls spawn_agent() or spawn_team() via the
        OrchestrateTools toolkit — Agno handles sub-team execution.
        """
        # Core tools
        registry = ToolRegistry(
            base_dir=str(self.project_dir),
            permissions=ToolPermissions(
                project_dir=self.project_dir,
                settings_permissions=self.settings.permissions,
            ),
            cloud_token=self._cloud.access_token,
            cloud_server_url=self._cloud_server_url,
        )
        # Shell-first toolkit: Bash handles search/find/list/read directly
        # (`rg`, `find`, `cat`, etc.). Edit/Write are kept for surgical
        # changes and new files because shell-based alternatives (sed,
        # heredoc rewrites) are fragile. Grep/Glob/Read toolkits intentionally
        # omitted — they overlapped with shell and confused the model.
        tool_names = [
            "Write",
            "Edit",
            "Bash",
            "Schedule",
            "NotebookEdit",
        ]
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
        # CodeIndex tools are only exposed when there's a usable local
        # chroma index for the current git HEAD. Without one,
        # ``codeindex_search`` would return empty results and waste a
        # tool slot in the agent's catalog — hide it entirely. The
        # ``self._codeindex_available`` flag was set in __init__ before
        # pool.load_definitions ran (so the pool could pick the right
        # ``<name>.codeindex.md`` vs ``<name>.md`` variant per agent).
        if self._codeindex_available:
            tool_names.append("CodeIndex")
        tools = registry.resolve(tool_names)

        # Orchestration tools — lets the agent delegate to specialists
        from ember_code.core.tools.orchestrate import OrchestrateTools

        orchestrate = OrchestrateTools(
            pool=self.pool,
            settings=self.settings,
            current_depth=0,
            hook_executor=self.hook_executor,
            session_id=self.session_id,
            hitl_coordinator=self.sub_agent_hitl,
        )
        tools.append(orchestrate)

        # Reasoning tools (optional)
        reasoning = _create_reasoning_tools(self.settings)
        if reasoning:
            tools.append(reasoning)

        # Knowledge tools — chroma-backed; available when knowledge is configured.
        if self.knowledge is not None:
            from ember_code.core.tools.knowledge import KnowledgeTools

            tools.append(KnowledgeTools(self.knowledge_mgr))

        # Loop tools — let the agent start / stop the in-session loop
        # via tool calls so plain-language requests like *"keep doing
        # this for each item"* / *"stop the loop"* work without the
        # user typing the slash command. ``LoopProgressTool`` is the
        # per-iteration key/value scratchpad the model uses to track
        # which sub-tasks have already been completed across
        # iterations — without it, iteration N has no memory of what
        # iteration N-1 finished and the loop re-does work.
        from ember_code.core.tools.loop import LoopProgressTool, LoopTools

        tools.append(LoopTools(self))
        tools.append(LoopProgressTool(self))

        # MCP tools — connected MCP server clients
        connected_mcp = self.mcp_manager.list_connected()
        for mcp_name in connected_mcp:
            client = self.mcp_manager._clients.get(mcp_name)
            if client and client not in tools:
                tools.append(client)

        # Custom tools from .ember/tools/
        plugin_tool_dirs = self.plugin_loader.collect_tool_dirs(
            disabled=self._disabled_plugins,
        )
        custom_toolkits = registry.load_custom_tools(
            self.project_dir,
            plugin_tool_dirs=plugin_tool_dirs,
        )
        if custom_toolkits:
            tools.extend(custom_toolkits)

        # Tool event hooks (PreToolUse/PostToolUse/PostToolUseFailure)
        tool_event_hook = self._create_tool_event_hook()

        # System prompt with substitutions. When CodeIndex is available
        # we load ``main_agent.codeindex.md`` — a wholly CodeIndex-first
        # variant — instead of the plain ``main_agent.md``. The
        # CodeIndex variant has the tool reference inline and re-frames
        # tool preferences / read-before-edit / search guidance around
        # the index. The ``{{CODEINDEX_TOOLS}}`` placeholder only exists
        # in the plain variant; for the codeindex variant we substitute
        # the empty string (no-op since the placeholder isn't present).
        prompt_name = "main_agent.codeindex" if self._codeindex_available else "main_agent"
        prompt = load_prompt(prompt_name)
        prompt = prompt.replace(
            "{{AGENT_CATALOG}}", self._build_agent_catalog() or "(no agents loaded)"
        )
        prompt = prompt.replace("{{CODEINDEX_TOOLS}}", "")

        # Inject the per-commit Project Map. Auto-generated by
        # apply_delta; gives the agent a factual overview (taxonomy,
        # tables, cached-resource wrappers, vocabulary glossary,
        # entry points) before it issues a single tool call. Missing
        # map is non-fatal — the prompt still works without it.
        if self._codeindex_available:
            try:
                from ember_code.core.code_index.manifest import Manifest
                from ember_code.core.code_index.project_map import load_project_map

                manifest = Manifest(
                    project=self.project_dir,
                    data_dir=self.settings.storage.data_dir,
                )
                head_sha = manifest.load().head
                if head_sha:
                    map_md = load_project_map(
                        self.project_dir,
                        head_sha,
                        data_dir=self.settings.storage.data_dir,
                    )
                    if map_md:
                        prompt += "\n\n## Project Map\n\n" + map_md
            except Exception:  # pragma: no cover — defensive
                pass

        # Append skill descriptions if any
        skill_descriptions = self.skill_pool.describe()
        if skill_descriptions and self.settings.skills.auto_trigger:
            prompt += "\n\n## Available Skills (user can invoke via /name)\n" + skill_descriptions

        # Model + context window (capped by settings to keep compression aggressive)
        model_registry = ModelRegistry(self.settings)
        model = model_registry.get_model()
        context_window = min(
            model_registry.get_context_window(),
            self.settings.models.max_context_window,
        )

        # Instructions
        instructions = [prompt]
        if self.project_instructions:
            instructions.append(f"Project instructions:\n{self.project_instructions}")

        # Persistent TODO — root only, loaded automatically
        todo_path = self.project_dir / ".ember" / "TODO.md"
        if todo_path.is_file():
            todo_content = todo_path.read_text().strip()
            if todo_content:
                instructions.append(f"Active TODO (.ember/TODO.md):\n{todo_content}")

        # Multi-workspace context
        workspace_ctx = self.workspace.get_context_instructions()
        if workspace_ctx:
            instructions.append(workspace_ctx)
            for extra_dir in self.workspace.additional_dirs:
                extra_rules = load_project_context(
                    extra_dir,
                    self.settings.context.project_file,
                    read_claude_md=self.settings.rules.cross_tool_support,
                )
                if extra_rules:
                    instructions.append(f"Additional workspace ({extra_dir.name}):\n{extra_rules}")

        # Guardrails
        guardrails = _create_guardrails(self.settings)

        # Compression — triggers at 80% of context window
        compression = CompressionManager(
            model=model,
            compress_tool_results=True,
            compress_token_limit=int(context_window * 0.8),
        )

        agent = Agent(
            name="ember",
            model=model,
            tools=tools,
            instructions=instructions,
            markdown=True,
            # Retry transient model-API failures (timeouts, 5xx) before
            # bubbling the error up to the user. Same default as the
            # specialist pool — see ``pool.build_agent``.
            retries=getattr(self.settings.models, "retries", 2),
            # Session persistence
            db=self.db,
            session_id=self.session_id,
            user_id=self.user_id,
            # History — keep all turns until 80% compaction triggers
            add_history_to_context=True,
            num_history_runs=10000,
            # Memory — agentic memory removed; LearningMachine handles learning.
            # Existing memories still loaded into context.
            enable_agentic_memory=False,
            add_memories_to_context=self.settings.memory.add_memories_to_context,
            # Compression
            compress_tool_results=True,
            compression_manager=compression,
            # Session summaries — disabled at init to avoid per-turn LLM calls.
            # _compact() creates the manager on demand. Existing summaries
            # from prior compaction are still injected if present.
            enable_session_summaries=False,
            add_session_summary_to_context=True,
            # Streaming
            stream=True,
            stream_events=True,
            # Knowledge — agents reach the index via the ``KnowledgeTools`` toolkit,
            # not Agno's built-in ``search_knowledge``. Our facade isn't an
            # ``agno.knowledge.Knowledge`` instance and Agno's Weaviate adapter
            # uses a different vectorizer path than our text2vec-transformers MT
            # collections, so we pass nothing here.
            knowledge=None,
            search_knowledge=False,
            # Guardrails
            pre_hooks=guardrails,
            # Learning — wired so Agno surfaces ``update_user_memory``
            # as a tool. The earlier "blocks arun" concern was about
            # ``mode=ALWAYS`` automatic extraction; we now configure
            # user_memory in AGENTIC mode (see ``core/learn.py``), so
            # the only model call is the one fired when the agent
            # explicitly decides to call ``update_user_memory(task)``.
            # ``_inject_learnings()`` below still runs as a
            # belt-and-suspenders context injection.
            learning=self._learning,
            add_learnings_to_context=True,
            # Tool event hooks
            tool_hooks=[tool_event_hook],
        )
        return agent

    async def _inject_learnings(self) -> None:
        """Inject learning context into the main agent's instructions."""
        if self._learning is None:
            return
        if self._learning.model is None:
            from ember_code.core.config.models import ModelRegistry

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

    def _create_tool_event_hook(self) -> ToolEventHook:
        """Create a ToolEventHook for tool event hooks and protected path enforcement."""
        return ToolEventHook(
            executor=self.hook_executor,
            session_id=self.session_id,
            protected_paths=self.settings.safety.protected_paths,
            blocked_commands=self.settings.safety.blocked_commands,
        )

    # ── Lazy knowledge initialization (no-op while phase 3 is pending) ──

    def start_knowledge_background(self) -> None:
        """Stub — the chroma-backed knowledge index is rebuilt in phase 3."""
        return

    async def _ensure_knowledge(self) -> None:
        """Stub — knowledge is offline during the chroma migration."""
        return

    def start_codeindex_background(self) -> None:
        """Fire an initial sync, evict stale commit chromas, and start
        the HEAD watcher — all fire-and-forget on the running loop.

        ``CodeIndex.clean()`` drops every commit that isn't HEAD, isn't
        a branch tip, and hasn't been touched in the last 30 days. We
        run it once per session after the initial sync so the cutoff
        applies to a freshly-refreshed manifest — HEAD's
        ``last_used_at`` was just bumped by ``sync_now``, so we won't
        accidentally evict the current commit even if its previous
        timestamp was old.
        """

        async def _bootstrap() -> None:
            # Sweep orphaned chroma dirs from prior sessions BEFORE we
            # open any client. ``CodeIndex.clean`` defers the
            # filesystem ``rmtree`` until startup so it doesn't pull
            # the rug out from under a live chromadb client (same
            # trap that bit ``forget_commit`` in v0.5.8). The first
            # safe chance to finish that eviction is right here,
            # before ``sync_now`` constructs the first PersistentClient.
            try:
                swept = self.code_index.sweep_stale_dirs()
                if swept:
                    logger.info(
                        "Reclaimed %d orphaned chroma dir(s): %s",
                        len(swept),
                        ", ".join(s[:8] for s in swept[:5]) + ("…" if len(swept) > 5 else ""),
                    )
            except Exception as exc:
                logger.debug("sweep_stale_dirs failed (%s); continuing", exc)

            await self.code_index_sync.sync_now()
            # If that initial sync populated the chroma (most common
            # case: fresh checkout, prior session wiped, first
            # install), the agent built earlier in __init__ has the
            # wrong system prompt — it was constructed before any
            # data was available. Recheck and rebuild if so.
            try:
                self.refresh_codeindex_availability()
            except Exception as exc:
                logger.debug(
                    "refresh_codeindex_availability after initial sync failed (%s)",
                    exc,
                )
            try:
                dropped = await self.code_index.clean()
                if dropped:
                    logger.info(
                        "Auto-clean dropped %d idle commit chroma(s): %s",
                        len(dropped),
                        ", ".join(s[:8] for s in dropped[:5]) + ("…" if len(dropped) > 5 else ""),
                    )
            except Exception as exc:
                logger.debug("Auto-clean failed (%s); continuing", exc)
            await self.code_index_sync.start_watcher()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # No running loop yet — caller will trigger us once one exists.
        loop.create_task(_bootstrap())

    def start_marketplace_refresh_background(self) -> None:
        """Refresh every registered plugin marketplace catalog in the
        background.

        Mirrors :meth:`start_codeindex_background` — fire-and-forget on
        the running loop, no throttle, per-marketplace timeout, all
        failures logged and swallowed. Net effect: by the time the
        user reaches for ``/plugin install @foo/bar`` (seconds to
        minutes later) the catalog is current. Session start is
        unaffected even if every marketplace is unreachable.
        """
        from ember_code.core.plugins.marketplaces import (
            load_registry,
            refresh_marketplace,
        )

        async def _refresh_all() -> None:
            # Auto-register the canonical defaults (Anthropic's
            # official marketplace, mainly) before refreshing so a
            # brand-new install sees plugins on first open without
            # the user having to run ``/plugin marketplace add``.
            # Idempotent — ``add_marketplace`` updates in place when
            # a marketplace by the same name already exists.
            from ember_code.core.plugins.marketplaces import (
                DEFAULT_MARKETPLACES,
                add_marketplace,
            )

            registry = load_registry(self.settings.storage.data_dir)
            registered_names = {m.name for m in registry.marketplaces}
            for default_name, default_url in DEFAULT_MARKETPLACES:
                if default_name in registered_names:
                    continue
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(
                            add_marketplace,
                            default_url,
                            data_dir=self.settings.storage.data_dir,
                        ),
                        timeout=15.0,
                    )
                    logger.info(
                        "Auto-registered default marketplace: %s",
                        default_name,
                    )
                except Exception as e:  # noqa: BLE001 — best-effort
                    logger.warning(
                        "Auto-registering default marketplace '%s' "
                        "failed: %s — user can add manually later.",
                        default_name,
                        e,
                    )

            # Refresh whatever is now registered (defaults + any
            # user-added marketplaces). Re-read the registry since
            # the auto-register step may have appended entries.
            registry = load_registry(self.settings.storage.data_dir)
            for entry in registry.marketplaces:
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(
                            refresh_marketplace,
                            entry.name,
                            data_dir=self.settings.storage.data_dir,
                        ),
                        timeout=10.0,
                    )
                except Exception as e:  # noqa: BLE001 — best-effort
                    logger.warning(
                        "Marketplace refresh for '%s' failed: %s",
                        entry.name,
                        e,
                    )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(_refresh_all())

    # ── MCP initialization (async, runs once) ──────────────────────

    async def ensure_mcp(self) -> None:
        """Connect user-configured MCP servers and rebuild agents.

        Reads from .mcp.json / .ember/.mcp.json.  No auto-detection —
        only servers the user explicitly configured are connected.
        Runs once on first message.

        INFO-level log lines bracket each connect so the timeline
        is reconstructable from ``~/.ember/debug.log`` — diagnosing
        a "MCP says connected but the agent doesn't see the tools"
        race needs to know exactly when each server came online vs.
        when the model fired its first tool call.
        """
        if self._mcp_initialized:
            return
        self._mcp_initialized = True

        available = self.mcp_manager.list_servers()
        if not available:
            logger.info("MCP init: no configured servers; skipping connect loop")
            return

        logger.info("MCP init: connecting %d server(s): %s", len(available), available)
        clients: dict[str, Any] = {}
        for name in available:
            t0 = asyncio.get_event_loop().time()
            client = await self.mcp_manager.connect(name)
            elapsed = asyncio.get_event_loop().time() - t0
            if client is not None:
                # Tool count surfaces the most common silent-failure
                # mode: server-side gating on auth that returns zero
                # tools. We let the connect succeed (Agno wraps it
                # in MCPTools) but flag the empty case explicitly.
                tool_count = len(getattr(client, "functions", None) or {})
                logger.info(
                    "MCP init: connected '%s' in %.2fs (%d tool(s))",
                    name,
                    elapsed,
                    tool_count,
                )
                clients[name] = client
            else:
                error = self.mcp_manager.get_error(name)
                logger.info(
                    "MCP init: connection to '%s' failed after %.2fs: %s",
                    name,
                    elapsed,
                    error or "unknown error",
                )
                print_info(f"MCP '{name}' connection failed: {error or 'unknown error'}")

        if not clients:
            logger.info("MCP init: no clients to attach; team rebuild skipped")
            return

        # Rebuild agents with MCP tools included, then rebuild main team
        logger.info(
            "MCP init: rebuilding agents + main team with %d MCP client(s)",
            len(clients),
        )
        self.pool.build_agents(mcp_clients=clients)
        self.main_team = self._build_main_agent()
        logger.info("MCP init: agents + main team rebuilt — tools active")

    def rebuild_mcp(self) -> None:
        """Rebuild agents and main agent with current MCP client set.

        Called after toggling individual MCP servers on/off.
        """
        connected = self.mcp_manager.list_connected()
        clients = {name: self.mcp_manager._clients[name] for name in connected}
        self.pool.build_agents(mcp_clients=clients if clients else None)
        self.main_team = self._build_main_agent()

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

    async def _compact(self) -> None:
        """Generate a summary of the conversation, then clear old messages.

        1. Generate summary covering the full conversation
        2. Delete all runs from the session (summary preserved)
        3. Enable summary injection so the agent has context

        After compaction, messages accumulate fresh until next compaction.
        """
        # Load the session from DB
        agno_session = await self.main_team.aget_session(
            session_id=self.session_id,
            user_id=self.user_id,
        )
        if agno_session is None:
            logger.warning("No session found to compact")
            return

        # Create summary manager and generate summary
        try:
            from agno.session.summary import SessionSummaryManager

            ssm = SessionSummaryManager(model=self.main_team.model)
            await ssm.acreate_session_summary(session=agno_session)
            logger.info("Session summary generated")
        except Exception as e:
            logger.warning("Failed to generate session summary: %s", e)

        # Clear runs — summary stays
        agno_session.runs = []
        try:
            await self.main_team.asave_session(agno_session)
            logger.info("Session runs cleared from DB")
        except Exception as e:
            logger.warning("Failed to save session: %s", e)

        # Rebuild the main agent from scratch. This is the only reliable
        # way to clear Agno's in-memory message history — the cached
        # session, run_response, and internal state all hold old messages.
        self.main_team = self._build_main_agent()
        logger.info("Compacted: summary injected, agent rebuilt")

    async def compact_if_needed(self, input_tokens: int, context_window: int) -> bool:
        """Auto-compact at 80% context usage.

        Messages accumulate freely until context fills up. At 80%,
        a summary is generated and old turns are dropped.

        Returns True if compaction was applied.
        """
        if context_window <= 0 or input_tokens <= 0:
            return False

        usage = input_tokens / context_window
        if usage < 0.8:
            return False

        await self._compact()
        logger.info("Auto-compacted at %.0f%% context usage", usage * 100)
        return True

    async def force_compact(self) -> tuple[str, str]:
        """Manually compact conversation context.

        Returns (status_message, summary_text).
        """
        # Check if there's anything to compact
        try:
            agno_session = await self.main_team.aget_session(
                session_id=self.session_id,
                user_id=self.user_id,
            )
            if agno_session is None or not agno_session.runs:
                return "Nothing to compact — no conversation history.", ""
        except Exception:
            pass

        await self._compact()

        # Retrieve the generated summary from DB
        summary = ""
        try:
            agno_session = await self.main_team.aget_session(
                session_id=self.session_id,
                user_id=self.user_id,
            )
            if agno_session and agno_session.summary:
                summary = agno_session.summary.summary or ""
        except Exception:
            pass

        return "Context compacted. Conversation summarized, history cleared.", summary

    # ── Debug logging ─────────────────────────────────────────────────

    def _log_run_messages(self) -> None:
        """Dump messages from the last run for debugging tool result delivery."""
        try:
            rr = getattr(self.main_team, "run_response", None)
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

    # ── Message handling (headless path) ──────────────────────────────

    async def handle_message(self, message: str, **media_kwargs) -> str:
        """Handle a single user message and return the response.

        Accepts optional media keyword arguments (images, audio, videos, files)
        which are forwarded directly to team.arun().
        """

        # ── Connect MCP servers on first message ──────────────────────
        await self.ensure_mcp()

        # ── Hook: UserPromptSubmit (can block) ───────────────────────
        hook_result = await self.hook_executor.execute(
            event=HookEvent.USER_PROMPT_SUBMIT.value,
            payload={"message": message, "session_id": self.session_id},
        )
        if not hook_result.should_continue:
            blocked_msg = hook_result.message or "Blocked by UserPromptSubmit hook."
            self.audit.log(
                session_id=self.session_id,
                agent_name="session",
                tool_name="user_prompt",
                status="BLOCKED",
                details={"reason": blocked_msg},
            )
            return blocked_msg

        # ── Guardrails (inform, don't block) ──────────────────────────
        guardrail_prefix = ""
        if self.guardrail_runner.enabled:
            gr_results = await self.guardrail_runner.check(message)
            if gr_results:
                warnings = "; ".join(r.message for r in gr_results)
                guardrail_prefix = (
                    f"[GUARDRAIL WARNING] The following issues were detected in "
                    f"the user message: {warnings}\n"
                    f"Please be cautious and do not repeat or use any flagged content.\n\n"
                )
                logger.info("Guardrails triggered: %s", warnings)

        try:
            # ── Execute (Agno auto-persists via db) ──────────────────
            from datetime import datetime

            timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
            effective_message = (
                f"<system-context>Current datetime: {timestamp}</system-context>\n{message}"
            )
            if guardrail_prefix:
                effective_message = guardrail_prefix + effective_message
            response = await self.main_team.arun(effective_message, stream=False, **media_kwargs)
            self._log_run_messages()
            response_text = extract_response_text(response)

            # ── Audit log ────────────────────────────────────────────
            self.audit.log(
                session_id=self.session_id,
                agent_name="ember",
                tool_name="main_team",
                status="success",
            )

            # ── Hook: Stop (can block up to 3 times) ─────────────────
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
                # Hook blocked — feed the rejection back to the agent
                feedback = stop_result.message or "Response blocked by Stop hook."
                system_msg = (
                    f"[SYSTEM] Your previous response was rejected by a Stop hook: "
                    f"{feedback}\nPlease revise your response to address this issue."
                )
                response = await self.main_team.arun(system_msg, stream=False)
                response_text = extract_response_text(response)

            # ── Compact history if approaching context limit ─────────
            metrics = getattr(getattr(self.main_team, "run_response", None), "metrics", None)
            if metrics:
                input_tokens = getattr(metrics, "input_tokens", 0) or 0
                await self.compact_if_needed(input_tokens, self._context_window)

            return response_text

        except Exception as e:
            error_msg = f"Error handling message: {e}"
            print_error(error_msg)

            self.audit.log(
                session_id=self.session_id,
                agent_name="session",
                tool_name="main_team",
                status="error",
                details={"error": str(e)},
            )

            return error_msg


# ── Factory helpers ────────────────────────────────────────────────


def _create_reasoning_tools(settings: Settings) -> Any | None:
    """Create Agno ReasoningTools from config."""
    if not settings.reasoning.enabled:
        return None
    try:
        from agno.tools.reasoning import ReasoningTools

        return ReasoningTools(
            add_instructions=settings.reasoning.add_instructions,
            add_few_shot=settings.reasoning.add_few_shot,
        )
    except ImportError:
        logger.debug("agno.tools.reasoning not available")
        return None


def _create_guardrails(settings: Settings) -> list | None:
    """Create Agno guardrail pre_hooks from config."""
    hooks: list = []
    cfg = settings.guardrails

    if cfg.pii_detection:
        try:
            from agno.guardrails.pii import PIIDetectionGuardrail

            hooks.append(PIIDetectionGuardrail())
        except ImportError:
            logger.debug("agno.guardrails.pii not available")

    if cfg.prompt_injection:
        try:
            from agno.guardrails.prompt_injection import PromptInjectionGuardrail

            hooks.append(PromptInjectionGuardrail())
        except ImportError:
            logger.debug("agno.guardrails.prompt_injection not available")

    if cfg.moderation:
        try:
            from agno.guardrails.openai import OpenAIModerationGuardrail

            hooks.append(OpenAIModerationGuardrail())
        except ImportError:
            logger.debug("agno.guardrails.openai not available")

    return hooks if hooks else None
