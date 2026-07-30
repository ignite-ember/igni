"""Backward-compat façade for the marketplace-registry surface.

The old god-module ``marketplaces.py`` mixed persistence, catalog
fetch, install-ref parsing, and five Pydantic schemas into one
387-line file. That code now lives in two colocated modules:

* :mod:`.models` — every marketplace schema
  (:class:`MarketplaceCatalog`, :class:`MarketplacePluginEntry`,
  :class:`MarketplaceEntry`, :class:`MarketplaceRegistry`,
  :class:`ResolvedSource`, :class:`InstallRef`, plus the two
  discriminated-union arms :class:`MarketplaceUrlSource` and
  :class:`MarketplaceGitSubdirSource`).
* :mod:`.marketplace_store` — the operational surface, held on a
  single :class:`MarketplaceRegistryStore` class plus the
  Pattern-3 :class:`CatalogFetchResult` value object.

This module re-exports the schemas so external imports keep
working. The seven legacy free-function names
(``load_registry`` / ``save_registry`` / ``fetch_catalog`` /
``add_marketplace`` / ``remove_marketplace`` /
``refresh_marketplace`` / ``resolve_install_ref`` /
``registry_path``) are now bound methods on a single
:class:`_MarketplaceModuleAdapter` instance, published as
module attributes so ``patch("...marketplaces.<name>")`` sites
across the test suite (see ``tests/test_plugins_backend.py``,
``tests/test_plugins_background_refresh.py``,
``tests/test_plugins_slash_commands.py``) and the backend
re-exporters (:mod:`ember_code.backend.command_handler`,
:mod:`ember_code.backend.plugin_controller`, etc.) keep
resolving unchanged. Bound-method assignment to a module
attribute is functionally identical to a free function from
``unittest.mock.patch``'s perspective — the patch replaces the
module attribute, not the underlying callable — so the ~50
existing patch sites need no changes.

The adapter itself owns no state beyond the default
``data_dir`` / ``git_client`` values and delegates every call to
:class:`MarketplaceRegistryStore`. New in-tree callers should
instantiate :class:`MarketplaceRegistryStore` directly; the
adapter is deprecated and exists purely as a monkey-patch
compat surface.
"""

from __future__ import annotations

from pathlib import Path

from ember_code.core.plugins.git import GitClient
from ember_code.core.plugins.marketplace_store import (
    DEFAULT_MARKETPLACES,
    CatalogFetchResult,
    MarketplaceRefreshOutcome,
    MarketplaceRegistryStore,
)
from ember_code.core.plugins.models import (
    InstallRef,
    MarketplaceCatalog,
    MarketplaceEntry,
    MarketplaceGitSubdirSource,
    MarketplacePluginEntry,
    MarketplaceRegistry,
    MarketplaceUrlSource,
    ResolvedSource,
)


