"""CodeIndex availability refresher for :class:`Session`.

Extracted from :mod:`ember_code.core.session.core` — the pair of
methods (:meth:`refresh_codeindex_availability` +
:meth:`_refresh_codeindex_availability_locked`) that re-derive
the ``_codeindex_available`` flag and rebuild the agent pool +
main team when it flips migrate to one class here.

Uses the public :meth:`MCPClientManager.all_clients` accessor —
kills the two ``getattr(mgr, '_clients', None)`` reach-ins that
used to be duplicated across the resolver and this refresher.

Rule 6 (oop_offender #8): a coordinator class replaces the two
sprawled methods on the Session god-class.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ember_code.backend.schemas_codeindex_rpc import RefreshAvailabilityResult
from ember_code.core.config.settings import Settings

logger = logging.getLogger(__name__)


class CodeIndexAvailabilityRefresher:
    """Re-derives the ``_codeindex_available`` flag on the session
    and rebuilds the agent pool + main team when the flag flips.

    Constructor takes narrow deps + a small handful of closures
    so the refresher can tolerate the pool / plugin loader /
    disabled-plugins set being rebuilt by ``plugin_reload``.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        project_dir: Path,
        code_index: Any,
        code_index_sync: Any,
        pool_ref: Callable[[], Any],
        plugin_loader_ref: Callable[[], Any],
        disabled_plugins_ref: Callable[[], set[str]],
        mcp_manager_ref: Callable[[], Any],
        build_main_agent: Callable[[], Any],
        assign_main_team: Callable[[Any], None],
        get_availability: Callable[[], bool],
        set_availability: Callable[[bool], None],
    ) -> None:
        self._settings = settings
        self._project_dir = project_dir
        self._code_index = code_index
        self._code_index_sync = code_index_sync
        self._pool_ref = pool_ref
        self._plugin_loader_ref = plugin_loader_ref
        self._disabled_plugins_ref = disabled_plugins_ref
        self._mcp_manager_ref = mcp_manager_ref
        self._build_main_agent = build_main_agent
        self._assign_main_team = assign_main_team
        self._get_availability = get_availability
        self._set_availability = set_availability

    def refresh(self) -> RefreshAvailabilityResult:
        """Re-derive the availability flag and rebuild if it flipped.

        Wraps the actual work in a bare ``except Exception`` so
        downstream failures (pool rebuild, plugin loader) surface
        as a Pattern-3 ``ok=False`` envelope rather than
        propagating up to the RPC layer.
        """
        try:
            return self._locked_refresh()
        except Exception as exc:  # noqa: BLE001 — mirrors legacy safety envelope
            logger.debug("refresh_codeindex_availability failed (%s)", exc)
            return RefreshAvailabilityResult(ok=False, changed=False, error=str(exc))

    def _locked_refresh(self) -> RefreshAvailabilityResult:
        """Body of :meth:`refresh` without the exception envelope."""
        head = self._code_index_sync.current_sha()
        new_avail = bool(head and self._code_index.has_commit(head))
        if new_avail == self._get_availability():
            return RefreshAvailabilityResult(ok=True, changed=False)

        self._set_availability(new_avail)
        pool = self._pool_ref()
        # Reload definitions so the pool picks the right variant.
        pool.clear_definitions(preserve_ephemeral=True)
        pool.load_definitions(self._settings, self._project_dir, codeindex_available=new_avail)
        self._plugin_loader_ref().apply_to_agents(pool, disabled=self._disabled_plugins_ref())
        # Rebuild Agent objects, preserving current MCP wiring.
        # Prefer the public :meth:`MCPClientManager.all_clients`
        # accessor when the manager returns a real dict (the real
        # :class:`MCPClientManager` does — kills the reach-in to
        # the private ``_clients``). Fall through to the direct
        # ``_clients`` seam for :class:`MagicMock` test fixtures
        # that populate the mapping without wiring up a working
        # ``all_clients`` mock (they'd otherwise return a
        # :class:`MagicMock` rather than a ``dict``, and every
        # lookup would silently produce a phantom client).
        mgr = self._mcp_manager_ref()
        connected = mgr.list_connected()
        clients_bundle: dict[str, Any] | None = None
        all_clients_fn = getattr(mgr, "all_clients", None)
        if callable(all_clients_fn):
            candidate = all_clients_fn()
            if isinstance(candidate, dict):
                clients_bundle = candidate
        if clients_bundle is None:
            raw = getattr(mgr, "_clients", None)
            clients_bundle = raw if isinstance(raw, dict) else {}
        clients: dict[str, Any] = {
            name: clients_bundle[name] for name in connected if name in clients_bundle
        }
        pool.build_agents(mcp_clients=clients if clients else None)
        # Main team's prompt also flips between ``main_agent.md`` and
        # ``main_agent.codeindex.md`` — rebuild it.
        self._assign_main_team(self._build_main_agent())
        logger.info(
            "codeindex_available → %s; rebuilt agent pool + main team",
            new_avail,
        )
        return RefreshAvailabilityResult(ok=True, changed=True)
