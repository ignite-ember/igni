"""``Session._build_main_agent`` extracted as a free function.

Extracted from ``core.py`` — the 400-line agent construction
routine that assembles tools, prompt, guardrails, compression
manager, and every Agno-side switch (streaming, memory,
learning, hooks) into a single :class:`Agent` instance.

Kept as one big function because every step is tightly
coupled to the ``Session`` instance's state (registry,
knowledge, plugin loader, workspace, output styles). Splitting
it further would only add plumbing without clarifying the
build sequence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# Test-patch transparency: tests patch symbols at
# ``ember_code.core.session.core.<Name>`` (Agent,
# CompressionManager, ToolRegistry, _create_reasoning_tools,
# _create_guardrails, etc.). Look them up via the module at
# call time so those patches still take effect after the
# extract. See ``session_core`` alias below.
import ember_code.core.session.core as _session_core

from ember_code.core.code_index.manifest import Manifest
from ember_code.core.code_index.project_map import load_project_map
from ember_code.core.tools.knowledge import KnowledgeTools
from ember_code.core.tools.loop import LoopProgressTool, LoopTools
from ember_code.core.tools.lsp import LspTools
from ember_code.core.tools.monitors import MonitorTools
from ember_code.core.tools.orchestrate import OrchestrateTools
from ember_code.core.tools.plan import PlanTool
from ember_code.core.tools.slash import SlashCommandTool
from ember_code.core.tools.todo import TodoTools
from ember_code.core.utils.context import (
    load_project_context,
    memory_writeback_instructions,
)

if TYPE_CHECKING:
    from agno.agent import Agent

    from ember_code.core.session.core import Session


def build_main_agent(session: "Session") -> "Agent":
    """See :meth:`Session._build_main_agent`."""
    """Build the main agent with all tools and orchestration capability.

    A single agent handles everything directly. When it needs a
    specialist, it calls spawn_agent() or spawn_team() via the
    OrchestrateTools toolkit — Agno handles sub-team execution.
    """
    # Core tools
    registry = _session_core.ToolRegistry(
        base_dir=str(session.project_dir),
        permissions=_session_core.ToolPermissions(
            project_dir=session.project_dir,
            settings_permissions=session.settings.permissions,
        ),
        cloud_token=session._cloud.access_token,
        cloud_server_url=session._cloud_server_url,
        broadcast=session.broadcast,
    )
    tool_names = session._resolve_main_tool_names(registry)
    tools = registry.resolve(tool_names)

    # Orchestration tools — lets the agent delegate to specialists
    from ember_code.core.tools.orchestrate import OrchestrateTools

    orchestrate = OrchestrateTools(
        pool=session.pool,
        settings=session.settings,
        current_depth=0,
        hook_executor=session.hook_executor,
        session_id=session.session_id,
        hitl_coordinator=session.sub_agent_hitl,
        project_dir=session.project_dir,
    )
    tools.append(orchestrate)

    # Reasoning tools (optional)
    reasoning = _session_core._create_reasoning_tools(session.settings)
    if reasoning:
        tools.append(reasoning)

    # Knowledge tools — chroma-backed; available when knowledge is configured.
    if session.knowledge is not None:
        from ember_code.core.tools.knowledge import KnowledgeTools

        tools.append(KnowledgeTools(session.knowledge_mgr))

    # Loop tools — let the agent start / stop the in-session loop
    # via tool calls so plain-language requests like *"keep doing
    # this for each item"* / *"stop the loop"* work without the
    # user typing the slash command. ``LoopProgressTool`` is the
    # per-iteration key/value scratchpad the model uses to track
    # which sub-tasks have already been completed across
    # iterations — without it, iteration N has no memory of what
    # iteration N-1 finished and the loop re-does work.
    from ember_code.core.tools.loop import LoopProgressTool, LoopTools

    tools.append(LoopTools(session))
    tools.append(LoopProgressTool(session))

    # TodoWrite — agent-facing planning tool (CC parity).
    # The model uses it to maintain a per-session todo list
    # that the UI can render alongside the chat. Keeps
    # multi-step plans visible without scrolling back through
    # reasoning output.
    from ember_code.core.tools.todo import TodoTools

    tools.append(TodoTools(session))

    # Plan mode — ``exit_plan_mode`` agent tool (CC parity,
    # row 50). The user toggles plan mode via ``/plan`` (which
    # flips ``permissions.mode`` to ``plan`` and blocks file
    # edits via ``PermissionEvaluator``); this tool lets the
    # agent signal "plan ready for review" at the end of a
    # plan-mode turn. Mode flip back to default stays
    # user-controlled — the agent can't exit the sandbox on
    # its own.
    from ember_code.core.tools.plan import PlanTool

    tools.append(PlanTool(session))

    # SlashCommand — agent-facing re-entrant slash command
    # dispatch (CC parity). Lets the agent invoke ``/help``,
    # ``/ctx``, ``/codeindex search …``, etc. from inside a
    # tool-using turn. A small blocklist (``/quit``, ``/clear``,
    # ``/model``, ``/login``, ``/logout``) is refused with an
    # explanatory error — those would either kill the session
    # or require UI interaction the agent can't provide.
    from ember_code.core.tools.slash import SlashCommandTool

    tools.append(SlashCommandTool(session))

    # LSP query tool — exposes plugin-declared language
    # servers (CC parity, row 32). Only registered when at
    # least one server is configured so the toolkit doesn't
    # clutter the agent's tool list in sessions that don't
    # use LSP. Lazy-launch happens inside ``LspServerManager``
    # on first ``lsp_query`` call.
    if session.lsp_manager is not None and session.lsp_manager.list_servers():
        from ember_code.core.tools.lsp import LspTools

        tools.append(LspTools(session.lsp_manager))

    # Monitor inspection tools (CC parity, row 33). Only
    # registered when at least one monitor is configured —
    # same logic as the LSP toolkit. Monitors are
    # plugin-owned background processes; the agent observes
    # status / output and can restart, but can't define new
    # monitors at runtime.
    if session.monitor_manager is not None and session.monitor_manager.list_names():
        from ember_code.core.tools.monitors import MonitorTools

        tools.append(MonitorTools(session.monitor_manager))

    # MCP tools — connected MCP server clients
    connected_mcp = session.mcp_manager.list_connected()
    for mcp_name in connected_mcp:
        client = session.mcp_manager._clients.get(mcp_name)
        if client and client not in tools:
            tools.append(client)

    # Custom tools from .ember/tools/
    plugin_tool_dirs = session.plugin_loader.collect_tool_dirs(
        disabled=session._disabled_plugins,
    )
    custom_toolkits = registry.load_custom_tools(
        session.project_dir,
        plugin_tool_dirs=plugin_tool_dirs,
    )
    if custom_toolkits:
        tools.extend(custom_toolkits)

    # Tool event hooks (PreToolUse/PostToolUse/PostToolUseFailure)
    tool_event_hook = session._create_tool_event_hook()

    # System prompt with substitutions. When CodeIndex is available
    # we load ``main_agent.codeindex.md`` — a wholly CodeIndex-first
    # variant — instead of the plain ``main_agent.md``. The
    # CodeIndex variant has the tool reference inline and re-frames
    # tool preferences / read-before-edit / search guidance around
    # the index. The ``{{CODEINDEX_TOOLS}}`` placeholder only exists
    # in the plain variant; for the codeindex variant we substitute
    # the empty string (no-op since the placeholder isn't present).
    prompt_name = "main_agent.codeindex" if session._codeindex_available else "main_agent"
    prompt = _session_core.load_prompt(prompt_name)
    prompt = prompt.replace(
        "{{AGENT_CATALOG}}", session._build_agent_catalog() or "(no agents loaded)"
    )
    prompt = prompt.replace("{{CODEINDEX_TOOLS}}", "")

    # Inject the per-commit Project Map. Auto-generated by
    # apply_delta; gives the agent a factual overview (taxonomy,
    # tables, cached-resource wrappers, vocabulary glossary,
    # entry points) before it issues a single tool call. Missing
    # map is non-fatal — the prompt still works without it.
    if session._codeindex_available:
        try:
            from ember_code.core.code_index.manifest import Manifest
            from ember_code.core.code_index.project_map import load_project_map

            manifest = Manifest(
                project=session.project_dir,
                data_dir=session.settings.storage.data_dir,
            )
            head_sha = manifest.load().head
            if head_sha:
                map_md = load_project_map(
                    session.project_dir,
                    head_sha,
                    data_dir=session.settings.storage.data_dir,
                )
                if map_md:
                    prompt += "\n\n## Project Map\n\n" + map_md
        except Exception:  # pragma: no cover — defensive
            pass

    # Append skill descriptions if any
    skill_descriptions = session.skill_pool.describe()
    if skill_descriptions and session.settings.skills.auto_trigger:
        prompt += "\n\n## Available Skills (user can invoke via /name)\n" + skill_descriptions

    # Model + context window (capped by settings to keep compression aggressive)
    model_registry = _session_core.ModelRegistry(session.settings)
    model = model_registry.get_model()
    context_window = min(
        model_registry.get_context_window(),
        session.settings.models.max_context_window,
    )

    # Instructions
    instructions = [prompt]
    if session.project_instructions:
        instructions.append(f"Project instructions:\n{session.project_instructions}")

    # Persistent TODO — root only, loaded automatically
    todo_path = session.project_dir / ".ember" / "TODO.md"
    if todo_path.is_file():
        todo_content = todo_path.read_text().strip()
        if todo_content:
            instructions.append(f"Active TODO (.ember/TODO.md):\n{todo_content}")

    # Multi-workspace context
    workspace_ctx = session.workspace.get_context_instructions()
    if workspace_ctx:
        instructions.append(workspace_ctx)
        for extra_dir in session.workspace.additional_dirs:
            extra_rules = load_project_context(
                extra_dir,
                session.settings.context.project_file,
                read_claude_md=session.settings.rules.cross_tool_support,
            )
            if extra_rules:
                instructions.append(f"Additional workspace ({extra_dir.name}):\n{extra_rules}")

    # Plan-mode nudge (row 50 UX) — the model needs concrete
    # cues here or it falls back to the existing
    # ``spawn_team(mode="tasks")`` pattern (which also plans-
    # then-executes, but bypasses the user-approval gate this
    # mode provides). Three specific instructions:
    # 1. WHEN to enter (concrete examples, not just adjectives)
    # 2. The DIFFERENCE from spawn_team / tasks mode (the
    #    user-approval gate is the headline distinction).
    # 3. When CodeIndex is available, a strong nudge to use it
    #    heavily during the read-only research phase — the
    #    index is condensed source-of-truth (LLM-distilled
    #    summaries per code entity), so a few targeted queries
    #    beat a dozen file reads on real-world repos.
    plan_mode_nudge = (
        "PLAN MODE — agent self-discipline before complex work\n\n"
        "When the user asks for any of these, your VERY FIRST "
        "tool call must be `enter_plan_mode(reason)` — before "
        "reading, searching, or anything else:\n"
        '* Multi-file refactor (e.g. "refactor the auth system", '
        '"rename Foo → Bar across the codebase")\n'
        '* Architectural change (e.g. "move X to its own service", '
        '"replace the cookie session with JWT")\n'
        "* Broad feature addition spanning multiple modules\n"
        "* Anything where committing to a direction without "
        "checking with the user first would be expensive to undo\n\n"
        "After entering plan mode you can read, search, grep, "
        "consult the codeindex — but file edits and mutating "
        "shell are blocked. When you've gathered enough context, "
        "call `exit_plan_mode(plan, tasks=[...])` with a concrete "
        "proposal and STOP. The user clicks Approve in the UI; "
        "the next turn executes.\n\n"
        "Include `tasks=[...]` whenever the steps are "
        "enumerable — one entry per execution step, shape "
        '`{content: "Imperative description", activeForm: '
        '"Verb-noun gerund"}`. The user sees both your prose '
        "plan AND a live checklist; as you call `todo_write` "
        "during execution, the checklist ticks off in their "
        "UI in real time. Skip `tasks` only when the plan is "
        'genuinely unstructured (e.g. "I propose option A '
        'because…" — no enumerable steps).\n\n'
        'Plan mode vs spawn_team(mode="tasks"): plan mode pauses '
        "for USER approval before execution; tasks mode runs to "
        "completion autonomously. For requests involving file "
        "writes, prefer plan mode so the user sees the plan "
        "first. For pure research / read-only tasks where you'd "
        "synthesise an answer anyway, just answer directly.\n\n"
        "Skip plan mode for simple one-shot requests (a small "
        "bug fix, one obvious tweak, a typo correction)."
    )
    if session._codeindex_available:
        # CodeIndex is the condensed source-of-truth: LLM-
        # generated summaries of every meaningful code entity
        # in the project, queryable by natural language or
        # symbol name. In plan mode the agent SHOULD lean on
        # it heavily — a few targeted queries answer
        # "where does X live + what does it actually do" far
        # more cheaply than grep-and-read cycles on a real
        # repo. This block is appended only when the index is
        # actually populated for the current HEAD; otherwise
        # the model would be told to use a tool that returns
        # empty results.
        plan_mode_nudge += (
            "\n\n"
            "**CodeIndex is available for THIS commit** — use it "
            "as your PRIMARY research surface in plan mode. "
            "CodeIndex is a semantic index of every meaningful "
            "entity in this repo with an LLM-generated summary "
            "(condensed source-of-truth — one query often "
            "replaces five file reads). Plan-mode workflow with "
            "CodeIndex:\n"
            "1. Call `enter_plan_mode(reason)`.\n"
            "2. Fire several `codeindex_query` calls FIRST, "
            "from different angles: by feature ('JWT validation', "
            "'session storage'), by symbol name "
            "('AuthMiddleware', 'login_user'), by area "
            "('frontend auth', 'backend middleware'). Queries "
            "are cheap — issue a handful before reading any "
            "files.\n"
            "3. For any entity that looks central, drill in via "
            "`codeindex_tree` to see what depends on it / what "
            "it imports — that's how you find the blast radius "
            "of a refactor.\n"
            "4. `file_read` is for things the index couldn't "
            "tell you OR when you need exact source (a "
            "specific function body the index summarised as "
            '"validates X" but you need the validation logic). '
            "Don't read files BEFORE consulting the index — "
            "you'll be reading blind.\n"
            "5. Build the `plan` markdown and `tasks=[...]` "
            "from what CodeIndex told you. Cite specific files "
            "and functions surfaced by the index. Plans "
            "grounded in real codebase facts beat plans built "
            "from prior assumptions.\n\n"
            "Heuristic: if a plan-mode turn doesn't call "
            "`codeindex_query` at least 2-3 times, you "
            "probably haven't done enough research."
        )
    instructions.append(plan_mode_nudge)

    # Output style (row 52) — appends the active style's
    # body to the system prompt so the agent's communication
    # mode shifts without rebuilding the agent. Loaded by
    # ``discover_output_styles`` at session init; switched
    # via ``/output-style <name>``. Empty when no styles
    # are configured (the session still boots — falls back
    # to bare model behaviour).
    style = session.output_styles.get(session._active_output_style)
    if style and style.body:
        instructions.append(f"# Output style: {style.name}\n\n{style.body}")

    # Auto-memory write-back (row 61). Teaches the agent
    # WHEN and HOW to persist memories during this
    # conversation — the READ side (loading existing
    # MEMORY.md into the system prompt) landed with row 18.
    # ``ensure_memory_dir`` is called at session bootstrap
    # so the directory exists before the agent's first
    # ``save_file`` into it.
    from ember_code.core.utils.context import memory_writeback_instructions

    instructions.append(memory_writeback_instructions(session.project_dir))

    # Guardrails
    guardrails = _session_core._create_guardrails(session.settings)

    # Compression — triggers at 80% of context window
    compression = _session_core.CompressionManager(
        model=model,
        compress_tool_results=True,
        compress_token_limit=int(context_window * 0.8),
    )

    agent = _session_core.Agent(
        name="ember",
        model=model,
        tools=tools,
        instructions=instructions,
        markdown=True,
        # Retry transient model-API failures (timeouts, 5xx) before
        # bubbling the error up to the user. Same default as the
        # specialist pool — see ``pool.build_agent``.
        retries=getattr(session.settings.models, "retries", 2),
        # Session persistence
        db=session.db,
        session_id=session.session_id,
        user_id=session.user_id,
        # History — keep all turns until 80% compaction triggers
        add_history_to_context=True,
        num_history_runs=10000,
        # Memory — agentic memory removed; LearningMachine handles learning.
        # Existing memories still loaded into context.
        enable_agentic_memory=False,
        add_memories_to_context=session.settings.memory.add_memories_to_context,
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
        learning=session._learning,
        add_learnings_to_context=True,
        # Tool event hooks
        tool_hooks=[tool_event_hook],
    )
    return agent