class _MarketplaceModuleAdapter:
    """Deprecated compat surface — thin bound-method wrapper over
    :class:`MarketplaceRegistryStore`.

    New callers should use :class:`MarketplaceRegistryStore`
    directly. This adapter exists solely so the module-level
    attribute surface (``marketplaces.load_registry`` etc.) that
    ~50 test-suite ``patch("...marketplaces.<name>")`` calls and
    five backend re-exporters depend on keeps working without a
    wholesale rewrite.

    A single :class:`_MarketplaceModuleAdapter` instance is bound
    to the module's public function names at import time (see
    the ``_ADAPTER = _MarketplaceModuleAdapter()`` line below).
    Because :func:`unittest.mock.patch` replaces the module
    attribute — not the underlying callable — bound-method
    assignment is fully patch-compatible.

    TODO(follow-up): migrate the test-suite patch sites to
    ``patch.object(MarketplaceRegistryStore, "<method>")`` and
    then delete this adapter (and this whole file) along with
    the backend re-export blocks.
    """

    def _store(
        self,
        data_dir: str | Path,
        git_client: GitClient | None = None,
    ) -> MarketplaceRegistryStore:
        """Build a fresh :class:`MarketplaceRegistryStore` per call.

        Cheap (no I/O in the constructor) and required — the store
        snapshots ``data_dir`` at construction, but every method
        we forward to is meant to observe ``data_dir`` fresh.
        """
        return MarketplaceRegistryStore(data_dir=data_dir, git_client=git_client)

    def registry_path(self, data_dir: str | Path = "~/.ember") -> Path:
        """See :meth:`MarketplaceRegistryStore.path`."""
        return self._store(data_dir).path()

    def load_registry(self, data_dir: str | Path = "~/.ember") -> MarketplaceRegistry:
        """See :meth:`MarketplaceRegistryStore.load`."""
        return self._store(data_dir).load()

    def save_registry(
        self,
        registry: MarketplaceRegistry,
        data_dir: str | Path = "~/.ember",
    ) -> None:
        """See :meth:`MarketplaceRegistryStore.save`."""
        self._store(data_dir).save(registry)

    def fetch_catalog(self, url: str, *, git_client: GitClient | None = None) -> MarketplaceCatalog:
        """Fetch a marketplace catalog by URL.

        Legacy raise-on-error signature — the store's own fetch
        method returns a :class:`CatalogFetchResult`. This wrapper
        unwraps that into either the catalog or a raised
        :class:`ValueError` so the old contract
        (``pytest.raises(ValueError)`` in the marketplace tests)
        still holds.

        Raises:
            ValueError: catalog file missing, unparseable, or the
                clone step failed. The message carries the
                underlying reason verbatim.
        """
        return self._store("~/.ember", git_client).fetch_catalog(url).unwrap()

    def add_marketplace(
        self,
        url: str,
        *,
        data_dir: str | Path = "~/.ember",
        git_client: GitClient | None = None,
    ) -> MarketplaceEntry:
        """See :meth:`MarketplaceRegistryStore.add`."""
        return self._store(data_dir, git_client).add(url)

    def remove_marketplace(self, name: str, *, data_dir: str | Path = "~/.ember") -> bool:
        """See :meth:`MarketplaceRegistryStore.remove`."""
        return self._store(data_dir).remove(name)

    def refresh_marketplace(
        self,
        name: str,
        *,
        data_dir: str | Path = "~/.ember",
        git_client: GitClient | None = None,
    ) -> MarketplaceEntry | None:
        """See :meth:`MarketplaceRegistryStore.refresh`."""
        return self._store(data_dir, git_client).refresh(name)

    def resolve_install_ref(
        self, ref: str, *, data_dir: str | Path = "~/.ember"
    ) -> tuple[ResolvedSource, MarketplacePluginEntry] | None:
        """See :meth:`MarketplaceRegistryStore.resolve_install_ref`."""
        return self._store(data_dir).resolve_install_ref(ref)


# Single adapter instance; its bound methods become the module's
# public function surface. `unittest.mock.patch` replaces the
# module attribute wholesale, so binding a bound method here is
# indistinguishable from a free function to every caller and
# test patch site.
_ADAPTER = _MarketplaceModuleAdapter()

registry_path = _ADAPTER.registry_path  # deprecated: use MarketplaceRegistryStore.path
load_registry = _ADAPTER.load_registry  # deprecated: use MarketplaceRegistryStore.load
save_registry = _ADAPTER.save_registry  # deprecated: use MarketplaceRegistryStore.save
fetch_catalog = _ADAPTER.fetch_catalog  # deprecated: use MarketplaceRegistryStore.fetch_catalog
add_marketplace = _ADAPTER.add_marketplace  # deprecated: use MarketplaceRegistryStore.add
remove_marketplace = _ADAPTER.remove_marketplace  # deprecated: use MarketplaceRegistryStore.remove
refresh_marketplace = (
    _ADAPTER.refresh_marketplace
)  # deprecated: use MarketplaceRegistryStore.refresh
resolve_install_ref = (
    _ADAPTER.resolve_install_ref
)  # deprecated: use MarketplaceRegistryStore.resolve_install_ref


__all__ = [
    "DEFAULT_MARKETPLACES",
    "CatalogFetchResult",
    "InstallRef",
    "MarketplaceCatalog",
    "MarketplaceEntry",
    "MarketplaceGitSubdirSource",
    "MarketplacePluginEntry",
    "MarketplaceRefreshOutcome",
    "MarketplaceRegistry",
    "MarketplaceRegistryStore",
    "MarketplaceUrlSource",
    "ResolvedSource",
    "add_marketplace",
    "fetch_catalog",
    "load_registry",
    "refresh_marketplace",
    "registry_path",
    "remove_marketplace",
    "resolve_install_ref",
    "save_registry",
]
