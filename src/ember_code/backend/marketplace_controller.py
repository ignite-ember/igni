"""Marketplace registry controller — marketplace RPCs on one class.

* :meth:`list_registered` — snapshot of registered marketplaces +
  their cached catalogs.
* :meth:`add` — register a new marketplace by URL.
* :meth:`remove` — unregister a marketplace (installed plugins from
  it remain).
* :meth:`refresh` — re-fetch one or all marketplaces. Delegates to
  :meth:`_refresh_all` (typed :class:`MarketplaceRefreshResult`) +
  :meth:`_render`.

``self._session.settings.storage.data_dir`` is read fresh inside
each method (via :meth:`_store`) so a mid-session re-config of the
data dir is picked up on the next call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ember_code.backend.plugin_schemas import (
    MarketplaceRefreshFailure,
    MarketplaceRefreshResult,
)

# Module-attribute import (not ``from ... import name``) so tests
# patching ``marketplaces.refresh_marketplace`` at the source
# module take effect on call sites here. The controller drives
# ``MarketplaceRegistryStore`` directly for production paths, but
# the legacy free-function surface stays reachable for tests that
# monkey-patch it (``tests/test_plugins_backend.py``).
from ember_code.core.plugins import marketplaces as _plugin_marketplaces
from ember_code.core.plugins.git import GitError
from ember_code.core.plugins.models import (
    MarketplaceInfo,
    MarketplacePluginInfo,
)
from ember_code.protocol import messages as msg

if TYPE_CHECKING:
    from ember_code.core.session import Session


class MarketplaceController:
    """Marketplace registry CRUD + bulk refresh. Composed onto
    :class:`BackendServer` as ``self.marketplaces``.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ── List / add / remove ────────────────────────────────────────

    def list_registered(self) -> list[MarketplaceInfo]:
        """Snapshot of every registered marketplace for the panel.
        Returns typed :class:`MarketplaceInfo` models (nesting
        :class:`MarketplacePluginInfo` per catalog entry).
        """
        registry = _plugin_marketplaces.load_registry(
            data_dir=self._session.settings.storage.data_dir,
        )
        out: list[MarketplaceInfo] = []
        for m in registry.marketplaces:
            plugins = [
                MarketplacePluginInfo.from_catalog_entry(p, m.url)
                for p in (m.cached.plugins if m.cached else [])
            ]
            out.append(
                MarketplaceInfo(
                    name=m.name,
                    url=m.url,
                    last_fetched=m.last_fetched or "",
                    plugins=plugins,
                )
            )
        return out

    def add(self, url: str) -> msg.Info:
        """Register a new marketplace by URL and cache its catalog."""
        try:
            entry = _plugin_marketplaces.add_marketplace(
                url, data_dir=self._session.settings.storage.data_dir
            )
        except GitError as e:
            return msg.Info(text=f"git error: {e}")
        except Exception as e:  # noqa: BLE001 — surface verbatim
            return msg.Info(text=f"Failed to add marketplace: {e}")
        count = len(entry.cached.plugins) if entry.cached else 0
        return msg.Info(text=f"Added '{entry.name}' ({count} plugin(s) catalogued).")

    def remove(self, name: str) -> msg.Info:
        """Unregister a marketplace. Installed plugins from it remain."""
        if not _plugin_marketplaces.remove_marketplace(
            name, data_dir=self._session.settings.storage.data_dir
        ):
            return msg.Info(text=f"No marketplace named '{name}'.")
        return msg.Info(text=f"Unregistered '{name}'. Installed plugins from it remain.")

    # ── Refresh (orchestration + render split) ────────────────────

    def refresh(self, name: str | None = None) -> msg.Info:
        """Re-fetch one marketplace or all.

        The named path returns immediately (single-marketplace
        error message shape hasn't been merged into the aggregate
        rendering — the FE surfaces it differently). The
        refresh-all path splits orchestration from message shaping
        via :meth:`_refresh_all` + :meth:`_render`.
        """
        data_dir = self._session.settings.storage.data_dir
        if name:
            try:
                entry = _plugin_marketplaces.refresh_marketplace(name, data_dir=data_dir)
            except Exception as e:  # noqa: BLE001
                return msg.Info(text=f"Refresh failed for '{name}': {e}")
            if entry is None:
                return msg.Info(text=f"No marketplace named '{name}'.")
            count = len(entry.cached.plugins) if entry.cached else 0
            return msg.Info(text=f"Refreshed '{entry.name}' ({count} plugins).")

        result = self._refresh_all()
        return self._render(result)

    def _refresh_all(self) -> MarketplaceRefreshResult:
        """Iterate every registered marketplace and refresh it.

        Errors are collected per-marketplace and reported together
        so a single bad URL doesn't abort the whole refresh.
        Returns a typed result — rendered into :class:`msg.Info`
        by :meth:`_render`.

        Uses the free-function shims on :mod:`.marketplaces` (not
        :class:`MarketplaceRegistryStore.refresh_all`) so tests
        that patch ``ember_code.core.plugins.marketplaces.load_registry``
        + ``.refresh_marketplace`` still land — the shim contract
        is what those tests exercise.
        """
        data_dir = self._session.settings.storage.data_dir
        registry = _plugin_marketplaces.load_registry(data_dir=data_dir)
        ok: list[str] = []
        failed: list[MarketplaceRefreshFailure] = []
        for m in registry.marketplaces:
            try:
                _plugin_marketplaces.refresh_marketplace(m.name, data_dir=data_dir)
                ok.append(m.name)
            except Exception as e:  # noqa: BLE001
                failed.append(MarketplaceRefreshFailure(name=m.name, reason=str(e)))
        return MarketplaceRefreshResult(ok=ok, failed=failed)

    @staticmethod
    def _render(result: MarketplaceRefreshResult) -> msg.Info:
        """Format a :class:`MarketplaceRefreshResult` into the
        panel's :class:`msg.Info`. Pure function of the result
        — testable without touching the marketplaces module."""
        if not result.ok and not result.failed:
            return msg.Info(text="No marketplaces to refresh.")
        line = f"Refreshed {len(result.ok)} ok"
        if result.failed:
            failures = ", ".join(f"{f.name} ({f.reason})" for f in result.failed)
            line += f"; {len(result.failed)} failed: {failures}"
        return msg.Info(text=line)
