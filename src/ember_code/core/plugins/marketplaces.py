"""Marketplace registry — Claude-Code-compatible plugin catalogs.

A marketplace = a git repo whose ``.claude-plugin/marketplace.json``
(or ``marketplace.json`` at root) carries a catalog of plugins. We
register marketplaces by URL, cache their catalogs locally, and
resolve ``@<marketplace>/<plugin>`` install refs against the cache.

The registry file at ``~/.ember/marketplaces.json`` lives alongside
``plugins.json``. Each entry stores the marketplace's URL, the
parsed catalog from its last fetch, and the timestamp — surfaced in
the plugins panel so users can tell if their catalog is stale.

The cached catalog is **never** required to act — every entry point
falls back to a fresh fetch + parse if the cache is absent. This
keeps first-use after install responsive: nothing breaks because
the background refresh hasn't completed.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ember_code.core.plugins.git import GitClient

logger = logging.getLogger(__name__)


# ── Schemas ─────────────────────────────────────────────────────────


class MarketplacePluginEntry(BaseModel):
    """One row in a marketplace's catalog.

    Mirrors Claude Code's ``marketplace.json#plugins[*]`` schema.
    ``source`` is the git URL the plugin lives at — ``install`` hands
    this to :class:`PluginInstaller`. Extra fields are preserved for
    forward compatibility.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    source: str
    description: str | None = None
    author: str | dict | None = None
    version: str | None = None
    branch: str | None = None


class MarketplaceCatalog(BaseModel):
    """The ``.claude-plugin/marketplace.json`` shape."""

    model_config = ConfigDict(extra="allow")

    name: str
    description: str | None = None
    plugins: list[MarketplacePluginEntry] = Field(default_factory=list)


class MarketplaceEntry(BaseModel):
    """One registered marketplace in our local registry."""

    name: str
    url: str
    last_fetched: str | None = None  # ISO-8601 UTC; None until first fetch
    cached: MarketplaceCatalog | None = None


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


# ── Persistence ─────────────────────────────────────────────────────


def registry_path(data_dir: str | Path = "~/.ember") -> Path:
    return Path(str(data_dir)).expanduser() / "marketplaces.json"


def load_registry(data_dir: str | Path = "~/.ember") -> MarketplaceRegistry:
    """Read the registry file, or return an empty registry if missing/corrupt.

    Corrupt = log warning + return empty. The user can rebuild by
    re-adding marketplaces; no pinning info is lost (that lives in
    ``plugins.json``).
    """
    path = registry_path(data_dir)
    if not path.is_file():
        return MarketplaceRegistry()
    try:
        return MarketplaceRegistry.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Failed to parse marketplace registry at %s: %s", path, e)
        return MarketplaceRegistry()


def save_registry(registry: MarketplaceRegistry, data_dir: str | Path = "~/.ember") -> None:
    """Atomically write the registry. Creates parent dir as needed."""
    path = registry_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(registry.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)


# ── Catalog fetch ──────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch_catalog(url: str, *, git_client: GitClient | None = None) -> MarketplaceCatalog:
    """Shallow-clone *url* to a temp dir, read its catalog, return it.

    Looks for the catalog at ``.claude-plugin/marketplace.json``
    first (Claude's canonical location), falls back to
    ``marketplace.json`` at the repo root for marketplaces that
    haven't migrated.

    Raises:
        GitError: clone failed.
        ValueError: catalog file missing or malformed.
    """
    git = git_client or GitClient()
    tmp_root = Path(tempfile.mkdtemp(prefix="ember_mkt_"))
    try:
        clone_dir = tmp_root / "repo"
        git.clone(url, clone_dir)

        candidates = [
            clone_dir / ".claude-plugin" / "marketplace.json",
            clone_dir / "marketplace.json",
        ]
        catalog_path = next((p for p in candidates if p.is_file()), None)
        if catalog_path is None:
            raise ValueError(
                f"No marketplace.json found at {url} — checked "
                ".claude-plugin/marketplace.json and root."
            )

        return MarketplaceCatalog.model_validate_json(catalog_path.read_text(encoding="utf-8"))
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


# ── Operations ──────────────────────────────────────────────────────


def add_marketplace(
    url: str,
    *,
    data_dir: str | Path = "~/.ember",
    git_client: GitClient | None = None,
) -> MarketplaceEntry:
    """Register a marketplace by URL: probe, fetch catalog, persist.

    The marketplace's display name comes from its catalog (the
    ``name`` field in marketplace.json), not from the URL. So users
    write ``@anthropics-plugins/foo`` regardless of where the
    marketplace's git repo lives.
    """
    catalog = fetch_catalog(url, git_client=git_client)

    registry = load_registry(data_dir)
    existing = registry.find(catalog.name)
    if existing is not None:
        # Update URL + cache if a marketplace with this name is re-added
        # (the user might be migrating to a new git URL). Keeps the
        # registry idempotent rather than throwing on a no-op-shaped
        # action.
        existing.url = url
        existing.cached = catalog
        existing.last_fetched = _now_iso()
    else:
        registry.marketplaces.append(
            MarketplaceEntry(
                name=catalog.name,
                url=url,
                last_fetched=_now_iso(),
                cached=catalog,
            )
        )

    save_registry(registry, data_dir)
    return registry.find(catalog.name)  # type: ignore[return-value]


def remove_marketplace(name: str, *, data_dir: str | Path = "~/.ember") -> bool:
    """Drop a marketplace from the registry. Installed plugins from
    the marketplace are NOT touched — they keep working until removed
    via ``/plugin remove``. Returns True if a marketplace was removed."""
    registry = load_registry(data_dir)
    before = len(registry.marketplaces)
    registry.marketplaces = [m for m in registry.marketplaces if m.name != name]
    if len(registry.marketplaces) == before:
        return False
    save_registry(registry, data_dir)
    return True


def refresh_marketplace(
    name: str,
    *,
    data_dir: str | Path = "~/.ember",
    git_client: GitClient | None = None,
) -> MarketplaceEntry | None:
    """Re-fetch a single marketplace's catalog and update its cache.

    Returns the updated entry, or ``None`` if no marketplace by that
    name is registered. Network or parse failures raise — callers
    that want best-effort behavior (the background refresh) catch
    explicitly.
    """
    registry = load_registry(data_dir)
    entry = registry.find(name)
    if entry is None:
        return None

    entry.cached = fetch_catalog(entry.url, git_client=git_client)
    entry.last_fetched = _now_iso()
    save_registry(registry, data_dir)
    return entry


def resolve_install_ref(
    ref: str, *, data_dir: str | Path = "~/.ember"
) -> tuple[str, MarketplacePluginEntry] | None:
    """Resolve an ``@<marketplace>/<plugin>`` ref to its git URL.

    Returns ``(git_url, MarketplacePluginEntry)`` on hit, ``None`` if
    the ref doesn't match an installed marketplace or doesn't contain
    a known plugin name. Callers should fall through to treating the
    ref as a plain git URL when this returns ``None``.
    """
    if not ref.startswith("@") or "/" not in ref:
        return None

    marketplace_name, _, plugin_name = ref[1:].partition("/")
    if not marketplace_name or not plugin_name:
        return None

    registry = load_registry(data_dir)
    entry = registry.find(marketplace_name)
    if entry is None or entry.cached is None:
        return None

    for plugin in entry.cached.plugins:
        if plugin.name == plugin_name:
            return plugin.source, plugin
    return None
