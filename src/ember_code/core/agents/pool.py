"""Agent Pool — a thin cache over :class:`AgentEntry` s.

Two-phase lifecycle:

1. :meth:`AgentPool.load_definitions` — parse .md files, resolve
   priorities. Delegates to :class:`AgentDefinitionLoader`.
2. :meth:`AgentPool.build_agents` — construct :class:`Agent`
   objects. Delegates to :class:`AgentBuilder`.

Agents are built lazily on first :meth:`get`, so startup only
pays the cost of parsing ``.md`` files (~50ms), not importing
LLM provider modules (~350ms). Ephemeral CRUD lives on
:class:`EphemeralAgentStore` and is reached via :attr:`ephemeral`.

Backward-compat shim surface (``_definitions`` / ``_codeindex_available``
/ ``_ephemeral_count`` / ``_max_ephemeral`` / ``_ephemeral_dir`` /
``_load_directory``) is quarantined in :mod:`pool_legacy` — see that
module for the follow-up-PR deletion target.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from ember_code.core.agents.builder import AgentBuilder
from ember_code.core.agents.ephemeral import EphemeralAgentStore
from ember_code.core.agents.loader import AgentDefinitionLoader
from ember_code.core.agents.plugin_policy import PluginRestrictionPolicy
from ember_code.core.agents.pool_legacy import LegacyAgentPoolMixin
from ember_code.core.agents.schemas import (
    AgentBuildContext,
    AgentDefinition,
    AgentEntry,
    AgentPriority,
    Broadcast,
    DbHandle,
    KnowledgeManager,
    LoadReport,
    McpClient,
)
from ember_code.core.config.settings import Settings
from ember_code.core.tools.orchestrate_budget import SpawnBudget

if TYPE_CHECKING:
    from agno.agent import Agent


class AgentPool(LegacyAgentPoolMixin):
    """Cache + orchestrator for the agent definitions and their
    built :class:`Agent` instances.

    Kept intentionally lean — ~7 instance fields, ~14 public
    methods. Discovery, build, and ephemeral CRUD are delegated
    to the three collaborators (:class:`AgentDefinitionLoader`,
    :class:`AgentBuilder`, :class:`EphemeralAgentStore`). Legacy
    reach-in attribute shims live on :class:`LegacyAgentPoolMixin`
    in :mod:`pool_legacy` — this class knows nothing about them.
    """

    def __init__(
        self,
        db: DbHandle | None = None,
        broadcast: Broadcast | None = None,
    ) -> None:
        self._entries: dict[str, AgentEntry] = {}
        self._agents: dict[str, Agent] = {}
        self._builder: AgentBuilder | None = None
        self._ephemeral: EphemeralAgentStore | None = None
        # Stashed so we can construct the initial AgentBuildContext
        # when ``load_definitions`` runs. ``AgentPool()`` calls
        # deliberately allow these to be None (test scaffolding).
        self._db: DbHandle | None = db
        self._broadcast: Broadcast | None = broadcast
        # Settings pointer stashed for ``build_agents``'s lazy
        # re-context. Populated by ``load_definitions``.
        self._settings: Settings | None = None
        self._base_dir: str | None = None
        # Codeindex-availability captured on the last ``load_*``
        # call — used by :meth:`load_plugin_directory` so plugins
        # pick the same prompt variant as the base load. The legacy
        # ``_codeindex_available`` property on the mixin shadows
        # this by design (data-descriptor precedence) — read/write
        # the raw flag via the mixin property from *within* the
        # class too, so there's only one storage slot.
        self._codeindex_available_flag: bool = False
        # Pending knowledge manager set before ``build_agents``
        # first runs. Declared here so no ``getattr`` hidden-field.
        self._pending_knowledge_mgr: KnowledgeManager | None = None
        # Per-session sub-agent budget cache — lazily constructed
        # on first :meth:`spawn_budget` call so every
        # :class:`OrchestrateTools` for the same session shares one
        # counter.
        self._spawn_budgets: dict[str, SpawnBudget] = {}

    def _ensure_initialised(self) -> None:
        """Idempotent guard: install the ``__init__`` fields if the
        pool was created via ``AgentPool.__new__`` (bypassing
        ``__init__``).

        The legacy view uses this so
        ``tests/test_plugin_agent_restrictions.py`` can build a
        pool via ``__new__`` and immediately assign
        ``pool._definitions = {}``.
        """
        if getattr(self, "_entries", None) is not None:
            return
        object.__setattr__(self, "_entries", {})
        object.__setattr__(self, "_agents", {})
        object.__setattr__(self, "_builder", None)
        object.__setattr__(self, "_ephemeral", None)
        object.__setattr__(self, "_db", None)
        object.__setattr__(self, "_broadcast", None)
        object.__setattr__(self, "_settings", None)
        object.__setattr__(self, "_base_dir", None)
        object.__setattr__(self, "_codeindex_available_flag", False)
        object.__setattr__(self, "_pending_knowledge_mgr", None)
        object.__setattr__(self, "_spawn_budgets", {})

    # ── Introspection helpers used by the collaborators ─────────

    def iter_entries(self) -> Iterable[AgentEntry]:
        """Iterate the current :class:`AgentEntry` values."""
        return self._entries.values()

    def iter_entry_items(self) -> Iterable[tuple[str, AgentEntry]]:
        """Iterate ``(name, entry)`` pairs.

        Public helper used by :class:`_LegacyDefinitionsView` (via
        the mixin) so the legacy adapter never reaches into
        :attr:`_entries` directly. Order matches insertion order.
        """
        return list(self._entries.items())

    def has_definition(self, name: str) -> bool:
        return name in self._entries

    def get_entry(self, name: str) -> AgentEntry:
        entry = self._entries.get(name)
        if entry is None:
            available = ", ".join(sorted(self._entries.keys()))
            raise KeyError(f"Agent '{name}' not found. Available: {available}")
        return entry

    def upsert_entry(self, entry: AgentEntry) -> None:
        """Insert ``entry`` if new, or overwrite the existing
        same-name entry regardless of priority.

        Used by :class:`EphemeralAgentStore` to (re)register
        ephemerals and to re-tag them after a ``promote()``.
        """
        self._entries[entry.definition.name] = entry
        # Invalidate the cached Agent so the next ``get()`` rebuilds
        # with the new definition.
        self._agents.pop(entry.definition.name, None)

    def remove(self, name: str) -> None:
        """Drop an entry (and its cached agent) from the pool."""
        self._entries.pop(name, None)
        self._agents.pop(name, None)

    def clear_entries(self) -> None:
        """Public entry-clear used by :class:`_LegacyDefinitionsView`.

        Prefer :meth:`clear_definitions` for internal callers — it
        respects the ephemeral-preservation flag. This method is
        the flat "drop everything" equivalent kept intentionally
        minimal so the legacy view has a public route (Rule 6).
        """
        self._entries.clear()
        self._agents.clear()

    def replace_entries_from(
        self,
        mapping: dict[str, tuple[AgentDefinition, int | AgentPriority] | AgentEntry],
    ) -> None:
        """Clear all entries and repopulate from ``mapping``.

        Accepts either ``(definition, priority)`` tuples or
        :class:`AgentEntry` values so legacy test paths that spell
        ``pool._definitions = {name: (defn, prio)}`` route through
        one public method rather than touching the entry store
        directly. Coercion lives on :meth:`AgentEntry.from_legacy_pair`.
        """
        self._entries.clear()
        self._agents.clear()
        for name, value in mapping.items():
            self._entries[name] = AgentEntry.from_legacy_pair(value)

    def load_ephemeral_directory(self, path: Path) -> None:
        """Merge a rehydrated ephemeral dir at
        :attr:`AgentPriority.EPHEMERAL`. Called by
        :class:`EphemeralAgentStore.init`."""
        loader = self._make_base_loader(codeindex_available=False)
        report = loader.load_directory(path, AgentPriority.EPHEMERAL)
        self._merge(report)

    # ── Phase 1: load ────────────────────────────────────────────

    def clear_definitions(self, *, preserve_ephemeral: bool = True) -> None:
        """Drop cached entries so the next :meth:`load_definitions`
        actually re-picks prompt variants.

        ``preserve_ephemeral=True`` (default) keeps runtime-
        created agents — they have no ``.md`` on disk to reload
        from, so wiping them would delete the user's work.
        """
        if preserve_ephemeral:
            self._entries = {
                name: entry
                for name, entry in self._entries.items()
                if entry.priority == AgentPriority.EPHEMERAL
            }
        else:
            self._entries = {}
        self._agents.clear()

    def load_definitions(
        self,
        settings: Settings,
        project_dir: Path | None = None,
        codeindex_available: bool = False,
    ) -> LoadReport:
        """Parse all agent ``.md`` files and resolve priorities.

        No :class:`Agent` objects are created — just data. Returns
        the :class:`LoadReport` so callers can surface parse
        errors (``report.errors``) to the FE / audit log."""
        if project_dir is None:
            project_dir = Path.cwd()

        self._settings = settings
        self._base_dir = str(project_dir)
        self._codeindex_available_flag = bool(codeindex_available)

        loader = AgentDefinitionLoader(
            settings=settings,
            project_dir=project_dir,
            codeindex_available=codeindex_available,
        )
        report = loader.load()
        self._merge(report)
        return report

    def load_directory(
        self,
        path: Path,
        priority: AgentPriority | int,
        settings: Settings,
        base_dir: str | None = None,
    ) -> LoadReport:
        """Load agents from a single directory. Test-focused
        convenience — sets up the build context enough that a
        subsequent ``get(...)`` works."""
        self._settings = settings
        self._base_dir = base_dir or str(path.parent)
        loader = self._make_base_loader(codeindex_available=False)
        report = loader.load_directory(path, priority)
        self._merge(report)
        # Ensure the builder exists so lazy ``get()`` works.
        self._ensure_builder()
        return report

    def load_plugin_directory(
        self,
        path: Path,
        priority: AgentPriority | int,
        namespace: str,
        restriction_policy: PluginRestrictionPolicy | None = None,
    ) -> LoadReport:
        """Public entry-point for :class:`PluginLoader` — replaces
        the old private-method reach-in ``pool._load_directory``.

        Applies the plugin security envelope (via
        :class:`PluginRestrictionPolicy`) and namespaces every
        loaded agent as ``<namespace>:<name>``."""
        policy = restriction_policy or PluginRestrictionPolicy.strict()
        loader = AgentDefinitionLoader(
            settings=self._settings_or_bare(),
            project_dir=Path(self._base_dir) if self._base_dir else Path.cwd(),
            codeindex_available=self._codeindex_available_flag,
            restriction_policy=policy,
        )
        report = loader.load_directory(path, priority, namespace=namespace)
        self._merge(report)
        return report

    # ── Phase 2: build ───────────────────────────────────────────

    def build_agents(self, mcp_clients: dict[str, McpClient] | None = None) -> None:
        """Construct :class:`Agent` objects lazily from all loaded
        definitions.

        Stores ``mcp_clients`` in the build context so the next
        :meth:`get` rebuilds against the current MCP set.
        """
        assert self._settings is not None, "Call load_definitions() first"
        context = AgentBuildContext(
            settings=self._settings,
            base_dir=self._base_dir,
            mcp_clients=mcp_clients,
            knowledge_mgr=self._pending_knowledge_mgr,
            db=self._db,
            broadcast=self._broadcast,
        )
        if self._builder is None:
            self._builder = AgentBuilder(context)
        else:
            self._builder.replace_context(context)
        self._agents.clear()

    def attach_knowledge_manager(self, knowledge_mgr: KnowledgeManager | None) -> None:
        """Public setter so :class:`Session` no longer reaches for
        ``pool._knowledge_mgr`` directly.

        If the builder is already constructed, the context is
        rebuilt so sub-agents built next see the manager."""
        self._pending_knowledge_mgr = knowledge_mgr
        if self._builder is not None:
            new_context = self._builder.context.model_copy(update={"knowledge_mgr": knowledge_mgr})
            self._builder.replace_context(new_context)
            # Invalidate the built agent cache — the next ``get()``
            # rebuilds with the knowledge manager attached.
            self._agents.clear()

    def load_all(
        self,
        settings: Settings,
        project_dir: Path | None = None,
        mcp_clients: dict[str, McpClient] | None = None,
    ) -> None:
        """Shorthand — parse definitions AND construct build
        context in one step."""
        self.load_definitions(settings, project_dir)
        self.build_agents(mcp_clients=mcp_clients)

    # ── Access ────────────────────────────────────────────────────

    def get(self, name: str) -> Agent:
        """Get an agent by name, building lazily if needed."""
        if name not in self._agents:
            if name not in self._entries:
                available = ", ".join(sorted(self._entries.keys()))
                raise KeyError(f"Agent not found: '{name}'. Available: {available}")
            builder = self._ensure_builder()
            self._agents[name] = builder.build(self._entries[name].definition)
        return self._agents[name]

    def get_definition(self, name: str) -> AgentDefinition:
        return self.get_entry(name).definition

    def list_agents(self) -> list[AgentDefinition]:
        return [entry.definition for entry in self._entries.values()]

    def describe(self) -> str:
        """Summary line per agent for the Orchestrator prompt."""
        lines: list[str] = []
        for entry in self._entries.values():
            defn = entry.definition
            tools_str = ", ".join(defn.tools) if defn.tools else "none"
            tags_str = ", ".join(defn.tags) if defn.tags else "none"
            lines.append(
                f"- **{defn.name}**: {defn.description} [tools: {tools_str}] [tags: {tags_str}]"
            )
        return "\n".join(lines)

    def get_member_agents(self) -> list[Agent]:
        """Every agent as a list (team-member composition)."""
        return [self.get(name) for name in sorted(self._entries.keys())]

    @property
    def agent_names(self) -> list[str]:
        return sorted(self._entries.keys())

    # ── Public db accessor + spawn-budget cache ─────────────────
    #
    # ``db`` replaces the ``getattr(pool, "_db", None)`` reach-in
    # from :class:`OrchestrateTools._build_sub_team` (audit AP7).
    # ``spawn_budget`` owns the per-session sub-agent counter that
    # used to sit in module globals in ``orchestrate.py``.

    @property
    def db(self) -> DbHandle | None:
        """Public read of the Agno DB handle threaded through the
        pool. Used by :class:`SpawnRunner` when it builds sub-teams
        so paused runs land in the same store as the parent."""
        return self._db

    def spawn_budget(self, session_id: str) -> SpawnBudget:
        """Return the :class:`SpawnBudget` for ``session_id``.

        Lazy per-session — the first call constructs the budget,
        subsequent calls return the same instance so every
        :class:`OrchestrateTools` for that session shares one
        counter. ``max_agents`` is drawn from
        ``self._settings.orchestration.max_total_agents`` when
        available; falls back to a large sentinel when settings
        haven't been loaded.
        """
        budget = self._spawn_budgets.get(session_id)
        if budget is None:
            max_agents = (
                int(self._settings.orchestration.max_total_agents)
                if self._settings is not None
                else 10_000
            )
            budget = SpawnBudget(max_agents)
            self._spawn_budgets[session_id] = budget
        return budget

    def forget_spawn_budget(self, session_id: str) -> None:
        """Drop the per-session budget on session teardown."""
        self._spawn_budgets.pop(session_id, None)

    # ── Ephemeral facade ─────────────────────────────────────────

    @property
    def ephemeral(self) -> EphemeralAgentStore:
        """Access the ephemeral store. Raises if not initialised."""
        if self._ephemeral is None:
            raise RuntimeError("Ephemeral agents not initialized. Call init_ephemeral() first.")
        return self._ephemeral

    def init_ephemeral(self, project_dir: Path, max_ephemeral: int = 5) -> None:
        """Set up the ephemeral store + rehydrate leftovers."""
        self._ephemeral = EphemeralAgentStore(
            project_dir=project_dir,
            pool=self,
            max_ephemeral=max_ephemeral,
        )
        self._ephemeral.init()

    def register_ephemeral(
        self,
        name: str,
        description: str,
        system_prompt: str,
        tools: list[str] | None = None,
        model: str | None = None,
    ) -> Agent:
        return self.ephemeral.register(
            name=name,
            description=description,
            system_prompt=system_prompt,
            tools=tools,
            model=model,
        )

    def list_ephemeral(self) -> list[AgentDefinition]:
        if self._ephemeral is None:
            return []
        return self._ephemeral.list_agents()

    def promote_ephemeral(self, name: str, project_dir: Path) -> Path:
        return self.ephemeral.promote(name, project_dir)

    def discard_ephemeral(self, name: str) -> None:
        self.ephemeral.discard(name)

    def cleanup_ephemeral(self) -> int:
        if self._ephemeral is None:
            return 0
        return self._ephemeral.cleanup()

    def cleanup_ephemeral_if_auto(self, settings: Settings) -> int:
        """Cleanup ephemeral agents iff auto-cleanup is enabled.

        Encapsulates the ``settings.orchestration.auto_cleanup``
        gate so callers (shutdown pipeline, interactive loop)
        stop reaching into the settings object themselves.
        Returns the number of ephemeral agents removed; ``0``
        when the gate is off or the ephemeral store is not
        initialised.
        """
        if not settings.orchestration.auto_cleanup:
            return 0
        return self.cleanup_ephemeral()

    def is_ephemeral(self, defn: AgentDefinition) -> bool:
        """Public predicate — is this definition an ephemeral agent?

        Replaces the ``pool._ephemeral_dir`` reach-in previously
        used by the panel snapshot. Returns ``False`` when the
        ephemeral store isn't initialised, when the definition has
        no source file, or when its file doesn't live inside the
        ephemeral directory.
        """
        ephemeral_dir = self._ephemeral.directory if self._ephemeral else None
        return bool(
            ephemeral_dir and defn.source_path and ephemeral_dir in defn.source_path.parents
        )

    # ── Internals ────────────────────────────────────────────────

    def _merge(self, report: LoadReport) -> None:
        """Merge a :class:`LoadReport` into ``_entries`` honouring
        priority."""
        for name, entry in report.entries.items():
            existing = self._entries.get(name)
            if existing is None or entry.priority > existing.priority:
                self._entries[name] = entry

    def _make_base_loader(self, *, codeindex_available: bool) -> AgentDefinitionLoader:
        """Construct a bare-bones loader for single-directory
        loads (tests, ephemeral rehydrate)."""
        return AgentDefinitionLoader(
            settings=self._settings_or_bare(),
            project_dir=Path(self._base_dir) if self._base_dir else Path.cwd(),
            codeindex_available=codeindex_available,
        )

    def _ensure_builder(self) -> AgentBuilder:
        """Return the builder, constructing a bare one from
        stashed settings if ``build_agents`` was never called."""
        if self._builder is not None:
            return self._builder
        settings = self._settings_or_bare()
        context = AgentBuildContext(
            settings=settings,
            base_dir=self._base_dir,
            mcp_clients=None,
            knowledge_mgr=self._pending_knowledge_mgr,
            db=self._db,
            broadcast=self._broadcast,
        )
        self._builder = AgentBuilder(context)
        return self._builder

    def _settings_or_bare(self) -> Settings:
        """Return the stashed :class:`Settings` or a fresh default.

        The default is used only by the plugin-load path when a
        pool has never had ``load_definitions`` called on it (rare
        — the plugin loader runs after the base load in prod). A
        settings-less path still works because the plugin loader
        only reads ``settings.agents.cross_tool_support`` — not
        touched by ``load_directory``.
        """
        if self._settings is not None:
            return self._settings
        return Settings()


__all__ = ["AgentPool"]
