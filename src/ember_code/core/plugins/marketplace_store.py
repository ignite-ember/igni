"""Marketplace-registry operational surface — persistence, catalog
fetch, and install-ref resolution — as a single owning class.

A marketplace = a git repo whose ``.claude-plugin/marketplace.json``
(or ``marketplace.json`` at root) carries a catalog of plugins. We
register marketplaces by URL, cache their catalogs locally, and
resolve ``@<marketplace>/<plugin>`` install refs against the cache.

The registry file at ``~/.ember/marketplaces.json`` lives alongside
``plugins.json``. Each entry stores the marketplace's URL, the
parsed catalog from its last fetch, and the timestamp — surfaced
in the plugins panel so users can tell if their catalog is stale.

The cached catalog is **never** required to act — every entry
point falls back to a fresh fetch + parse if the cache is absent.
This keeps first-use after install responsive: nothing breaks
because the background refresh hasn't completed.

Schemas (:class:`MarketplaceCatalog`,
:class:`MarketplacePluginEntry`, :class:`MarketplaceEntry`,
:class:`MarketplaceRegistry`, :class:`ResolvedSource`,
:class:`InstallRef`) live in :mod:`.models` per Pattern 7
(wire/domain schemas colocated). This module owns *behaviour*.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ember_code.core.plugins.git import GitClient
from ember_code.core.plugins.models import (
    InstallRef,
    MarketplaceCatalog,
    MarketplaceEntry,
    MarketplacePluginEntry,
    MarketplaceRegistry,
    ResolvedSource,
)

logger = logging.getLogger(__name__)


# ── Default marketplaces ───────────────────────────────────────────


# Marketplaces auto-registered on session start so users see plugins
# the moment they open the panel — no ``/plugin marketplace add``
# step required.
#
# Currently a single canonical entry: Anthropic's official directory
# of ~200 Claude-Code plugins. Adding it on first run mirrors the
# Claude Code CLI's own out-of-box behavior and gives our
# Claude-Code-compatibility claim immediate concrete proof
# (browse the panel, install something, watch it work).
#
# Each entry is ``(catalog_name, git_url)``. The ``catalog_name``
# is what the user types after ``@`` (``@claude-plugins-official/foo``);
# we know it ahead of time because the marketplace's own
# ``marketplace.json`` declares it.
DEFAULT_MARKETPLACES: list[tuple[str, str]] = [
    (
        "claude-plugins-official",
        "https://github.com/anthropics/claude-plugins-official",
    ),
]


# ── Catalog fetch (Pattern 3 result) ────────────────────────────────


class CatalogFetchResult(BaseModel):
    """Pattern-3 typed result of a catalog fetch attempt.

    Callers on the best-effort path (background refresh, batch
    refresh) inspect ``ok`` and skip failed entries. Callers on the
    fail-fast path (``/plugin marketplace add``) call
    :meth:`unwrap` to preserve the "raise on error" contract.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    ok: bool
    catalog: MarketplaceCatalog | None = None
    reason: str = ""

    def unwrap(self) -> MarketplaceCatalog:
        """Return the catalog if the fetch succeeded; raise
        :class:`ValueError` with ``reason`` otherwise. Bridges the
        Result-style API to callers that want the classic raise."""
        if self.ok and self.catalog is not None:
            return self.catalog
        raise ValueError(self.reason or "catalog fetch failed")


