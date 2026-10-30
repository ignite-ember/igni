"""Plugin-marketplace catalog refresh phase.

Auto-registers the canonical default marketplace (Anthropic's
official one) and the organisation's own (``plugins.marketplaces``)
on a brand-new install, then refreshes every registered
marketplace's catalog. Both steps are fire-and-forget on the
running loop — session boot is unaffected even if every marketplace
is unreachable.

Both steps go through :class:`PluginSourcePolicy`. The default
marketplace is a clone from github.com, so ``plugins.default_marketplace:
false`` turns it off on its own, and ``air_gapped: true`` turns it off
along with the refresh of any registered marketplace that is not a
local path or one of the organisation's: no git process is started for
those, so no connection is attempted.

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
from ember_code.core.plugins.source_policy import PluginSourcePolicy
from ember_code.core.session.startup.base import SessionStartupPhase

logger = logging.getLogger(__name__)


class MarketplaceWarmupPhase(SessionStartupPhase):
    """Fire-and-forget refresh of every registered plugin marketplace
    catalog + auto-register defaults.

    Fire-and-forget on the
    running loop, per-marketplace timeout, all failures logged and
    swallowed. A catalog fetched within :attr:`REFRESH_EVERY_SECONDS`
    is kept: a refresh clones the marketplace's repository into a
    temporary directory and deletes it again — thousands of files
    written and removed, which in the cloud are memory a suspended
    VM must keep — and every session start used to do it, including
    right after the auto-register step had just fetched the same
    catalog. ``/plugin marketplace refresh`` still fetches on demand.
    """

    _TIMEOUT_ADD: ClassVar[float] = 15.0
    _TIMEOUT_REFRESH: ClassVar[float] = 10.0
    #: A catalog younger than this is not fetched again at session start.
    REFRESH_EVERY_SECONDS: ClassVar[float] = 6 * 3600.0

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

    def _policy(self) -> PluginSourcePolicy:
        """Read fresh from the session's settings on every pass."""
        return PluginSourcePolicy.from_settings(self.session.settings)

    async def _auto_register_defaults(self, registry: MarketplaceRegistry, data_dir: Path) -> None:
        """Auto-register the default marketplace (when allowed) and the
        organisation's own, so a brand-new install sees plugins on
        first open without the user having to run ``/plugin
        marketplace add``. Idempotent — a marketplace already registered
        under the default's name or at the same URL is skipped, and
        ``add_marketplace`` updates in place anyway.
        """
        policy = self._policy()
        registered_names = {m.name for m in registry.marketplaces}
        registered_urls = {m.url for m in registry.marketplaces}
        default_names = {url: name for name, url in _marketplaces.DEFAULT_MARKETPLACES}
        for url in policy.marketplaces_to_register():
            if url in registered_urls or default_names.get(url) in registered_names:
                continue
            if not policy.allows(url):
                logger.warning("Not registering marketplace: %s", policy.refusal(url))
                continue
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        _marketplaces.add_marketplace,
                        url,
                        data_dir=data_dir,
                    ),
                    timeout=self._TIMEOUT_ADD,
                )
                logger.info("Auto-registered marketplace: %s", url)
            except Exception as e:  # noqa: BLE001 — best-effort
                logger.warning(
                    "Auto-registering marketplace '%s' failed: %s — user can add manually later.",
                    url,
                    e,
                )

    async def _refresh_registered(self, registry: MarketplaceRegistry, data_dir: Path) -> None:
        """Refresh whatever is now registered (defaults + any
        user-added marketplaces) that the policy allows."""
        policy = self._policy()
        for entry in registry.marketplaces:
            if entry.fetched_within(self.REFRESH_EVERY_SECONDS):
                continue
            if not policy.allows(entry.url):
                logger.info(
                    "Not refreshing marketplace '%s': %s", entry.name, policy.refusal(entry.url)
                )
                continue
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
