"""Typed wire-shape models for the MCP subsystem.

Matches the sibling ``schemas.py`` convention used by
:mod:`ember_code.core.init`, :mod:`ember_code.core.evals`,
:mod:`ember_code.core.auth`, :mod:`ember_code.core.agents`,
:mod:`ember_code.core.lsp`, :mod:`ember_code.core.hooks`, and
:mod:`ember_code.core.session` — every subsystem's wire shapes
live in one file with ``from_sdk`` / ``from_manager``
classmethods that own the domain-object-to-wire projection.

Contents:

* :class:`MCPResource` / :class:`MCPPrompt` — Pydantic wrappers
  around the MCP SDK's raw resource / prompt objects. Replaces
  the ``list[dict[str, str]]`` return type that
  :meth:`MCPClientManager.get_resources` /
  :meth:`~MCPClientManager.get_prompts` used to hand back
  (Rule 1 offender — raw dicts across the RPC boundary).
* :class:`MCPToolInfo` — packages a tool's name, description
  and enabled state into one wire object so
  :class:`~ember_code.backend.schemas_mcp.MCPServerSnapshot`
  stops stitching three separate manager calls
  (``get_tools`` + ``get_tool_descriptions`` +
  ``get_disabled_tools``) at projection time.
* :class:`MCPConnectResult` — explicit Result type returned by
  :meth:`MCPClientManager.connect`. Replaces the pre-refactor
  ``Any | None`` return + ``self._errors`` side-channel +
  empty-string sentinel (Pattern 3 offender).

The ``from_sdk`` classmethods on :class:`MCPResource` and
:class:`MCPPrompt` accept the MCP Python SDK's raw objects
(``mcp.types.Resource``, ``mcp.types.Prompt``) and coerce
their camelCase fields (``mimeType``) into snake_case
(``mime_type``) so the wire shape stays PEP 8-consistent
with every other :class:`BaseModel` in the codebase.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from agno.tools.mcp import MCPTools


class MCPResource(BaseModel):
    """Wire shape for one MCP resource entry.

    Fields are the four columns the panel actually renders —
    ``uri`` is the only required identity field, the rest
    default to ``""`` so servers that omit optional metadata
    still serialize cleanly.
    """

    uri: str
    name: str = ""
    description: str = ""
    mime_type: str = ""

    @classmethod
    def from_sdk(cls, resource: Any) -> MCPResource:
        """Project an ``mcp.types.Resource`` SDK object into the
        wire model. Coerces the SDK's camelCase ``mimeType`` to
        the snake-case ``mime_type`` field and tolerates ``None``
        on any optional attribute."""
        return cls(
            uri=str(getattr(resource, "uri", "") or ""),
            name=getattr(resource, "name", "") or "",
            description=getattr(resource, "description", "") or "",
            mime_type=getattr(resource, "mimeType", "") or "",
        )


class MCPPrompt(BaseModel):
    """Wire shape for one MCP prompt entry.

    ``arguments`` is a flat ``list[str]`` of argument names —
    the panel only needs the identity list, not the full
    argument metadata. If a future consumer needs the type /
    required-flag columns, add fields here and update
    :meth:`from_sdk` to project them.
    """

    name: str
    description: str = ""
    arguments: list[str] = Field(default_factory=list)

    @classmethod
    def from_sdk(cls, prompt: Any) -> MCPPrompt:
        """Project an ``mcp.types.Prompt`` SDK object into the
        wire model. Flattens the ``prompt.arguments`` list of
        argument objects to their ``.name`` strings."""
        raw_args = getattr(prompt, "arguments", None) or []
        return cls(
            name=getattr(prompt, "name", "") or "",
            description=getattr(prompt, "description", "") or "",
            arguments=[getattr(a, "name", "") or "" for a in raw_args],
        )


class MCPToolInfo(BaseModel):
    """Packaged wire shape for one MCP tool row.

    Replaces the ``get_tools`` + ``get_tool_descriptions`` +
    ``get_disabled_tools`` triple-call composition that
    :class:`~ember_code.backend.schemas_mcp.MCPServerSnapshot`
    used to perform at projection time. A single manager call
    (:meth:`MCPClientManager.list_tool_info`) now hands back
    a list of these — one row per tool — with the three
    columns pre-joined.
    """

    name: str
    description: str = ""
    enabled: bool = True


class MCPConnectResult(BaseModel):
    """Explicit Result type returned by
    :meth:`MCPClientManager.connect`.

    Replaces the pre-refactor ``Any | None`` + ``self._errors``
    side-channel with a single value that carries success /
    failure and the reason together (Pattern 3 offender fix).

    * ``ok`` — ``True`` on successful connect.
    * ``client`` — the live ``agno.tools.mcp.MCPTools`` handle
      on success, or ``None`` on failure. Excluded from
      :meth:`model_dump` so a serialization pass never walks
      the live subprocess-owning handle.
    * ``reason`` — human-readable failure explanation on
      ``ok=False``; empty string on success.

    Use :meth:`success` / :meth:`failure` classmethod
    constructors so every failure branch in
    :meth:`~MCPClientManager.connect` reads as
    ``return MCPConnectResult.failure("...")`` and callers
    can't accidentally construct an ``ok=True`` Result with a
    non-empty ``reason``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    ok: bool
    client: Any = Field(default=None, exclude=True)
    reason: str = ""

    @classmethod
    def success(cls, client: MCPTools) -> MCPConnectResult:
        """Build a ``ok=True`` Result carrying the live MCPTools
        handle. Callers pull the client via ``result.client``."""
        return cls(ok=True, client=client, reason="")

    @classmethod
    def failure(cls, reason: str) -> MCPConnectResult:
        """Build a ``ok=False`` Result carrying only the reason
        string. The client is left as ``None`` so callers can
        safely branch on ``result.ok`` before touching
        ``result.client``."""
        return cls(ok=False, client=None, reason=reason)