class _CatalogFetcher:
    """Shallow-clone a marketplace repo, read its catalog, delete
    the clone. Isolated as a class so tests can stub it at the
    store-instance level (``store._fetcher = FakeFetcher(...)``)
    without monkey-patching module attributes.

    The class also keeps the ``shutil`` / ``tempfile`` imports out
    of :mod:`.models`, matching the convention that pure schemas
    don't pull heavy stdlib into their module.
    """

    def __init__(self, git_client: GitClient | None = None) -> None:
        self._git = git_client or GitClient()

    def fetch(self, url: str) -> CatalogFetchResult:
        """Shallow-clone *url* to a temp dir, read its catalog,
        return a :class:`CatalogFetchResult`.

        Looks for the catalog at ``.claude-plugin/marketplace.json``
        first (Claude's canonical location), falls back to
        ``marketplace.json`` at the repo root for marketplaces
        that haven't migrated. Any git / parse failure becomes a
        Result with ``ok=False`` and the exception message; the
        temp clone is always cleaned up.
        """
        tmp_root = Path(tempfile.mkdtemp(prefix="ember_mkt_"))
        try:
            clone_dir = tmp_root / "repo"
            try:
                self._git.clone(url, clone_dir)
            except Exception as exc:  # noqa: BLE001 — surface as Result
                # ``GitError`` most commonly; anything else is
                # equally a fetch failure the caller wants to know
                # about via ``reason``.
                return CatalogFetchResult(ok=False, reason=str(exc))

            candidates = [
                clone_dir / ".claude-plugin" / "marketplace.json",
                clone_dir / "marketplace.json",
            ]
            catalog_path = next((p for p in candidates if p.is_file()), None)
            if catalog_path is None:
                return CatalogFetchResult(
                    ok=False,
                    reason=(
                        f"No marketplace.json found at {url} — checked "
                        ".claude-plugin/marketplace.json and root."
                    ),
                )

            try:
                catalog = MarketplaceCatalog.model_validate_json(
                    catalog_path.read_text(encoding="utf-8")
                )
            except Exception as exc:  # noqa: BLE001 — pydantic ValidationError, JSON errors
                return CatalogFetchResult(ok=False, reason=str(exc))

            return CatalogFetchResult(ok=True, catalog=catalog)
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)


# ── The store ──────────────────────────────────────────────────────


