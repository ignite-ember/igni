"""Agent Pool — loads, parses, and manages agent definitions from .md files."""

import re
import sys
from pathlib import Path
from typing import Any

import yaml
from agno.agent import Agent
from pydantic import BaseModel, Field

from ember_code.core.config.models import ModelRegistry
from ember_code.core.config.settings import Settings
from ember_code.core.config.tool_permissions import ToolPermissions
from ember_code.core.tools.registry import ToolRegistry


class AgentDefinition(BaseModel):
    """Parsed agent definition from a .md file."""

    name: str
    description: str
    tools: list[str] = Field(default_factory=list)
    model: str | None = None
    color: str | None = None
    reasoning: bool = False
    reasoning_min_steps: int = 1
    reasoning_max_steps: int = 10
    tags: list[str] = Field(default_factory=list)
    can_orchestrate: bool = True
    mcp_servers: list[str] = Field(default_factory=list)
    max_turns: int | None = None
    temperature: float | None = None
    system_prompt: str = ""
    source_path: Path | None = None


class AgentInfo(BaseModel):
    """Wire format for one agent — emitted by
    :meth:`BackendServer.get_agent_details`, consumed by the
    agents panel.

    Sub-set of :class:`AgentDefinition` adapted for JSON transport:
    ``source_path`` is widened to ``str`` (Path doesn't serialize),
    and ``is_ephemeral`` is computed at the backend (cheaper there
    than reconstructing the directory match on every frontend
    render).
    """

    name: str
    description: str = ""
    tools: list[str] = Field(default_factory=list)
    model: str = ""
    color: str = ""
    can_orchestrate: bool = True
    mcp_servers: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    system_prompt: str = ""
    source_path: str = ""
    is_ephemeral: bool = False


# ── Parsing ──────────────────────────────────────────────────────────


def parse_agent_file(path: Path) -> AgentDefinition:
    """Parse a .md file with YAML frontmatter into an AgentDefinition."""
    content = path.read_text()

    frontmatter_match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", content, re.DOTALL)
    if not frontmatter_match:
        raise ValueError(f"No YAML frontmatter found in {path}")

    yaml_str = frontmatter_match.group(1)
    body = frontmatter_match.group(2).strip()
    fm = yaml.safe_load(yaml_str) or {}

    if "name" not in fm:
        raise ValueError(f"Agent definition missing 'name' in {path}")
    if "description" not in fm:
        raise ValueError(f"Agent definition missing 'description' in {path}")

    # Parse tools
    tools_raw = fm.get("tools", [])
    if isinstance(tools_raw, str):
        tools = [t.strip() for t in tools_raw.split(",") if t.strip()]
    elif isinstance(tools_raw, list):
        tools = tools_raw
    else:
        tools = []

    # Parse tags
    tags = fm.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    return AgentDefinition(
        name=fm["name"],
        description=fm["description"],
        tools=tools,
        model=fm.get("model"),
        color=fm.get("color"),
        reasoning=fm.get("reasoning", False),
        reasoning_min_steps=fm.get("reasoning_min_steps", 1),
        reasoning_max_steps=fm.get("reasoning_max_steps", 10),
        tags=tags,
        can_orchestrate=fm.get("can_orchestrate", True),
        mcp_servers=fm.get("mcp_servers", []) or [],
        max_turns=fm.get("max_turns"),
        temperature=fm.get("temperature"),
        system_prompt=body,
        source_path=path,
    )


# ── Building ─────────────────────────────────────────────────────────


