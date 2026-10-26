"""Run the actual tool callable + fire post/failure hooks.

Isolates the sync/async result handling (``inspect.isawaitable``)
and the try/except that surrounds it. Fires
:attr:`HookEvent.POST_TOOL_USE` on success and
:attr:`HookEvent.POST_TOOL_USE_FAILURE` on exception, then
re-raises so Agno's exec chain sees the original error.

Kept intentionally slim — the tool-result post-processing that
appends rules-index content lives in :mod:`rules_suffixer`, not
here. This class' single job is "call the func, fire the right
event, return / raise" — plus the one thing only it can do:
deliver the PostToolUse hook's ``systemMessage``, which has no
other route back to the model once the call has already run.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from ember_code.core.hooks.events import HookEvent
from ember_code.core.hooks.hook_firer import HookFirer
from ember_code.core.hooks.permission_pipeline import ToolCallContext
from ember_code.core.hooks.tool_events import (
    PostToolUseFailurePayload,
    PostToolUsePayload,
)
from ember_code.core.hooks.untrusted import should_wrap, wrap_untrusted

logger = logging.getLogger(__name__)


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

        post = await self._firer.fire(
            HookEvent.POST_TOOL_USE,
            ctx.name,
            PostToolUsePayload.from_result(ctx.name, ctx.args, result),
        )
        # Quarantine external/attacker-influenceable tool output as untrusted
        # data (prompt-injection defense) BEFORE any trusted, igni-generated
        # annotation is appended, so the hook message / rules suffix land OUTSIDE
        # the block. String results from external-content tools only — igni's own
        # control tools return guidance the model is meant to act on.
        if isinstance(result, str) and should_wrap(ctx.name):
            result = wrap_untrusted(result)
        return self._with_hook_message(ctx, result, post.message)

    @staticmethod
    def _with_hook_message(ctx: ToolCallContext, result: Any, message: str) -> Any:
        """Append a PostToolUse hook's ``systemMessage`` to a string
        tool result.

        Until this existed the message was computed and thrown away.
        PostToolUse cannot block — the call has already run, so
        nothing reads ``should_continue`` — which left the annotated
        tool result as the hook's only channel back to the model, and
        it was not wired. A ``post-hook returns "reminder: run the
        linter"`` reached no one.

        Non-string results pass through untouched, the same rule
        :class:`RulesSuffixer` uses: a tool returning bytes or a dict
        must not be silently coerced to ``str`` by an advisory
        message. That drop is logged rather than hidden.
        """
        if not message:
            return result
        if not isinstance(result, str):
            logger.debug(
                "PostToolUse hook returned a message but %s's result is %s, "
                "not a string — dropping it",
                ctx.name,
                type(result).__name__,
            )
            return result
        return f"{result}\n\n<hook-message>\n{message.strip()}\n</hook-message>"