class MarketplaceRegistryStore:
    """Owns the marketplace registry on disk plus the operations
    that read from and write to it.

    Every method reads ``data_dir`` fresh via :meth:`path` — the
    constructor snapshots ``data_dir`` at build time (typical
    session lifetime), but the file is re-opened on each call so
    an atomic external edit is picked up on the next load.

    Composed of one :class:`_CatalogFetcher` per store instance so
    tests can swap in a fake fetcher for hermetic runs.
    """

    def __init__(
        self,
        data_dir: str | Path = "~/.ember",
        *,
        git_client: GitClient | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._fetcher = _CatalogFetcher(git_client=git_client)

    # ── Persistence ────────────────────────────────────────────────

    def path(self) -> Path:
        """Absolute path to the on-disk registry file. Expanded
        each call so ``~/.ember`` follows the user's actual HOME
        at read time (matters for tests + Windows profile switches)."""
        return Path(str(self._data_dir)).expanduser() / "marketplaces.json"

    def load(self) -> MarketplaceRegistry:
        """Read the registry file, or return an empty registry if
        missing/corrupt.

        Corrupt = log warning + return empty. The user can rebuild
        by re-adding marketplaces; no pinning info is lost (that
        lives in ``plugins.json``).
        """
        path = self.path()
        if not path.is_file():
            return MarketplaceRegistry()
        try:
            return MarketplaceRegistry.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001 — corruption is best-effort
            logger.warning("Failed to parse marketplace registry at %s: %s", path, e)
            return MarketplaceRegistry()

    def save(self, registry: MarketplaceRegistry) -> None:
        """Atomically write the registry. Creates parent dir as needed."""
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(registry.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)

    # ── Catalog fetch (thin façade) ────────────────────────────────

    def fetch_catalog(self, url: str) -> CatalogFetchResult:
        """Fetch a marketplace's catalog by URL. Returns a
        :class:`CatalogFetchResult` — never raises for expected
        network / parse failures."""
        return self._fetcher.fetch(url)

    # ── High-level operations ──────────────────────────────────────

    def add(self, url: str) -> MarketplaceEntry:
        """Register a marketplace by URL: probe, fetch catalog, persist.

        The marketplace's display name comes from its catalog (the
        ``name`` field in marketplace.json), not from the URL. So
        users write ``@anthropics-plugins/foo`` regardless of where
        the marketplace's git repo lives.

        Raises on fetch failure (network / parse) — mirrors the old
        contract; callers that catch got the same behaviour before.
        """
        # ``unwrap`` preserves the raise-on-error contract of the
        # legacy ``add_marketplace``; callers wanting best-effort go
        # through :meth:`fetch_catalog` directly.
        catalog = self._fetcher.fetch(url).unwrap()

        registry = self.load()
        new_entry = MarketplaceEntry(name=catalog.name, url=url)
        new_entry.mark_fetched(catalog)
        effective = registry.add(new_entry)
        self.save(registry)
        return effective

    def remove(self, name: str) -> bool:
        """Drop a marketplace from the registry. Installed plugins
        from it are NOT touched — they keep working until removed
        via ``/plugin remove``. Returns ``True`` when a marketplace
        was removed."""
        registry = self.load()
        if not registry.remove(name):
            return False
        self.save(registry)
        return True

    def refresh(self, name: str) -> MarketplaceEntry | None:
        """Re-fetch a single marketplace's catalog and update its
        cache.

        Returns the updated entry, or ``None`` if no marketplace by
        that name is registered. Network or parse failures raise
        via :meth:`CatalogFetchResult.unwrap` — callers that want
        best-effort (background refresh) either catch or drive
        :meth:`fetch_catalog` directly and inspect ``ok``.
        """
        registry = self.load()
        entry = registry.find(name)
        if entry is None:
            return None
        catalog = self._fetcher.fetch(entry.url).unwrap()
        entry.mark_fetched(catalog)
        self.save(registry)
        return entry

    def refresh_all(self) -> list[MarketplaceRefreshOutcome]:
        """Refresh every registered marketplace best-effort.

        Failures are captured per-marketplace so a single bad URL
        doesn't abort the batch. Returns one
        :class:`MarketplaceRefreshOutcome` per marketplace (order
        preserved from registry order); callers render the batch
        into whatever UI shape they need.
        """
        outcomes: list[MarketplaceRefreshOutcome] = []
        registry = self.load()
        for m in registry.marketplaces:
            result = self._fetcher.fetch(m.url)
            if result.ok and result.catalog is not None:
                m.mark_fetched(result.catalog)
                outcomes.append(MarketplaceRefreshOutcome(name=m.name, ok=True))
            else:
                outcomes.append(
                    MarketplaceRefreshOutcome(name=m.name, ok=False, reason=result.reason)
                )
        # Persist even partial success — the ``ok`` entries updated
        # their cache in place, we want that on disk regardless of
        # the failed ones alongside them.
        self.save(registry)
        return outcomes

    def resolve_install_ref(self, ref: str) -> tuple[ResolvedSource, MarketplacePluginEntry] | None:
        """Resolve an ``@<marketplace>/<plugin>`` ref to a clone
        spec.

        Returns ``(ResolvedSource, MarketplacePluginEntry)`` on
        hit, ``None`` if the ref doesn't parse as a marketplace
        ref, doesn't match a registered marketplace, the catalog
        doesn't list the plugin, or the entry's ``source`` field is
        unparseable. Callers should fall through to treating
        ``ref`` as a plain git URL when this returns ``None``.
        """
        install_ref = InstallRef.parse(ref)
        if install_ref is None:
            return None
        return self.load().resolve_install_ref(install_ref)


class MarketplaceRefreshOutcome(BaseModel):
    """Per-marketplace outcome from :meth:`MarketplaceRegistryStore.refresh_all`."""

    name: str
    ok: bool
    reason: str = ""


__all__ = [
    "DEFAULT_MARKETPLACES",
    "CatalogFetchResult",
    "MarketplaceRefreshOutcome",
    "MarketplaceRegistryStore",
]
