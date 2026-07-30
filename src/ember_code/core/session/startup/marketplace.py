"""Plugin-marketplace catalog refresh phase.

Auto-registers the canonical default marketplaces (Anthropic's
official one, mainly) on a brand-new install, then refreshes
every registered marketplace's catalog. Both steps are fire-and-
forget on the running loop — session boot is unaffected even if
every marketplace is unreachable.

The ``_marketplaces`` module attribute-lookup import is
INTENTIONAL — tests patch
``ember_code.core.plugins.marketplaces.load_registry`` /
``.refresh_marketplace`` and expect the call sites here to route
through that attribute lookup. A direct ``from ... import
load_registry`` would bind at import time and break the patching
contract. This is the ONE call site that needs that seam.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import ClassVar

# Module-attribute pattern — tests patch
# ``ember_code.core.plugins.marketplaces.load_registry`` /
# ``.refresh_marketplace`` and expect the call sites here to
# route through that attribute lookup. Direct ``from … import``
# would bind at import time and break the patching contract.
from ember_code.core.plugins import marketplaces as _marketplaces
from ember_code.core.plugins.models import MarketplaceRegistry
from ember_code.core.session.startup.base import SessionStartupPhase

logger = logging.getLogger(__name__)


class MarketplaceWarmupPhase(SessionStartupPhase):
    """Fire-and-forget refresh of every registered plugin marketplace
    catalog + auto-register defaults.

    Mirrors :class:`CodeIndexWarmupPhase` — fire-and-forget on the
    running loop, no throttle, per-marketplace timeout, all
    failures logged and swallowed. Net effect: by the time the
    user reaches for ``/plugin install`` (seconds to minutes
    later) the catalog is current.
    """

    _TIMEOUT_ADD: ClassVar[float] = 15.0
    _TIMEOUT_REFRESH: ClassVar[float] = 10.0

    def start_background(self) -> None:
        """Kick the marketplace refresh sequence in the background."""
        self._schedule_on_loop(self._refresh_all)

    async def _refresh_all(self) -> None:
        """Auto-register defaults then refresh every entry now
        present in the registry. Idempotent — re-runs on subsequent
        sessions no-op the auto-register step."""
        data_dir = self.session.settings.storage.data_dir
        registry = _marketplaces.load_registry(data_dir)
        await self._auto_register_defaults(registry, data_dir)
        # Re-read the registry since the auto-register step may have
        # appended entries.
        registry = _marketplaces.load_registry(data_dir)
        await self._refresh_registered(registry, data_dir)

    async def _auto_register_defaults(self, registry: MarketplaceRegistry, data_dir: Path) -> None:
        """Auto-register the canonical defaults (Anthropic's
        official marketplace, mainly) so a brand-new install sees
        plugins on first open without the user having to run
        ``/plugin marketplace add``. Idempotent —
        ``add_marketplace`` updates in place when a marketplace by
        the same name already exists.
        """
        registered_names = {m.name for m in registry.marketplaces}
        for default_name, default_url in _marketplaces.DEFAULT_MARKETPLACES:
            if default_name in registered_names:
                continue
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        _marketplaces.add_marketplace,
                        default_url,
                        data_dir=data_dir,
                    ),
                    timeout=self._TIMEOUT_ADD,
                )
                logger.info(
                    "Auto-registered default marketplace: %s",
                    default_name,
                )
            except Exception as e:  # noqa: BLE001 — best-effort
                logger.warning(
                    "Auto-registering default marketplace '%s' "
                    "failed: %s — user can add manually later.",
                    default_name,
                    e,
                )

    async def _refresh_registered(self, registry: MarketplaceRegistry, data_dir: Path) -> None:
        """Refresh whatever is now registered (defaults + any
        user-added marketplaces)."""
        for entry in registry.marketplaces:
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        _marketplaces.refresh_marketplace,
                        entry.name,
                        data_dir=data_dir,
                    ),
                    timeout=self._TIMEOUT_REFRESH,
                )
            except Exception as e:  # noqa: BLE001 — best-effort
                logger.warning(
                    "Marketplace refresh for '%s' failed: %s",
                    entry.name,
                    e,
                )