def build_agent(
    definition: AgentDefinition,
    settings: Settings,
    base_dir: str | None = None,
    mcp_clients: dict[str, Any] | None = None,
    knowledge_mgr: Any | None = None,
    db: Any | None = None,
) -> Agent:
    """Build an Agno Agent from an AgentDefinition.

    This is the single place where an agent is constructed.  It gathers
    everything the agent needs — model, tools, MCP tools, prompts,
    reasoning config — and produces a ready-to-use ``Agent``.
    """
    # ── Model ──────────────────────────────────────────────────────
    BUILTIN_DEFAULT = "MiniMax-M2.7"
    agent_model = definition.model
    if not agent_model or agent_model == BUILTIN_DEFAULT:
        agent_model = settings.models.default
    model = ModelRegistry(settings).get_model(agent_model)

    if definition.temperature is not None:
        model.temperature = definition.temperature

    # ── Tools ──────────────────────────────────────────────────────
    tools: list[Any] = []
    if definition.tools:
        permissions = ToolPermissions(project_dir=Path(base_dir) if base_dir else None)
        registry = ToolRegistry(base_dir=base_dir, permissions=permissions)
        tools = registry.resolve(definition.tools)

    # ── Schedule tools (shared across all agents) ───────────────
    if tools:
        from ember_code.core.tools.schedule import ScheduleTools

        tools.append(ScheduleTools())

    # ── Knowledge tools (shared across all agents) ────────────────
    if tools and knowledge_mgr is not None:
        from ember_code.core.tools.knowledge import KnowledgeTools

        tools.append(KnowledgeTools(knowledge_mgr))

    # ── MCP tools (user-configured servers) ─────────────────────
    # If the agent specifies mcp_servers, only include those.
    # If mcp_servers is empty, include all MCP tools (backward-compatible).
    agent_mcp: dict[str, Any] = {}
    if tools and mcp_clients:
        if definition.mcp_servers:
            agent_mcp = {
                name: client
                for name, client in mcp_clients.items()
                if name in definition.mcp_servers
            }
        else:
            agent_mcp = mcp_clients

        for client in agent_mcp.values():
            if client not in tools:
                tools.append(client)

    # ── Instructions ────────────────────────────────────────────────
    instructions: list[str] = []
    if definition.system_prompt:
        instructions.append(definition.system_prompt)
    if base_dir:
        instructions.append(f"Working directory: {base_dir}")
    if agent_mcp:
        mcp_names = ", ".join(agent_mcp.keys())
        instructions.append(
            f"You have MCP tools from: {mcp_names}. "
            f"Project path: {base_dir}\n"
            f"If an MCP tool returns empty/no data, do NOT retry with different arguments. "
            f"Report what happened and ask the user."
        )

    # ── Construct ──────────────────────────────────────────────────
    kwargs: dict[str, Any] = {
        "name": definition.name,
        "model": model,
        "description": definition.description,
        "instructions": instructions if instructions else None,
        "tools": tools if tools else None,
        "markdown": True,
        "num_history_runs": settings.storage.max_history_runs,
        # Retry transient model-API failures rather than failing the
        # whole spawn. Hung connections still need the per-request
        # timeout in models.py to surface as an exception first;
        # ``retries`` only kicks in when the model call raises.
        "retries": getattr(settings.models, "retries", 2),
    }

    # Share the session's SQLite DB so HITL-paused runs can be resumed
    # via ``acontinue_run(run_id, session_id)``. Without a db Agno has
    # nowhere to look up the paused run and resume fails with
    # ``RuntimeError: No runs found for run ID …``.
    if db is not None:
        kwargs["db"] = db

    if definition.reasoning:
        kwargs["reasoning"] = True
        kwargs["reasoning_min_steps"] = definition.reasoning_min_steps
        kwargs["reasoning_max_steps"] = definition.reasoning_max_steps

    return Agent(**kwargs)


# ── Pool ─────────────────────────────────────────────────────────────


