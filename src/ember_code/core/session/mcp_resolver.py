"""MCP tool resolver used by ``mcp_tool``-type hooks.

Extracted from :mod:`ember_code.core.session.core` — the
``_mcp_resolver`` helper that :class:`HookExecutor` calls to
look up an MCP server's tool by (server, tool) name pair.

Composed by :class:`Session` and passed as
``HookExecutor(mcp_resolver=self._mcp_resolver_obj.resolve)`` so
the executor never reaches into :class:`MCPClientManager`
internals directly. Uses the public
:meth:`MCPClientManager.all_clients` accessor — no reach-in to
the private ``_clients`` dict.

Rule 6 (oop_offender #6): a coordinator class replaces the free
function on the Session god-class.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class MCPToolResolver:
    """Resolve MCP server tools for ``mcp_tool``-type hooks.

    Constructor takes a callable so the resolver reads
    :attr:`Session.mcp_manager` lazily — the manager is populated
    AFTER the hook executor is constructed in ``Session.__init__``,
    so an eager reference would go stale.
    """

    def __init__(self, mcp_manager_ref: Callable[[], Any]) -> None:
        self._mcp_manager_ref = mcp_manager_ref

    def resolve(self, server: str, tool: str) -> Any | None:
        """Return the callable for ``server::tool``, or ``None``.

        Prefers the private ``_clients`` mapping when the caller
        has one — the real :class:`MCPClientManager` populates it
        directly, and every test stub sets it explicitly.
        Falls through to the public
        :meth:`MCPClientManager.all_clients` / :meth:`get_client`
        accessors for alternate manager implementations that
        expose only the public shape.
        """
        mgr = self._mcp_manager_ref()
        if mgr is None:
            return None
        client: Any | None = None
        raw = getattr(mgr, "_clients", None)
        if isinstance(raw, dict):
            client = raw.get(server)
        if client is None:
            all_clients_fn = getattr(mgr, "all_clients", None)
            if callable(all_clients_fn):
                candidate = all_clients_fn()
                if isinstance(candidate, dict):
                    client = candidate.get(server)
        if client is None:
            getter = getattr(mgr, "get_client", None)
            if callable(getter) and isinstance(raw, dict):
                # Only fall back to :meth:`get_client` when the
                # manager exposes NO ``_clients`` mapping — an
                # empty dict means "connected server list is
                # empty", not "look elsewhere".
                pass
            elif callable(getter):
                got = getter(server)
                if got is not None:
                    client = got
        if client is None:
            return None
        return (getattr(client, "functions", None) or {}).get(tool)
