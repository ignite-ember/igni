"""Pydantic models for the plugin system.

The manifest schema mirrors Claude Code's ``.claude-plugin/plugin.json``
so plugins published for Claude work here without changes. Unknown
fields are preserved (``extra="allow"``) so future Claude additions
don't break loading — they just go unused until we adopt them.

This module also owns every marketplace-related schema (previously
scattered across ``marketplaces.py``): keeping schemas colocated
with the panel-facing wire types (:class:`MarketplaceInfo`,
:class:`MarketplacePluginInfo`) is the codebase's established
schema-alignment convention. Operational surface — persistence,
catalog fetch, install-ref resolution — lives next door in
:mod:`marketplace_store`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PluginRoot = Literal[
    "user-claude",  # ~/.claude/plugins/
    "user-ember",  # ~/.ember/plugins/
    "project-claude",  # <project>/.claude/plugins/
    "project-ember",  # <project>/.ember/plugins/
    "managed-claude",  # sysadmin <managed>/.claude/plugins/
    "managed-ember",  # sysadmin <managed>/.ember/plugins/
]


class PluginManifest(BaseModel):
    """The ``.claude-plugin/plugin.json`` schema.

    Only ``name`` is required — version/description/author are
    metadata for the plugins panel. Extra fields are preserved so
    Claude Code's manifest evolution doesn't break loading.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    version: str | None = None
    description: str | None = None
    author: str | dict | None = None


class PluginSource(BaseModel):
    """Where a plugin was discovered.

    ``priority`` follows the four-root convention: project beats user,
    ember beats claude. Higher wins on same-name collisions across roots.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    root: PluginRoot
    path: Path
    priority: int


class PluginDefinition(BaseModel):
    """A discovered plugin: manifest + source + bundled-contents inventory.

    The ``has_*`` flags are set during scanning so the plugins panel
    can render counts without re-statting the filesystem, and so apply
    steps can skip plugins that bundle nothing in a given category.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    manifest: PluginManifest
    source: PluginSource
    has_skills: bool = False
    has_agents: bool = False
    has_hooks: bool = False
    has_mcp: bool = False
    has_tools: bool = False
    has_lsp: bool = False
    has_monitors: bool = False

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def is_managed(self) -> bool:
        """``True`` when the plugin was loaded from a managed
        (sysadmin) root. Managed plugins can't be disabled from
        the panel or via plugin state — they're enforced by the
        OS-protected source location."""
        return self.source.root in ("managed-claude", "managed-ember")

    @property
    def root_path(self) -> Path:
        return self.source.path


# ── Marketplace catalog schemas ────────────────────────────────────
#
# The wire-shaped Pydantic models for a marketplace catalog live
# here (not in :mod:`marketplace_store`) because Pydantic types
# are contract; ``marketplace_store`` owns behaviour. This is
# Pattern 7 (wire/domain schemas colocated) — the panel-facing
# :class:`MarketplaceInfo` / :class:`MarketplacePluginInfo` live
# in the same file for exactly the same reason.