class AgentPool:
    """Manages the pool of available agents.

    Two-phase lifecycle:
      1. ``load_definitions()`` — parse .md files, resolve priorities
      2. ``build_agents()`` — construct Agent objects (lazy by default)

    Agents are built lazily on first access via ``get()``, so startup
    only pays the cost of parsing .md files (~50ms), not importing
    LLM provider modules (~350ms).  Call ``build_agents()`` explicitly
    to force eager construction (e.g. after MCP servers connect).
    """

    def __init__(self, db: Any | None = None):
        self._definitions: dict[str, tuple[AgentDefinition, int]] = {}
        self._agents: dict[str, Agent] = {}
        self._settings: Settings | None = None
        self._base_dir: str | None = None
        self._mcp_clients: dict[str, Any] | None = None
        self._knowledge_mgr: Any | None = None
        self._ephemeral_count: int = 0
        self._ephemeral_dir: Path | None = None
        self._max_ephemeral: int = 5
        # Shared with the main session so paused sub-agent runs are
        # persisted alongside the team's runs and Agno can find them on
        # ``acontinue_run``.
        self._db: Any | None = db

    # ── Phase 1: Load definitions ─────────────────────────────────

    def load_definitions(
        self,
        settings: Settings,
        project_dir: Path | None = None,
        codeindex_available: bool = False,
    ) -> None:
        """Parse all agent .md files and resolve priorities.

        No Agent objects are created — just AgentDefinition data.

        ``codeindex_available`` selects which variant of an agent's
        prompt to load when both ``<name>.md`` and
        ``<name>.codeindex.md`` exist on disk:

        - ``True``  → load the ``.codeindex.md`` variant (CodeIndex-first
                      prompt) and ignore the plain sibling.
        - ``False`` → load the plain ``.md`` and ignore the
                      ``.codeindex.md`` sibling (otherwise the agent
                      would be told to call a tool it doesn't have).
        """
        if project_dir is None:
            project_dir = Path.cwd()

        self._settings = settings
        self._base_dir = str(project_dir)
        self._codeindex_available = codeindex_available

        dirs = [
            (Path.home() / ".ember" / "agents", 1),
            (project_dir / ".ember" / "agents.local", 2),
            (project_dir / ".ember" / "agents", 3),
        ]

        if settings.agents.cross_tool_support:
            dirs.append((project_dir / ".claude" / "agents", 2))
            dirs.append((Path.home() / ".claude" / "agents", 1))

        for directory, priority in dirs:
            self._load_directory(directory, priority)

    def _load_directory(
        self,
        path: Path,
        priority: int,
        namespace: str | None = None,
    ) -> None:
        """Parse .md files from a directory, keeping highest-priority wins.

        Skips the wrong CodeIndex variant per
        ``self._codeindex_available``: when CodeIndex is unavailable we
        skip every ``*.codeindex.md`` file; when it's available we skip
        any plain ``*.md`` whose sibling ``*.codeindex.md`` is also
        present in this directory.

        ``namespace`` prefixes every loaded agent's ``name`` as
        ``<namespace>:<name>``. Used by the plugin loader so each
        plugin's agents land under their own namespace and can't
        collide with same-named agents from other plugins or the
        user's own ``.ember/agents/``.
        """
        if not path.exists():
            return

        use_codeindex = getattr(self, "_codeindex_available", False)
        all_files = sorted(path.glob("*.md"))
        codeindex_stems = {
            f.name[: -len(".codeindex.md")] for f in all_files if f.name.endswith(".codeindex.md")
        }

        for md_file in all_files:
            is_codeindex_variant = md_file.name.endswith(".codeindex.md")
            # Skip variants we don't want for this session.
            if is_codeindex_variant and not use_codeindex:
                continue
            if not is_codeindex_variant and use_codeindex and md_file.stem in codeindex_stems:
                # Plain variant has a .codeindex.md sibling in this
                # directory; the codeindex sibling wins.
                continue
            try:
                definition = parse_agent_file(md_file)
                if namespace:
                    definition = definition.model_copy(
                        update={"name": f"{namespace}:{definition.name}"}
                    )
                name = definition.name
                existing = self._definitions.get(name)

                if existing is None or priority > existing[1]:
                    self._definitions[name] = (definition, priority)
            except Exception as e:
                print(f"Warning: Failed to parse agent from {md_file}: {e}", file=sys.stderr)

    # ── Phase 2: Build agents ─────────────────────────────────────

    def build_agents(self, mcp_clients: dict[str, Any] | None = None) -> None:
        """Construct Agent objects from all loaded definitions.

        Call this after ``load_definitions()``.  Clears the agent cache
        and stores ``mcp_clients`` so agents are rebuilt with MCP tools
        on next access.  Agents are built lazily in ``get()``.
        """
        assert self._settings is not None, "Call load_definitions() first"
        self._mcp_clients = mcp_clients
        self._agents.clear()

    def _build_one(self, name: str) -> Agent:
        """Build a single agent on demand."""
        definition, _ = self._definitions[name]
        return build_agent(
            definition,
            self._settings,
            self._base_dir,
            mcp_clients=self._mcp_clients,
            knowledge_mgr=self._knowledge_mgr,
            db=self._db,
        )

    # ── Convenience: load + build in one call ─────────────────────

    def load_all(
        self,
        settings: Settings,
        project_dir: Path | None = None,
        mcp_clients: dict[str, Any] | None = None,
    ) -> None:
        """Parse definitions and build agents in one step.

        Shorthand for ``load_definitions()`` + ``build_agents()``.
        """
        self.load_definitions(settings, project_dir)
        self.build_agents(mcp_clients=mcp_clients)

    # ── Convenience: single directory load + build ──────────────────

    def load_directory(
        self,
        path: Path,
        priority: int,
        settings: Settings,
        base_dir: str | None = None,
    ) -> None:
        """Load and build agents from a single directory.

        Convenience method for tests and simple use cases.
        """
        self._settings = settings
        self._base_dir = base_dir or str(path.parent)
        self._load_directory(path, priority)
        self.build_agents()

    # ── Access ────────────────────────────────────────────────────

    def get(self, name: str) -> Agent:
        """Get an agent by name, building it lazily if needed."""
        if name not in self._agents:
            if name not in self._definitions:
                available = ", ".join(sorted(self._definitions.keys()))
                raise KeyError(f"Agent not found: '{name}'. Available: {available}")
            self._agents[name] = self._build_one(name)
        return self._agents[name]

    def get_definition(self, name: str) -> AgentDefinition:
        """Get an agent definition by name."""
        entry = self._definitions.get(name)
        if entry is None:
            available = ", ".join(sorted(self._definitions.keys()))
            raise KeyError(f"Agent definition not found: '{name}'. Available: {available}")
        return entry[0]

    def list_agents(self) -> list[AgentDefinition]:
        """List all agent definitions."""
        return [defn for defn, _pri in self._definitions.values()]

    def describe(self) -> str:
        """Generate a summary of all agents for the Orchestrator."""
        lines = []
        for defn, _pri in self._definitions.values():
            tools_str = ", ".join(defn.tools) if defn.tools else "none"
            tags_str = ", ".join(defn.tags) if defn.tags else "none"
            lines.append(
                f"- **{defn.name}**: {defn.description} [tools: {tools_str}] [tags: {tags_str}]"
            )
        return "\n".join(lines)

    def get_member_agents(self) -> list[Agent]:
        """Return all agents as a list (for use as team members)."""
        return [self.get(name) for name in sorted(self._definitions.keys())]

    @property
    def agent_names(self) -> list[str]:
        """Get sorted list of agent names."""
        return sorted(self._definitions.keys())

    # ── Ephemeral agents ─────────────────────────────────────────

    def init_ephemeral(self, project_dir: Path, max_ephemeral: int = 5) -> None:
        """Set up the ephemeral agent directory."""
        self._ephemeral_dir = project_dir / ".ember" / "agents.tmp"
        self._ephemeral_dir.mkdir(parents=True, exist_ok=True)
        self._max_ephemeral = max_ephemeral
        self._ephemeral_count = 0

    def register_ephemeral(
        self,
        name: str,
        description: str,
        system_prompt: str,
        tools: list[str] | None = None,
        model: str | None = None,
    ) -> Agent:
        """Create an ephemeral agent, write it to agents.tmp, and add to pool."""
        if self._ephemeral_dir is None:
            raise RuntimeError("Ephemeral agents not initialized. Call init_ephemeral() first.")
        if self._ephemeral_count >= self._max_ephemeral:
            raise ValueError(
                f"Ephemeral agent limit reached ({self._max_ephemeral}). "
                f"Promote or remove existing ephemeral agents first."
            )
        if name in self._definitions:
            raise ValueError(f"Agent '{name}' already exists in the pool.")

        tools = tools or ["Read", "Write", "Edit", "Bash", "Grep", "Glob"]

        # Map Agno function names to our registry names (the LLM sees
        # function names like read_file but our registry uses Read)
        _fn_to_registry = {
            "read_file": "Read",
            "read_file_chunk": "Read",
            "list_files": "Read",
            "save_file": "Write",
            "edit_file": "Edit",
            "edit_file_replace_all": "Edit",
            "create_file": "Edit",
            "run_shell_command": "Bash",
            "grep": "Grep",
            "grep_files": "Grep",
            "grep_count": "Grep",
            "glob_files": "Glob",
            "web_search": "WebSearch",
            "search_news": "WebSearch",
            "fetch_url": "WebFetch",
            "fetch_json": "WebFetch",
            "schedule_task": "Schedule",
            "list_scheduled_tasks": "Schedule",
            "cancel_scheduled_task": "Schedule",
            "notebook_read": "NotebookEdit",
            "notebook_read_cell": "NotebookEdit",
            "notebook_edit_cell": "NotebookEdit",
            "notebook_add_cell": "NotebookEdit",
            "notebook_remove_cell": "NotebookEdit",
        }
        tools = [_fn_to_registry.get(t, t) for t in tools]
        # Deduplicate while preserving order
        tools = list(dict.fromkeys(tools))

        # Validate
        valid_tools = {
            "Read",
            "Write",
            "Edit",
            "Bash",
            "BashOutput",
            "Grep",
            "Glob",
            "LS",
            "WebSearch",
            "WebFetch",
            "Python",
            "Schedule",
            "NotebookEdit",
        }
        invalid = [t for t in tools if t not in valid_tools and not t.startswith("MCP:")]
        if invalid:
            raise ValueError(
                f"Unknown tool(s): {', '.join(invalid)}. "
                f"Available: {', '.join(sorted(valid_tools))}"
            )

        # Write .md file
        tools_str = ", ".join(tools)
        md_content = f"---\nname: {name}\ndescription: {description}\ntools: {tools_str}\n"
        if model:
            md_content += f"model: {model}\n"
        md_content += f"---\n{system_prompt}\n"

        md_path = self._ephemeral_dir / f"{name}.md"
        md_path.write_text(md_content)

        # Parse and register
        definition = parse_agent_file(md_path)
        self._definitions[name] = (definition, 10)  # highest priority
        self._agents.pop(name, None)  # clear cache so it rebuilds
        self._ephemeral_count += 1

        return self.get(name)

    def list_ephemeral(self) -> list[AgentDefinition]:
        """List all ephemeral agent definitions."""
        if self._ephemeral_dir is None:
            return []
        return [
            defn
            for defn, _ in self._definitions.values()
            if defn.source_path and self._ephemeral_dir in defn.source_path.parents
        ]

    def promote_ephemeral(self, name: str, project_dir: Path) -> Path:
        """Move an ephemeral agent to the permanent agents directory."""
        if self._ephemeral_dir is None:
            raise RuntimeError("Ephemeral agents not initialized.")

        entry = self._definitions.get(name)
        if entry is None:
            raise KeyError(f"Agent '{name}' not found.")

        defn = entry[0]
        if not defn.source_path or self._ephemeral_dir not in defn.source_path.parents:
            raise ValueError(f"Agent '{name}' is not an ephemeral agent.")

        dest_dir = project_dir / ".ember" / "agents"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / defn.source_path.name

        import shutil

        shutil.move(str(defn.source_path), str(dest_path))

        # Update definition source path and priority
        defn.source_path = dest_path
        self._definitions[name] = (defn, 3)  # project-level priority
        self._ephemeral_count = max(0, self._ephemeral_count - 1)

        return dest_path

    def discard_ephemeral(self, name: str) -> None:
        """Delete an ephemeral agent from disk and remove from pool."""
        if self._ephemeral_dir is None:
            raise RuntimeError("Ephemeral agents not initialized.")

        entry = self._definitions.get(name)
        if entry is None:
            raise KeyError(f"Agent '{name}' not found.")

        defn = entry[0]
        if not defn.source_path or self._ephemeral_dir not in defn.source_path.parents:
            raise ValueError(f"Agent '{name}' is not an ephemeral agent.")

        if defn.source_path.exists():
            defn.source_path.unlink()

        del self._definitions[name]
        self._ephemeral_count = max(0, self._ephemeral_count - 1)

    def cleanup_ephemeral(self) -> int:
        """Delete all ephemeral agents from disk and pool. Returns count removed."""
        if self._ephemeral_dir is None:
            return 0
        ephemeral = self.list_ephemeral()
        for defn in ephemeral:
            if defn.source_path and defn.source_path.exists():
                defn.source_path.unlink()
            self._definitions.pop(defn.name, None)
        self._ephemeral_count = 0
        return len(ephemeral)
