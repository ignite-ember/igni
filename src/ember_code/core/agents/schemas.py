"""Pydantic models + typed protocols for the agents package.

Splits five concerns off of the old ``core/pool.py``:

* :class:`AgentPriority` — resolution priorities as an :class:`IntEnum`
  (was a class-with-integer-constants). ``IntEnum`` composes with
  ``int`` comparisons so external test code passing raw integers
  still works.
* :class:`AgentDefinition` — parsed frontmatter + body from a
  ``.md`` file. Now carries two methods so the plugin-namespace
  prefix and plugin-restriction envelope live where the data does
  (Rule 1: methods on the model, not free functions taking the
  model as first arg).
* :class:`AgentInfo` — wire format for the agents panel.
* :class:`AgentEntry` — the (definition, priority) tuple replaced
  by a typed Pydantic pair. Every unpack site in the package used
  to spell ``defn, prio = entry`` — now they use ``entry.definition``
  / ``entry.priority``.
* :class:`AgentConstructorArgs` — replaces the ``kwargs: dict[str, Any]``
  splat in the old ``build_agent`` free function. A single method
  :meth:`AgentConstructorArgs.to_agno_kwargs` bridges to Agno's
  keyword surface.
* :class:`LoadReport` + :class:`LoadError` — typed Result over the
  old "print to stderr and swallow" pattern.
* :class:`AgentBuildContext` — the six shared-state params
  (settings, base_dir, mcp_clients, knowledge_mgr, db, broadcast)
  bundled once so :class:`AgentBuilder` takes one construction
  param instead of six.
* Typed :class:`typing.Protocol` s — replaces ``Any`` on the public
  API surface (audit AP5). Keeps runtime imports cheap (the real
  Agno / KnowledgeManager / etc types stay behind
  ``TYPE_CHECKING``).
"""

from __future__ import annotations

from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    pass


class AgentPriority(IntEnum):
    """Resolution priorities — highest wins on same-name collision.

    Within the same scope, native Ember sources beat cross-tool
    Claude sources by +1::

        10  ephemeral agents created at runtime
         4  <project>/.ember/agents/          (project, native)
         3  <project>/.ember/agents.local/    (project personal)
         2  <project>/.claude/agents/         (project, cross-tool)
         1  ~/.ember/agents/                  (user, native)
         0  ~/.claude/agents/                 (user, cross-tool)
    """

    USER_CLAUDE = 0
    USER_EMBER = 1
    PROJECT_CLAUDE = 2
    PROJECT_LOCAL = 3
    PROJECT_EMBER = 4
    EPHEMERAL = 10


