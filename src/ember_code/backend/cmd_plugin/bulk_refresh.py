"""Bulk-refresh runner for ``/plugin marketplace refresh`` (no name).

Split out so :class:`MarketplaceRefreshVerb` stays thin — the verb
only decides one-vs-all, then delegates to either
:meth:`PluginBackendGateway.refresh_marketplace` (one) or
:class:`BulkRefreshRunner` (all). Owning bulk-refresh in its own
class matches the synthesis note's "dedicated runner keeps the verb
thin" guidance.

The runner is intentionally single-method — a class rather than a
free function so future concurrency (per-marketplace threading /
async gather) has a natural home without touching the verb.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ember_code.backend.plugin_schemas import (
    MarketplaceRefreshFailure,
    MarketplaceRefreshResult,
)

if TYPE_CHECKING:
    from ember_code.backend.cmd_plugin.gateway import PluginBackendGateway


class BulkRefreshRunner:
    """Refreshes every registered marketplace, collecting per-entry
    outcomes into a :class:`MarketplaceRefreshResult`. Composed with
    a :class:`PluginBackendGateway` at construction; the gateway is
    the seam for the underlying refresh call so this runner never
    touches the marketplaces module directly."""

    def __init__(self, gateway: PluginBackendGateway) -> None:
        self._gateway = gateway

    def run(self, registry) -> MarketplaceRefreshResult:
        """Iterate ``registry.marketplaces``, refresh each, collect
        results. Per-marketplace failures are recorded (not raised)
        so one broken URL doesn't abort the whole refresh."""
        ok: list[str] = []
        failed: list[MarketplaceRefreshFailure] = []
        for m in registry.marketplaces:
            result = self._gateway.refresh_marketplace(m.name)
            if result.ok:
                ok.append(m.name)
            elif result.not_found:
                # Registry iteration walked over an entry the gateway
                # can't find — treat as a data-integrity failure and
                # record so the user sees which entry misbehaved.
                failed.append(MarketplaceRefreshFailure(name=m.name, reason="not registered"))
            else:
                failed.append(MarketplaceRefreshFailure(name=m.name, reason=result.error))
        return MarketplaceRefreshResult(ok=ok, failed=failed)


__all__ = ["BulkRefreshRunner"]
