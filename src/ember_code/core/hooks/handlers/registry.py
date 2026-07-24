"""Registry mapping ``HookType`` → :class:`HookHandler` instance.

Explicit-registration form (not ClassVar-based auto-registration
via subclass import order) — passing the four handlers into the
registry's ``__init__`` is a fragility-free source of truth and
lets tests substitute a fake handler without touching module-
level state.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from ember_code.core.hooks.handlers.base import HookHandler
from ember_code.core.hooks.handlers.command import CommandHookHandler
from ember_code.core.hooks.handlers.http import HttpHookHandler
from ember_code.core.hooks.handlers.mcp_tool import (
    MCPResolver,
    McpToolHookHandler,
)
from ember_code.core.hooks.handlers.prompt import PromptHookHandler
from ember_code.core.hooks.schemas import (
    HookDefinition,
    HookPayload,
    HookResult,
    HookType,
)

logger = logging.getLogger(__name__)


class HookHandlerRegistry:
    """Owns one instance per hook type.

    Constructor takes the dependencies needed by the concrete
    handlers (``mcp_resolver`` for :class:`McpToolHookHandler`,
    ``rewake_callback`` for :class:`CommandHookHandler`) and
    assembles the four instances explicitly. The internal
    ``_handlers`` map is instance state so tests can register a
    fake handler on an alternate registry without patching the
    module.
    """

    def __init__(
        self,
        *,
        mcp_resolver: MCPResolver | None = None,
        rewake_callback: Callable[[str], None] | None = None,
    ):
        handlers: list[HookHandler] = [
            CommandHookHandler(rewake_callback=rewake_callback),
            HttpHookHandler(),
            PromptHookHandler(),
            McpToolHookHandler(resolver=mcp_resolver),
        ]
        self._handlers: dict[HookType, HookHandler] = {h.handles: h for h in handlers}

    def register(self, handler: HookHandler) -> None:
        """Install (or replace) a handler for its ``handles`` type.

        Kept for test seams — production code doesn't need to
        register at runtime, but a subclass-with-side-effects
        that a test wants to observe can drop in here.
        """
        self._handlers[handler.handles] = handler

    def supports(self, hook_type: HookType | str) -> bool:
        """Whether a handler is registered for ``hook_type``.

        Lets the executor short-circuit the dispatch (log &
        return ``None``) BEFORE spawning a coroutine, so unknown
        types don't produce empty tasks.
        """
        return hook_type in self._handlers

    async def run(
        self,
        hook: HookDefinition,
        event: str,
        payload: HookPayload,
    ) -> HookResult | None:
        """Dispatch to the appropriate handler by ``hook.type``.

        Returns ``None`` for unknown types (silent skip — parity
        with the pre-refactor debug-log-and-continue).
        """
        handler = self._handlers.get(hook.type)
        if handler is None:
            logger.debug("Unknown hook type %r — skipping", hook.type)
            return None
        return await handler.run(hook, event, payload)
