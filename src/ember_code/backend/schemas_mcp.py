"""Typed schemas for the MCP RPC surface.

Extracted from :mod:`ember_code.backend.server_mcp` so the panel
wire shapes live in one place instead of being hand-rolled as
raw ``list[dict]`` returns inside the controller. Sibling
convention: mirrors :mod:`ember_code.backend.schemas_panels` and
:mod:`ember_code.backend.schemas_hitl` — one file per top-level
concern, ``from_manager`` classmethods that own the domain-
object-to-wire projection.

Consumers:

* :class:`MCPToolToggleResult` — wire return for
  :meth:`McpController.set_tool_enabled`. Moved from the inline
  definition in ``server_mcp.py`` (Rule 1 offender — inline
  Pydantic when a sibling schemas file exists in the package).
* :class:`MCPServerSummary` — the two-field ``(name, connected)``
  row returned by :meth:`McpController.servers`. Replaces the
  raw ``list[dict]`` that the panel had to duck-type. Carries a
  sync :meth:`from_manager` factory.
* :class:`MCPServerSnapshot` — the full nine-field expanded-row
  panel payload returned by :meth:`McpController.server_details`.
  Replaces the raw ``list[dict]`` and encapsulates the projection
  that was inlined in the controller. Because two of the fields
  (``resources`` / ``prompts``) require an ``await`` on the
  manager, its :meth:`from_manager` factory is ``async``.

Field names on :class:`MCPServerSnapshot` are preserved
byte-for-byte from the previous raw-dict shape so
``clients/web/src/protocol/wire-schema.json`` and the
``wire-contract`` test continue to pass without a schema bump.
``resources`` / ``prompts`` were retyped from ``list[dict]`` to
``list[MCPResource]`` / ``list[MCPPrompt]`` — Pydantic serializes
them to the same field-for-field shape so the wire contract is
preserved.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from ember_code.core.mcp.schemas import MCPPrompt, MCPResource

if TYPE_CHECKING:
    from ember_code.core.mcp.client import MCPClientManager


class MCPToolToggleResult(BaseModel):
    """Wire shape for :meth:`McpController.set_tool_enabled` —
    the panel reads it back to confirm the row's new state without
    needing a second RPC roundtrip."""

    server: str
    tool: str
    enabled: bool


class MCPServerSummary(BaseModel):
    """Cheap two-field ``(name, connected)`` row returned by
    :meth:`McpController.servers`.

    Used when the panel just needs the connected-flag column
    without paying for the tools / resources / prompts fetch
    per row.
    """

    name: str
    connected: bool

    @classmethod
    def from_manager(cls, mgr: MCPClientManager, name: str) -> MCPServerSummary:
        """Project one manager entry into the wire summary."""
        return cls(name=name, connected=name in mgr.list_connected())


class MCPServerSnapshot(BaseModel):
    """Full expanded-row payload returned by
    :meth:`McpController.server_details`.

    Nine wire fields — preserved verbatim from the previous
    raw-dict shape:

    * ``name`` — server key from the config file.
    * ``connected`` — currently in ``mgr.list_connected()``.
    * ``transport`` — ``config.type`` (``stdio`` / ``sse``) or
      ``"unknown"`` if no config was found.
    * ``tool_names`` — every tool the server advertised at connect
      time (including individually-disabled ones).
    * ``tool_descriptions`` — ``{tool_name: description}``.
    * ``tools_disabled`` — tools the user has toggled off.
    * ``resources`` — MCP ``list_resources`` result (async fetch),
      typed as :class:`MCPResource` for the wire.
    * ``prompts`` — MCP ``list_prompts`` result (async fetch),
      typed as :class:`MCPPrompt` for the wire.
    * ``error`` — last connection error string, or ``""``.
    * ``policy_blocked`` — managed policy denies this server.
    """

    name: str
    connected: bool
    transport: str
    tool_names: list[str] = Field(default_factory=list)
    tool_descriptions: dict[str, str] = Field(default_factory=dict)
    tools_disabled: list[str] = Field(default_factory=list)
    resources: list[MCPResource] = Field(default_factory=list)
    prompts: list[MCPPrompt] = Field(default_factory=list)
    error: str = ""
    policy_blocked: bool = False

    @classmethod
    async def from_manager(
        cls,
        mgr: MCPClientManager,
        name: str,
        *,
        failures: dict[str, str] | None = None,
    ) -> MCPServerSnapshot:
        """Project one manager entry into the full panel snapshot.

        ``async`` because ``mgr.get_resources`` and
        ``mgr.get_prompts`` round-trip to the MCP session for
        connected servers. Disconnected servers skip both calls
        and get empty lists.

        ``failures`` is the session-scoped ``{name: reason}``
        cache built by
        :class:`~ember_code.core.session.startup.mcp.McpInitPhase`
        (and the ``/mcp`` command). When omitted the ``error``
        field is empty — matches the pre-refactor "no side
        channel" behaviour for callers that don't track connect
        failures.
        """
        config = mgr.configs.get(name)
        connected = name in mgr.list_connected()
        return cls(
            name=name,
            connected=connected,
            transport=(str(getattr(config.type, "value", config.type)) if config else "unknown"),
            tool_names=mgr.get_tools(name),
            tool_descriptions=mgr.get_tool_descriptions(name),
            tools_disabled=mgr.get_disabled_tools(name),
            resources=await mgr.get_resources(name) if connected else [],
            prompts=await mgr.get_prompts(name) if connected else [],
            error=(failures or {}).get(name, ""),
            policy_blocked=mgr.is_policy_denied(name),
        )
