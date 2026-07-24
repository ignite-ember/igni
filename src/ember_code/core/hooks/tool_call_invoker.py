"""Run the actual tool callable + fire post/failure hooks.

Isolates the sync/async result handling (``inspect.isawaitable``)
and the try/except that surrounds it. Fires
:attr:`HookEvent.POST_TOOL_USE` on success and
:attr:`HookEvent.POST_TOOL_USE_FAILURE` on exception, then
re-raises so Agno's exec chain sees the original error.

Kept intentionally slim — the tool-result post-processing that
appends rules-index content lives in :mod:`rules_suffixer`, not
here. This class' single job is "call the func, fire the right
event, return / raise".
"""

from __future__ import annotations

import inspect
from typing import Any

from ember_code.core.hooks.events import HookEvent
from ember_code.core.hooks.hook_firer import HookFirer
from ember_code.core.hooks.permission_pipeline import ToolCallContext
from ember_code.core.hooks.tool_events import (
    PostToolUseFailurePayload,
    PostToolUsePayload,
)


class ToolCallInvoker:
    """Invoke ``ctx.func(**ctx.args)`` with post-event bracketing."""

    def __init__(self, firer: HookFirer) -> None:
        self._firer = firer

    async def run(self, ctx: ToolCallContext) -> Any:
        """Execute the tool, fire PostToolUse / PostToolUseFailure,
        return the raw result (or re-raise). Callers pass the
        return value to :class:`RulesSuffixer` for optional post
        processing.
        """
        if ctx.func is None:
            return None

        error: BaseException | None = None
        result: Any = None
        try:
            result = ctx.func(**ctx.args)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            error = exc

        if error is not None:
            await self._firer.fire(
                HookEvent.POST_TOOL_USE_FAILURE,
                ctx.name,
                PostToolUseFailurePayload.from_exception(ctx.name, ctx.args, error),
            )
            raise error

        await self._firer.fire(
            HookEvent.POST_TOOL_USE,
            ctx.name,
            PostToolUsePayload.from_result(ctx.name, ctx.args, result),
        )
        return result