# Frontmatter keys that plugin-shipped agents are NOT allowed to
# declare — they'd let a plugin escalate its own privileges. CC
# parity row 37. Kept as a module constant only so it can be
# re-exported by name from the package __init__; the canonical
# owner is :class:`PluginRestrictionPolicy.RESTRICTED_KEYS`.
_PLUGIN_RESTRICTED_FRONTMATTER_KEYS: frozenset[str] = frozenset(
    {
        "hooks",
        "mcpServers",
        "mcp_servers",  # snake_case alias — ember already parses it
        "permissionMode",
        "permission_mode",
        "permissions",
    }
)


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
    # Per-agent output token cap. ``None`` = use provider default.
    # Overriding rescues agents whose whole reply IS the payload
    # (e.g. visualizer) from mid-stream truncation.
    max_tokens: int | None = None
    system_prompt: str = ""
    source_path: Path | None = None
    # ``"worktree"`` on plugin-shipped agents so their spawns run
    # in a fresh worktree; ``None`` for user / project agents. The
    # empty string is accepted for forward-compat with legacy
    # frontmatter but treated as ``None`` semantically (the coercing
    # validator below normalises it).
    force_isolation: Literal["", "worktree"] | None = None

    @field_validator("force_isolation", mode="before")
    @classmethod
    def _coerce_force_isolation(cls, v: Any) -> Any:
        """Coerce loose legacy inputs to the ``Literal`` set.

        The audit tightened this field from ``str | None`` to a
        two-value ``Literal`` (audit AP6). Legacy markdown
        frontmatter or serialized state may still carry an empty
        string, so we accept it and normalise to ``None`` — anything
        else falls through to Pydantic's strict rejection.
        """
        if v is None or v == "":
            return None
        return v

    def namespaced(self, prefix: str) -> AgentDefinition:
        """Return a copy with ``name`` prefixed by ``<prefix>:``.

        Used by the plugin loader so each plugin's agents land
        under their own namespace and can't collide with same-
        named agents from other plugins or the user's own
        ``.ember/agents/``.
        """
        return self.model_copy(update={"name": f"{prefix}:{self.name}"})

    def with_plugin_restrictions(
        self,
        raw_keys: set[str],
        plugin_name: str = "",
    ) -> AgentDefinition:
        """Return a copy with the plugin security envelope applied.

        Strips ``mcp_servers`` and forces
        ``force_isolation="worktree"``. Restricted keys detected
        in ``raw_keys`` trigger a WARNING via the package logger
        so plugin authors see the policy violation and a security
        audit can spot escalation attempts.

        Delegates the warning + drop to
        :class:`PluginRestrictionPolicy` (defined in
        ``plugin_policy.py``) so the policy is unit-testable
        without a full model instance and the class-attribute
        constants (``RESTRICTED_KEYS``) live next to the code
        that reads them.
        """
        # Local import — the policy needs to import AgentDefinition
        # so we'd otherwise get an import cycle.
        from ember_code.core.agents.plugin_policy import PluginRestrictionPolicy

        return PluginRestrictionPolicy().apply(self, raw_keys, plugin_name)


class AgentInfo(BaseModel):
    """Wire format for one agent — emitted by
    :meth:`BackendServer.get_agent_details`, consumed by the
    agents panel.

    Sub-set of :class:`AgentDefinition` adapted for JSON transport:
    ``source_path`` is widened to ``str`` (Path doesn't serialize),
    and ``is_ephemeral`` is computed at the backend.
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


class AgentEntry(BaseModel):
    """One entry in the :class:`AgentPool` — a definition plus its
    resolution priority.

    Replaces the raw ``tuple[AgentDefinition, int]`` that used to
    live in ``AgentPool._definitions`` and got unpacked at ~8 call
    sites. Typed pair-container per Rule 1.
    """

    model_config = ConfigDict(frozen=False)

    definition: AgentDefinition
    priority: AgentPriority

    @classmethod
    def from_legacy_pair(cls, value: object) -> AgentEntry:
        """Coerce a legacy ``(definition, priority)`` tuple or an
        existing :class:`AgentEntry` into a canonical entry.

        Used by the legacy ``pool._definitions`` shim to normalise
        the two shapes test code still writes. Kept as a classmethod
        on the model (Rule 1) rather than a free helper so the
        coercion lives next to the data it produces.
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, tuple):
            defn, prio = value
            typed_prio = prio if isinstance(prio, AgentPriority) else AgentPriority(int(prio))
            return cls(definition=defn, priority=typed_prio)
        raise TypeError(
            "Expected an AgentEntry or (AgentDefinition, priority) tuple; "
            f"got {type(value).__name__}"
        )


class LoadError(BaseModel):
    """One parse failure from a directory-load pass.

    Kept as a typed record (not a free-form string) so the caller
    can act on the source path without regex-scraping a message.
    """

    path: Path
    reason: str


class LoadReport(BaseModel):
    """Result of :meth:`AgentDefinitionLoader.load` — accepted
    entries plus per-file parse errors.

    Replaces the previous ``print(..., file=sys.stderr)`` swallow —
    callers can now surface errors to the FE, decide whether to
    fail the boot, or route them to the audit log.
    """

    entries: dict[str, AgentEntry] = Field(default_factory=dict)
    errors: list[LoadError] = Field(default_factory=list)

    def merge(self, other: LoadReport) -> None:
        """Merge ``other`` into this report, honouring priority for
        collisions on ``entries`` and appending errors."""
        for name, entry in other.entries.items():
            existing = self.entries.get(name)
            if existing is None or entry.priority > existing.priority:
                self.entries[name] = entry
        self.errors.extend(other.errors)