class ResolvedSource(BaseModel):
    """Normalized plugin location after resolving a marketplace entry.

    The official ``marketplace.json`` schema lets ``source`` be one
    of three things (~25% / ~25% / ~50% of the catalog in practice):

    * **Bare URL string** — clone the whole repo, root is the plugin.
    * **``"./relative/path"`` string** — the plugin lives in a
      subdirectory of the *marketplace's own* git repo. We clone
      the marketplace URL and descend into the path.
    * **Object** with shape ``{"source": "url"|"git-subdir", ...}``.
      ``"url"`` is the bare-URL case in object form; ``"git-subdir"``
      is "clone ``url`` then descend into ``path``".

    All three normalize into the same downstream operation:
    *clone a URL, optionally descend into a subdir*. Everything
    that consumes a marketplace entry goes through
    :py:meth:`MarketplacePluginEntry.resolved_source` so the
    installer doesn't have to know the catalog's surface shape.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["url", "git-subdir", "relative"]
    url: str
    subdir: str | None = None  # None for "url" kind; relative path for subdir/relative
    ref: str | None = None  # branch / tag / sha (preferred), or None for default branch


class MarketplaceUrlSource(BaseModel):
    """Object form of a plugin ``source`` when it's ``{"source":"url", …}``.

    Preserves unknown keys (``extra='allow'``) so a future ``auth``
    or catalog-specific field survives round-trip.
    """

    model_config = ConfigDict(extra="allow")

    source: Literal["url"]
    url: str
    ref: str | None = None
    sha: str | None = None


class MarketplaceGitSubdirSource(BaseModel):
    """Object form of a plugin ``source`` when it's ``{"source":"git-subdir", …}``.

    ``path`` is the subdirectory of *url* where the plugin lives.
    """

    model_config = ConfigDict(extra="allow")

    source: Literal["git-subdir"]
    url: str
    path: str
    ref: str | None = None
    sha: str | None = None


class MarketplacePluginEntry(BaseModel):
    """One row in a marketplace's catalog.

    Mirrors Claude Code's ``marketplace.json#plugins[*]`` schema.
    ``source`` is a discriminated union over the three supported
    shapes; unknown object shapes fall through to a permissive
    ``dict`` so a novel catalog doesn't blow up validation — the
    normalization step in :meth:`resolved_source` handles the
    fallback by returning ``None`` and letting the caller decide.
    Extra fields are preserved for forward compatibility.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    # Order matters for Pydantic union parsing: try the typed object
    # shapes first (they're stricter and won't silently swallow a
    # bare URL); then bare string; then a dict fallback (novel
    # catalogs); finally ``None``.
    source: MarketplaceUrlSource | MarketplaceGitSubdirSource | str | dict[str, Any] | None = None
    description: str | None = None
    author: str | dict | None = None
    version: str | None = None
    branch: str | None = None

    def resolved_source(self, marketplace_url: str) -> ResolvedSource | None:
        """Normalize the raw ``source`` field into a clone-shaped spec.

        ``marketplace_url`` is required because relative-path
        entries (``"./plugins/x"``) need the marketplace's own
        git URL to be cloneable. Returns ``None`` when the source
        field is unparseable.
        """
        src = self.source
        if src is None:
            return None

        # Typed object forms — the discriminated-union parse produced
        # a concrete model instance. Handle them explicitly so field
        # access is typed, no dict.get() reaches remain.
        if isinstance(src, MarketplaceUrlSource):
            ref = src.sha or src.ref or self.branch
            return ResolvedSource(kind="url", url=src.url, subdir=None, ref=ref)
        if isinstance(src, MarketplaceGitSubdirSource):
            ref = src.sha or src.ref or self.branch
            return ResolvedSource(
                kind="git-subdir",
                url=src.url,
                subdir=src.path,
                ref=ref,
            )

        # Bare-string forms.
        if isinstance(src, str):
            if src.startswith("./") or src.startswith("../"):
                # Plugin lives inside the marketplace repo itself.
                return ResolvedSource(
                    kind="relative",
                    url=marketplace_url,
                    subdir=src.lstrip("./"),
                    ref=self.branch,
                )
            # Anything else is treated as a clonable URL — works
            # for ``https://...``, ``git@...``, ``file://...``.
            return ResolvedSource(
                kind="url",
                url=src,
                subdir=None,
                ref=self.branch,
            )

        # Novel object shape (dict fallback): be conservative —
        # extract a URL if present and treat the whole thing as a
        # plain clone. The installer will fail loudly if the
        # manifest isn't at the root.
        if isinstance(src, dict):
            url = src.get("url")
            if not url:
                return None
            ref = src.get("sha") or src.get("ref") or self.branch
            return ResolvedSource(kind="url", url=url, subdir=None, ref=ref)

        return None


class MarketplaceCatalog(BaseModel):
    """The ``.claude-plugin/marketplace.json`` shape."""

    model_config = ConfigDict(extra="allow")

    name: str
    description: str | None = None
    plugins: list[MarketplacePluginEntry] = Field(default_factory=list)


class MarketplaceEntry(BaseModel):
    """One registered marketplace in our local registry.

    Mutations that keep ``last_fetched`` in sync with ``cached``
    live as methods here so callers don't have to remember to
    stamp the timestamp by hand.
    """

    name: str
    url: str
    last_fetched: str | None = None  # ISO-8601 UTC; None until first fetch
    cached: MarketplaceCatalog | None = None

    def mark_fetched(self, catalog: MarketplaceCatalog) -> None:
        """Overwrite ``cached`` with a fresh catalog and stamp
        ``last_fetched`` to now-UTC (ISO-8601, seconds precision).
        Encapsulated so the timestamp shape has one owner."""
        self.cached = catalog
        self.last_fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def update_from_catalog(self, url: str, catalog: MarketplaceCatalog) -> None:
        """Re-add path: point the entry at a (possibly new) URL and
        replace its cached catalog + fetched timestamp."""
        self.url = url
        self.mark_fetched(catalog)


