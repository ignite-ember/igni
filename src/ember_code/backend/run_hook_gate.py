"""Fires the UserPromptSubmit + Stop hooks for a run.

Extracted out of two spots in the old ``server_run.py``:

* The ``UserPromptSubmit`` fire before ``team.arun`` (blocks the
  run when the hook says ``should_continue=False``).
* The ``Stop`` fire after the natural end-of-run.

Consolidates the ``hook_executor.execute(event=..., payload={...})``
+ ``.should_continue`` + ``.message`` handling behind typed
:class:`HookGateResult` return values so the RunController body
reads as ``result = await self._hooks.fire_user_prompt_submit(...)``.
"""

from __future__ import annotations

from ember_code.backend.schemas_run import (
    HookGateResult,
    StopHookPayload,
    UserPromptSubmitPayload,
)
from ember_code.core.hooks.events import HookEvent
from ember_code.core.hooks.executor import HookExecutor


class RunHookGate:
    """Fire pre-run / post-run hooks and translate their result
    into a typed :class:`HookGateResult`.

    Constructor takes the hook executor + session_id via
    composition — no reach-back into a backend attribute."""

    def __init__(self, hook_executor: HookExecutor, session_id: str) -> None:
        self._hook_executor = hook_executor
        self._session_id = session_id

    async def fire_user_prompt_submit(self, text: str) -> HookGateResult:
        """Fire the ``UserPromptSubmit`` hook and package its
        verdict.

        Returns:
            :class:`HookGateResult` — ``should_continue=False`` means
            the caller must yield an ``Error(text=block_message)``
            and bail before ``team.arun``. ``context_message`` (when
            set with ``should_continue=True``) is the string to
            append as a ``<hook-context>`` block onto the prompt.
        """
        payload = UserPromptSubmitPayload(message=text, session_id=self._session_id).model_dump()
        hook_result = await self._hook_executor.execute(
            event=HookEvent.USER_PROMPT_SUBMIT.value,
            payload=payload,
        )
        if not hook_result.should_continue:
            return HookGateResult(
                should_continue=False,
                block_message=hook_result.message or "Message blocked by hook.",
            )
        return HookGateResult(
            should_continue=True,
            context_message=hook_result.message or None,
        )

    async def fire_stop(self) -> HookGateResult:
        """Fire the ``Stop`` hook after the natural end-of-run.

        Returns:
            :class:`HookGateResult` — when the hook returns a message
            with ``should_continue=False``, the caller yields an
            ``Info(text=block_message)`` so the FE can surface any
            post-run guidance. Otherwise the message is silently
            dropped (matches the pre-refactor free-function
            behavior).
        """
        payload = StopHookPayload(session_id=self._session_id).model_dump()
        hook_result = await self._hook_executor.execute(
            event=HookEvent.STOP.value,
            payload=payload,
        )
        if hook_result.message and not hook_result.should_continue:
            return HookGateResult(
                should_continue=False,
                block_message=hook_result.message,
            )
        return HookGateResult(should_continue=True)
