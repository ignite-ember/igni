"""An MCP client session that puts igni's ``_meta`` on every tool call, and is busy during it.

Agno calls ``session.call_tool(name, arguments)`` with no ``meta``, and a
server that serves several callers needs to know which one a call is
for. The session is the one place every call passes through — Agno's
for the agent, igni's own for a page or a slash command — so the meta
goes on here, from a provider asked at the moment of each call.

The provider is the caller's to decide: the MCP layer does not know
what an extension or a principal is. What it returns is merged over
any meta the caller passed itself.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp import ClientSession, types

from ember_code.core.activity import ActivityRegistry, BusyKind

#: Asked on every tool call; ``None`` adds nothing.
RequestMeta = Callable[[], dict[str, Any] | None]


class RequestMetaSession(ClientSession):
    """A :class:`ClientSession` whose tool calls carry the provider's ``_meta``."""

    def __init__(
        self,
        *args: Any,
        request_meta: RequestMeta | None = None,
        busy_holder: str = "mcp",
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._request_meta = request_meta
        self._busy_holder = busy_holder

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *args: Any,
        meta: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> types.CallToolResult:
        added = self._request_meta() if self._request_meta is not None else None
        if added:
            meta = {**(meta or {}), **added}
        # Busy while the server works on it (``core/activity``).
        with ActivityRegistry.shared().hold(BusyKind.MCP_CALL, self._busy_holder, name):
            return await super().call_tool(name, arguments, *args, meta=meta, **kwargs)
