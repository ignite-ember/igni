"""``mcp_tool`` hook handler — invokes an MCP server tool with
``{event, payload, ...mcp_args}`` and translates the tool's
return into a :class:`HookResult`.

Missing MCP resolver, unknown server / tool, invoker exceptions
and timeouts all degrade to non-blocking. For firm gating, use
a ``command`` hook with exit 2.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ember_code.core.hooks.envelope import HookEnvelope
from ember_code.core.hooks.handlers.base import HookHandler
from ember_code.core.hooks.schemas import (
    HookDefinition,
    HookPayload,
    HookResult,
    HookType,
)

logger = logging.getLogger(__name__)


# Signature: ``(server, tool) -> callable | None``. The callable
# returned is the MCP tool's invoker — sync or async — taking
# keyword args and returning the tool's result. ``None`` means
# "server or tool not connected"; the handler degrades to a
# non-blocking result in that case.
MCPResolver = Callable[
    [str, str],
    "Callable[..., Any] | Awaitable[Any] | None",
]


class McpInvocationArgs(BaseModel):
    """Kwargs shape sent to an MCP tool invoker.

    Wraps the pre-refactor ``{"event": ..., "payload": ..., **hook.mcp_args}``
    splat so the composition rule (event/payload + static
    mcp_args from settings) has a name and a test surface.
    """

    model_config = ConfigDict(extra="allow")

    event: str
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_kwargs(self) -> dict[str, Any]:
        """Flat kwargs dict for ``invoker(**kwargs)``."""
        return self.model_dump()


class McpToolHookHandler(HookHandler):
    """MCP-tool-invoking hook handler."""

    handles: ClassVar[HookType] = "mcp_tool"

    def __init__(self, resolver: MCPResolver | None = None):
        # Injected closure: given ``(server, tool)`` returns the
        # tool's invoker (or None). Keeps the handler ignorant of
        # the MCP manager's internals — trivial to mock in tests.
        self._resolver = resolver

    async def run(
        self,
        hook: HookDefinition,
        event: str,
        payload: HookPayload,
    ) -> HookResult:
        if self._resolver is None:
            logger.debug("mcp_tool hook fired but no MCP resolver wired")
            return self._non_blocking()
        try:
            invoker = self._resolver(hook.mcp_server, hook.mcp_tool)
        except Exception as exc:
            logger.debug(
                "MCP resolver raised for %s/%s: %s",
                hook.mcp_server,
                hook.mcp_tool,
                exc,
            )
            return self._non_blocking()
        if invoker is None:
            logger.debug(
                "mcp_tool %s/%s not connected — skipping hook",
                hook.mcp_server,
                hook.mcp_tool,
            )
            return self._non_blocking()
        # ``MCPResolver`` unions Callable + Awaitable + None
        # defensively. Real resolvers always return a callable —
        # narrow here so mypy is happy and a resolver that ever
        # returns a bare coroutine degrades gracefully.
        if not callable(invoker):
            logger.debug(
                "mcp_tool %s/%s resolver returned non-callable — skipping",
                hook.mcp_server,
                hook.mcp_tool,
            )
            return self._non_blocking()
        try:
            call_args = McpInvocationArgs(
                event=event,
                payload=payload.to_wire_dict(),
                **hook.mcp_args,
            )
            timeout_secs = hook.timeout / 1000
            result = invoker(**call_args.to_kwargs())
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=timeout_secs)
        except asyncio.TimeoutError:
            return self._non_blocking("MCP tool hook timed out")
        except Exception as exc:
            logger.debug(
                "MCP tool hook %s/%s failed: %s",
                hook.mcp_server,
                hook.mcp_tool,
                exc,
            )
            return self._non_blocking()
        envelope = HookEnvelope.from_raw(result)
        if envelope is None:
            # Non-dict return — stringify into ``message`` so the
            # agent still sees the MCP tool's payload. ``None``
            # becomes an empty message; str/list/int stringify.
            if result is None:
                return self._non_blocking()
            return self._non_blocking(str(result))
        return envelope.to_result()
