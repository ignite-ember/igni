"""Per-tool enable/disable filter for MCP client tools.

Extracted from :class:`MCPClientManager` so the manager stays
focused on connection lifecycle and this file owns the cohesive
cluster of three fields (``_original_functions``,
``_disabled_tools``, ``_tool_state``) plus every method that
touches them (Pattern 4 + Pattern 8 fix — the manager's method
count drops below the soft cap once these move here).

The filter mutates Agno's live :class:`MCPTools.functions`
dict in place: to disable a tool we remove its entry, to
re-enable we restore from :attr:`_original_functions`
(snapshotted at connect time so a re-enable never triggers a
reconnect). Persistence rides on the composed
:class:`MCPToolStateStore`; the in-memory apply logic lives
here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ember_code.core.mcp.schemas import MCPToolInfo
from ember_code.core.mcp.tool_state import MCPToolStateStore

if TYPE_CHECKING:
    from agno.tools.mcp import MCPTools

logger = logging.getLogger(__name__)


class MCPToolFilter:
    """In-memory + on-disk state for per-tool enable/disable.

    Composes a :class:`MCPToolStateStore` for persistence and
    owns two in-memory dicts:

    * :attr:`_original_functions` — ``{server: {tool: func}}``
      snapshot taken at connect time. We restore from here
      whenever the disabled set changes, so a re-enabled tool
      comes back without a reconnect handshake.
    * :attr:`_disabled_tools` — ``{server: set[tool]}`` for the
      user's current per-tool toggle state, seeded from the
      store on construction.
    """

    def __init__(self, tool_state: MCPToolStateStore):
        self._tool_state = tool_state
        self._original_functions: dict[str, dict[str, Any]] = {}
        self._disabled_tools: dict[str, set[str]] = tool_state.load()

    def snapshot(self, name: str, client: MCPTools) -> None:
        """Cache the full ``client.functions`` dict for ``name``.

        Called by :class:`MCPClientManager` right after a
        successful connect. The snapshot is what
        :meth:`apply` restores from when a disabled tool is
        re-enabled.
        """
        funcs = getattr(client, "functions", None)
        if isinstance(funcs, dict):
            self._original_functions[name] = dict(funcs)

    def apply(self, name: str, client: MCPTools | None) -> None:
        """Filter the live ``client.functions`` dict to hide
        disabled tools from the agent.

        Re-applies the full original set first so a
        previously-disabled tool that's been re-enabled comes
        back. No-op if there's no live client or the client's
        ``functions`` attribute isn't a mutable dict.
        """
        if client is None:
            return
        live = getattr(client, "functions", None)
        if not isinstance(live, dict):
            return
        # Lazily snapshot the original set on first use — covers
        # servers that were connected before this code path existed.
        if name not in self._original_functions:
            self._original_functions[name] = dict(live)
        original = self._original_functions[name]
        disabled = self._disabled_tools.get(name, set())
        live.clear()
        for fname, func in original.items():
            if fname not in disabled:
                live[fname] = func

    def set_enabled(
        self,
        server: str,
        tool: str,
        enabled: bool,
        client: MCPTools | None,
    ) -> None:
        """Toggle a single tool. Persists state via the composed
        store and re-applies the filter to ``client`` so the
        change is visible to the next agent turn.

        ``client`` is passed in (rather than looked up here) so
        the filter never needs a back-reference to the manager's
        ``_clients`` dict — the manager keeps ownership of that
        lookup and only hands the resolved handle to the filter.
        """
        disabled = self._disabled_tools.setdefault(server, set())
        if enabled:
            disabled.discard(tool)
        else:
            disabled.add(tool)
        if not disabled:
            self._disabled_tools.pop(server, None)
        self._tool_state.save(self._disabled_tools)
        self.apply(server, client)

    def list_disabled(self, name: str) -> list[str]:
        """Sorted list of tools the user has individually
        disabled on server ``name``."""
        return sorted(self._disabled_tools.get(name, set()))

    def list_tools(self, name: str, client: MCPTools | None) -> list[str]:
        """Every tool name the server exposed at connect time,
        including individually-disabled ones. Panels use this to
        render disabled rows with a toggle-off state."""
        funcs = self._original_functions.get(name)
        if funcs:
            return list(funcs.keys())
        if client is None:
            return []
        return list((getattr(client, "functions", None) or {}).keys())

    def tool_descriptions(self, name: str, client: MCPTools | None) -> dict[str, str]:
        """Return ``{tool_name: description}`` for one server.

        Prefers the connect-time snapshot so disabled tools
        keep their descriptions in the panel; falls back to
        the live client for servers connected before this
        code path existed.
        """
        funcs = self._original_functions.get(name)
        if funcs:
            return {
                fname: (getattr(func, "description", "") or "") for fname, func in funcs.items()
            }
        if client is None:
            return {}
        functions = getattr(client, "functions", None) or {}
        return {
            fname: (getattr(func, "description", "") or "")
            for fname, func in functions.items()
            if hasattr(func, "description")
        }

    def list_tool_info(self, name: str, client: MCPTools | None) -> list[MCPToolInfo]:
        """Package name+description+enabled for every tool on
        server ``name`` into one typed list.

        The one-call replacement for the
        ``get_tools`` + ``get_tool_descriptions`` +
        ``get_disabled_tools`` three-way stitch that
        :class:`~ember_code.backend.schemas_mcp.MCPServerSnapshot`
        used to perform at projection time.
        """
        names = self.list_tools(name, client)
        descs = self.tool_descriptions(name, client)
        disabled = self._disabled_tools.get(name, set())
        return [
            MCPToolInfo(
                name=tool,
                description=descs.get(tool, ""),
                enabled=tool not in disabled,
            )
            for tool in names
        ]

    def forget(self, name: str) -> None:
        """Drop the connect-time snapshot for ``name`` — called
        by the manager on disconnect so a fresh connect starts
        clean instead of restoring stale function objects."""
        self._original_functions.pop(name, None)
