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
        self.project_dir = project_dir or Path.cwd()
        self.workspace = WorkspaceManager(self.project_dir, additional_dirs)
        self.session_id = resume_session_id or str(uuid.uuid4())[:8]
        self.session_named = bool(resume_session_id)
        self.user_id = getpass.getuser()

        # ── /loop state (in-session, ephemeral, not persisted) ───────
        # When the user invokes ``/loop <prompt>``, the same prompt is
        # automatically re-fired as the next turn after the agent
        # finishes — until ``/loop stop``, any non-``/loop`` user
        # input, or the safety cap. State lives here so the FE can
        # pop it after each ``_drain_queue`` returns empty.
        self.pending_loop_prompt: str | None = None
        self.loop_iteration_index: int = 0
        self.loop_iterations_remaining: int = 0

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

        # ── Hooks ────────────────────────────────────────────────────
        self._hook_loader = HookLoader(
            self.project_dir, cross_tool_support=settings.hooks.cross_tool_support
        )
        self.hooks_map = self._hook_loader.load()
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
        if settings.orchestration.generate_ephemeral:
            self.pool.init_ephemeral(
                self.project_dir, settings.orchestration.max_ephemeral_per_session
            )
        self.pool.build_agents()  # initial build without MCP

        # ── Skill Pool ───────────────────────────────────────────────
        self.skill_pool = SkillPool()
        self.skill_pool.load_all(self.project_dir, settings.skills.cross_tool_support)

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
        # user typing the slash command.
        from ember_code.core.tools.loop import LoopTools

        tools.append(LoopTools(self))

        # MCP tools — connected MCP server clients
        connected_mcp = self.mcp_manager.list_connected()
        for mcp_name in connected_mcp:
            client = self.mcp_manager._clients.get(mcp_name)
            if client and client not in tools:
                tools.append(client)

        # Custom tools from .ember/tools/
        custom_toolkits = registry.load_custom_tools(self.project_dir)
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
        """Fire an initial sync and start the HEAD watcher (fire-and-forget)."""

        async def _bootstrap() -> None:
            await self.code_index_sync.sync_now()
            await self.code_index_sync.start_watcher()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # No running loop yet — caller will trigger us once one exists.
        loop.create_task(_bootstrap())

    # ── MCP initialization (async, runs once) ──────────────────────

    async def ensure_mcp(self) -> None:
        """Connect user-configured MCP servers and rebuild agents.

        Reads from .mcp.json / .ember/.mcp.json.  No auto-detection —
        only servers the user explicitly configured are connected.
        Runs once on first message.
        """
        if self._mcp_initialized:
            return
        self._mcp_initialized = True

        available = self.mcp_manager.list_servers()
        if not available:
            return

        clients: dict[str, Any] = {}
        for name in available:
            client = await self.mcp_manager.connect(name)
            if client is not None:
                clients[name] = client
            else:
                error = self.mcp_manager.get_error(name)
                print_info(f"MCP '{name}' connection failed: {error or 'unknown error'}")

        if not clients:
            return

        # Rebuild agents with MCP tools included, then rebuild main team
        self.pool.build_agents(mcp_clients=clients)
        self.main_team = self._build_main_agent()

    def rebuild_mcp(self) -> None:
        """Rebuild agents and main agent with current MCP client set.

        Called after toggling individual MCP servers on/off.
        """
        connected = self.mcp_manager.list_connected()
        clients = {name: self.mcp_manager._clients[name] for name in connected}
        self.pool.build_agents(mcp_clients=clients if clients else None)
        self.main_team = self._build_main_agent()

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