class MarketplaceRegistry(BaseModel):
    """The shape of ``~/.ember/marketplaces.json``.

    Stored as a single object with a ``marketplaces`` list — gives
    room to add registry-wide fields later (e.g. policy settings)
    without a breaking format change.
    """

    marketplaces: list[MarketplaceEntry] = Field(default_factory=list)

    def find(self, name: str) -> MarketplaceEntry | None:
        for m in self.marketplaces:
            if m.name == name:
                return m
        return None

    def add(self, entry: MarketplaceEntry) -> MarketplaceEntry:
        """Insert ``entry`` or update the existing row with the same
        name in place. Returns the effective entry (post-merge).

        Idempotency: re-adding a marketplace with the same name
        replaces its URL and cache — supports migration when a
        marketplace moves git host without renaming itself.
        """
        existing = self.find(entry.name)
        if existing is None:
            self.marketplaces.append(entry)
            return entry
        # In-place update: keep the same list slot, refresh URL +
        # catalog + timestamp so views holding a reference to
        # ``existing`` see the merge.
        existing.url = entry.url
        if entry.cached is not None:
            existing.mark_fetched(entry.cached)
        else:
            existing.last_fetched = entry.last_fetched
        return existing

    def remove(self, name: str) -> bool:
        """Drop the marketplace named ``name``. Returns ``True`` when
        something was removed. Idempotent — a second ``remove`` of
        the same name is a no-op returning ``False``."""
        before = len(self.marketplaces)
        self.marketplaces = [m for m in self.marketplaces if m.name != name]
        return len(self.marketplaces) != before

    def resolve_install_ref(
        self, install_ref: InstallRef
    ) -> tuple[ResolvedSource, MarketplacePluginEntry] | None:
        """Look up an :class:`InstallRef` (parsed
        ``@<marketplace>/<plugin>``) in the cached catalogs.

        Returns ``(ResolvedSource, MarketplacePluginEntry)`` on hit,
        ``None`` when the marketplace isn't registered, its catalog
        hasn't been cached yet, the plugin isn't in the catalog, or
        the entry's ``source`` field is unparseable.
        """
        entry = self.find(install_ref.marketplace)
        if entry is None or entry.cached is None:
            return None
        for plugin in entry.cached.plugins:
            if plugin.name == install_ref.plugin:
                resolved = plugin.resolved_source(entry.url)
                if resolved is None:
                    return None
                return resolved, plugin
        return None


class InstallRef(BaseModel):
    """Parsed ``@<marketplace>/<plugin>`` install ref.

    Kept as a value object so the parse logic has one home — the
    ``resolve_install_ref`` free-function of the old design conflated
    "parse the ref" with "look it up in a registry".
    """

    model_config = ConfigDict(frozen=True)

    marketplace: str
    plugin: str

    @classmethod
    def parse(cls, ref: str) -> InstallRef | None:
        """Split ``@<marketplace>/<plugin>`` into its parts.

        Returns ``None`` for anything that isn't shaped like a
        marketplace ref — callers should fall through to treating
        the raw string as a git URL.
        """
        if not ref.startswith("@") or "/" not in ref:
            return None
        marketplace_name, _, plugin_name = ref[1:].partition("/")
        if not marketplace_name or not plugin_name:
            return None
        return cls(marketplace=marketplace_name, plugin=plugin_name)


# ── Wire-format models for the panel ───────────────────────────────
#
# The panel receives plugin/marketplace data over RPC as a list of
# dicts. These models are the contract for that shape — used to
# construct the response on the backend and reconstruct the typed
# view on the frontend. Defining them in this shared module (rather
# than the widget) keeps the source of truth in one place: if a
# field is added on the backend, the widget side picks it up
# automatically (same model).
#
# ``source_root`` is widened to ``str`` here (vs. ``PluginRoot``
# Literal on :class:`PluginSource`) since literal narrowing
# doesn't survive the JSON round-trip and these models are
# display-only.


class PluginInfo(BaseModel):
    """Wire format for one installed plugin — emitted by
    :meth:`BackendServer.get_plugin_details`, consumed by the
    plugins panel."""

    name: str
    version: str = ""
    description: str = ""
    source_root: str = ""
    path: str = ""
    enabled: bool = True
    has_skills: bool = False
    has_agents: bool = False
    has_hooks: bool = False
    has_mcp: bool = False
    has_tools: bool = False
    has_lsp: bool = False
    has_monitors: bool = False
    # ``True`` for plugins installed at the managed (sysadmin)
    # tier — surfaced so the panel can lock the disable toggle.
    managed: bool = False
    pin: str = ""


class MarketplacePluginInfo(BaseModel):
    """Wire format for one plugin entry inside a marketplace catalog."""

    name: str
    source: str
    description: str = ""
    version: str = ""
    branch: str = ""

    @classmethod
    def from_catalog_entry(
        cls,
        entry: MarketplacePluginEntry,
        marketplace_url: str,
    ) -> MarketplacePluginInfo:
        """Project a raw ``MarketplacePluginEntry`` into the panel's
        display shape. Handles the three source shapes:

        * bare URL → just the URL
        * URL + subdir → ``"<url> [<subdir>]"``
        * unresolvable → ``str(raw source)`` as a fallback

        Living on the wire model itself keeps data + display
        projection colocated (the projection IS a property of "how
        this row is presented"), rather than tucked inside the
        controller."""
        resolved = entry.resolved_source(marketplace_url)
        if resolved is None:
            source_display = str(entry.source) if entry.source else ""
        elif resolved.subdir:
            source_display = f"{resolved.url} [{resolved.subdir}]"
        else:
            source_display = resolved.url
        return cls(
            name=entry.name,
            source=source_display,
            description=entry.description or "",
            version=entry.version or "",
            branch=entry.branch or "",
        )


class MarketplaceInfo(BaseModel):
    """Wire format for one registered marketplace — registry-level
    metadata plus the cached catalog at the time of the last fetch."""

    name: str
    url: str
    last_fetched: str = ""
    plugins: list[MarketplacePluginInfo] = []