# ── Protocols for external collaborators (audit AP5) ─────────────
#
# The real classes live in packages we don't want to eagerly import
# here (they pull in Agno / chromadb / SQLite bindings; a bare
# ``AgentPool()`` for a test shouldn't pay those costs). Structural
# typing via Protocol keeps callers grep-able without the import
# graph tax.


@runtime_checkable
class Broadcast(Protocol):
    """Callable that dispatches ``(channel, payload)`` events to
    attached FE clients. In headless / tests this is ``None``."""

    def __call__(self, channel: str, payload: dict) -> None: ...


@runtime_checkable
class KnowledgeManager(Protocol):
    """The session-level knowledge manager wrapper. Only used to
    dispatch through to :class:`KnowledgeTools`; we don't call its
    methods directly here."""

    ...


@runtime_checkable
class DbHandle(Protocol):
    """Opaque Agno ``AsyncBaseDb`` handle. Threaded through so
    paused sub-agent runs land in the same store as the parent."""

    ...


@runtime_checkable
class McpClient(Protocol):
    """One MCP client — placed in the ``tools=[...]`` list of an
    agent to expose its remote tools locally."""

    ...


class AgentConstructorArgs(BaseModel):
    """Typed bundle of the kwargs :class:`AgentBuilder` hands to
    :class:`agno.agent.Agent`.

    Replaces the ``kwargs: dict[str, Any]`` splat that used to
    hand-mutate an Agent construction dict (Rule 1). Nothing here
    knows about Agno's ``**kwargs`` — the builder converts through
    :meth:`to_agno_kwargs` at the boundary.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    model: Any
    description: str
    instructions: list[str] | None
    tools: list[Any] | None
    markdown: bool = True
    num_history_runs: int
    retries: int
    db: Any | None = None
    reasoning: bool = False
    reasoning_min_steps: int | None = None
    reasoning_max_steps: int | None = None

    def to_agno_kwargs(self) -> dict[str, Any]:
        """Emit the kwargs Agno actually expects.

        Drops the reasoning trio when ``reasoning=False`` and the
        ``db`` key when ``db is None`` — Agno differentiates
        between "missing key" (use its default) and "key with
        None" (explicit disable) for both.
        """
        kwargs: dict[str, Any] = {
            "name": self.name,
            "model": self.model,
            "description": self.description,
            "instructions": self.instructions,
            "tools": self.tools,
            "markdown": self.markdown,
            "num_history_runs": self.num_history_runs,
            "retries": self.retries,
        }
        if self.db is not None:
            kwargs["db"] = self.db
        if self.reasoning:
            kwargs["reasoning"] = True
            kwargs["reasoning_min_steps"] = self.reasoning_min_steps
            kwargs["reasoning_max_steps"] = self.reasoning_max_steps
        return kwargs


class AgentBuildContext(BaseModel):
    """Shared state :class:`AgentBuilder` needs to construct any
    agent from a definition.

    Composing these six pointers into one Pydantic bundle lets
    :class:`AgentPool` re-construct only the fields that change
    (e.g. ``mcp_clients`` after MCP connects) via
    ``context.model_copy(update={...})`` instead of touching
    private builder state (Rule 1 + audit AP5).

    ``settings`` is typed ``Any`` to avoid the Pydantic forward-
    ref rebuild dance (a real
    :class:`ember_code.core.config.settings.Settings` doesn't
    play nicely as a Pydantic field before ``model_rebuild`` and
    isn't worth importing eagerly here).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    settings: Any
    base_dir: str | None = None
    mcp_clients: dict[str, Any] | None = None
    knowledge_mgr: Any | None = None
    db: Any | None = None
    broadcast: Any | None = None


__all__ = [
    "AgentBuildContext",
    "AgentConstructorArgs",
    "AgentDefinition",
    "AgentEntry",
    "AgentInfo",
    "AgentPriority",
    "Broadcast",
    "DbHandle",
    "KnowledgeManager",
    "LoadError",
    "LoadReport",
    "McpClient",
    "_PLUGIN_RESTRICTED_FRONTMATTER_KEYS",
]
