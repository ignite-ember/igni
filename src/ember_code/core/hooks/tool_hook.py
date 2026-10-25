"""Tool event hook — fires PreToolUse/PostToolUse/PostToolUseFailure events.

Agno fires ``tool_hooks`` around every tool call. :class:`ToolEventHook`
is the async facade that Agno's ``aexecute`` chain sees. It
composes four small collaborators, each with one job:

* :class:`~ember_code.core.hooks.permission_pipeline.PermissionPipeline`
  — hooks + safety lists + evaluator, ordered gates that either
  green-light the call or return a block message.
* :class:`~ember_code.core.hooks.tool_call_invoker.ToolCallInvoker`
  — runs the callable (sync or async), fires PostToolUse /
  PostToolUseFailure, re-raises on error.
* :class:`~ember_code.core.hooks.rules_suffixer.RulesSuffixer`
  — appends any newly-discovered subdirectory rules to a string
  result and fires ``InstructionsLoaded``.
* :class:`~ember_code.core.hooks.hook_firer.HookFirer` — the
  uniform wire path every collaborator uses to actually reach
  the :class:`HookExecutor`.

Module-level ``_safe_args`` / ``_preview`` are compat shims —
``tests/test_tool_hook.py`` imports them by name. Each is a
two-line delegator to :class:`PayloadSanitizer`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agno.tools.function import ToolResult

from ember_code.core.config.permission_eval import PermissionEvaluator
from ember_code.core.hooks.agno_coroutine_marker import AgnoCoroutineMarker
from ember_code.core.hooks.events import HookEvent
from ember_code.core.hooks.executor import HookExecutor
from ember_code.core.hooks.extension_gate import (
    ExtensionHookGate,
    PostToolUseValue,
    PreToolUseValue,
)
from ember_code.core.hooks.hook_firer import HookFirer
from ember_code.core.hooks.payload_sanitizer import PayloadSanitizer
from ember_code.core.hooks.permission_pipeline import (
    PermissionPipeline,
    ToolCallContext,
)
from ember_code.core.hooks.rules_suffixer import RulesSuffixer
from ember_code.core.hooks.tool_call_invoker import ToolCallInvoker
from ember_code.core.utils.rules_index import RulesIndex

logger = logging.getLogger(__name__)

__all__ = ["ToolEventHook", "_safe_args", "_preview"]


class ToolEventHook:
    """Async Agno ``tool_hook`` — thin facade over the pipeline.

    ``__call__`` runs: :class:`PermissionPipeline` → (if allowed)
    :class:`ToolCallInvoker` → :class:`RulesSuffixer`. The three
    collaborators share a :class:`HookFirer`, so hook events flow
    through a single wire path.

    The constructor signature is preserved verbatim so
    ``ToolEventHookFactory`` (session/tool_hook_factory.py) does
    not have to move.
    """

    def __init__(
        self,
        executor: HookExecutor,
        session_id: str = "",
        protected_paths: list[str] | None = None,
        blocked_commands: list[str] | None = None,
        rules_index: RulesIndex | None = None,
        project_dir: Path | None = None,
        permission_evaluator: PermissionEvaluator | None = None,
        bypass_guard: Any | None = None,
    ):
        # Agno's ``aexecute`` checks ``inspect.iscoroutinefunction``
        # — mark this callable-instance so the async path is used.
        AgnoCoroutineMarker.mark(self)

        self._session_id = session_id
        firer = HookFirer(executor, session_id)
        self._pipeline = PermissionPipeline(
            firer=firer,
            protected_paths=protected_paths or [],
            blocked_commands=blocked_commands or [],
            permission_evaluator=permission_evaluator,
            bypass_guard=bypass_guard,
        )
        self._invoker = ToolCallInvoker(firer)
        self._suffixer = RulesSuffixer(
            rules_index=rules_index,
            project_dir=project_dir,
            firer=firer,
        )

    async def __call__(
        self,
        name: str = "",
        func: Callable | None = None,
        args: dict[str, Any] | None = None,
        agent: Any = None,
        **kwargs: Any,
    ) -> Any:
        """Agno-owned signature — ``**kwargs`` absorbs any extra
        keys Agno's dispatch may add. Internally we immediately
        pack the call into a typed :class:`ToolCallContext` so
        every collaborator downstream is on the typed side of
        the seam.
        """
        ctx = ToolCallContext(
            name=name,
            func=func,
            args=args or {},
            agent=agent,
        )
        # Extensions' PreToolUse hooks are asked first, with the real
        # arguments, so igni's own checks — the configured PreToolUse
        # hooks among them — run on whatever they leave.
        pre = await ExtensionHookGate.ask(
            PreToolUseValue(session_id=self._session_id, tool_name=name, tool_args=ctx.args),
            HookEvent.PRE_TOOL_USE,
        )
        if not pre.allowed:
            return f"Blocked: {pre.reason}"
        ctx = ctx.with_args(pre.value.get("tool_args", ctx.args))
        block_message = await self._pipeline.evaluate(ctx)
        if block_message is not None:
            return block_message
        try:
            result = await self._invoker.run(ctx)
        except Exception as exc:
            ExtensionHookGate.tool_called(name, answered=False, detail=str(exc))
            raise
        unanswered = self._unanswered(result)
        ExtensionHookGate.tool_called(name, answered=unanswered is None, detail=unanswered or "")
        result = await self._after(ctx, result)
        return await self._suffixer.enrich(ctx, result)

    async def _after(self, ctx: ToolCallContext, result: Any) -> Any:
        """Extensions' PostToolUse hooks: what the model reads of a result.

        A reject withholds the result — the model is told it was
        withheld, and why. A change replaces it; only a text result (or
        an MCP tool's text content) can be changed, so for any other
        kind a change is refused and the result goes through as it is.
        """
        if not ExtensionHookGate.hooked(HookEvent.POST_TOOL_USE):
            return result
        text = self._text_of(result)
        post = await ExtensionHookGate.ask(
            PostToolUseValue(
                session_id=self._session_id,
                tool_name=ctx.name,
                tool_args=ctx.args,
                result=text if text is not None else str(result),
            ),
            HookEvent.POST_TOOL_USE,
        )
        if not post.allowed:
            return f"Withheld: {post.reason}"
        changed = post.value.get("result")
        if text is None:
            if changed != str(result):
                logger.warning(
                    "an extension changed %s's result, which is not text; it is not applied",
                    ctx.name,
                )
            return result
        if changed == text:
            return result
        if isinstance(result, ToolResult):
            return result.model_copy(update={"content": changed})
        return changed

    @staticmethod
    def _text_of(result: Any) -> str | None:
        """A result as the text the model reads, when it is text."""
        if isinstance(result, str):
            return result
        if isinstance(result, ToolResult) and isinstance(result.content, str):
            return result.content
        return None

    @staticmethod
    def _unanswered(result: Any) -> str | None:
        """Why an MCP server did not answer a call, or ``None`` when it did.

        Agno turns a call that raised — a timeout, a closed connection —
        into a result reading ``Error: …``, where a tool that reported an
        error of its own reads ``Error from MCP tool …``: that one
        answered.
        """
        if not isinstance(result, ToolResult) or not isinstance(result.content, str):
            return None
        if result.content.startswith("Error: "):
            return result.content.removeprefix("Error: ")
        return None


# ── Backwards-compat module-level shims ─────────────────────────────
#
# ``tests/test_tool_hook.py`` imports these names directly. Each
# is a two-line delegator to :class:`PayloadSanitizer` — the
# audit's "utility-module-of-related-helpers" offender for
# protected-paths / blocked-commands is fully re-homed onto
# :class:`ProtectedPathStage` / :class:`BlockedCommandStage`;
# their tests call the OOP surface directly.


def _safe_args(args: dict[str, Any]) -> dict[str, str]:
    """Compat shim — delegates to :meth:`PayloadSanitizer.safe_args`."""
    return PayloadSanitizer.safe_args(args)


def _preview(result: Any) -> str:
    """Compat shim — delegates to :meth:`PayloadSanitizer.preview`."""
    return PayloadSanitizer.preview(result)
