"""Message-handling pipeline for :class:`Session`.

Extracted from :mod:`ember_code.core.session.core` — the six
methods that own the headless message path:

* :meth:`SessionMessageHandler.handle` — the public entry.
* :meth:`_check_user_prompt_hook` — pre-turn hook / audit.
* :meth:`_guardrail_prefix` — inform-don't-block warning.
* :meth:`_build_effective_message` — reminders + datetime +
  guardrail prefix.
* :meth:`_retry_on_stop_hook_block` — post-turn Stop hook
  with up to 3 re-generates.
* :meth:`_handle_run_failure` — audit + StopFailure hook +
  formatted error return.

Constructor takes explicit collaborators — no reach-back
into :class:`Session`. ``team_ref`` is a **callable**, not the
live agent — the session rebuilds ``main_team`` under
compact / plugin-reload / MCP-rebuild / codeindex-refresh, so
storing a bare reference would go stale.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from ember_code.core.guardrails.runner import GuardrailRunner
from ember_code.core.hooks.events import HookEvent
from ember_code.core.hooks.executor import HookExecutor
from ember_code.core.session.schemas import (
    McpInitResult,
    StopFailureHookPayload,
    StopHookPayload,
    UserPromptSubmitHookPayload,
)
from ember_code.core.utils.audit import AuditEntry, AuditLogger
from ember_code.core.utils.display import DisplayManager
from ember_code.core.utils.response import (
    extract_response_text as _default_extract_response_text,
)

logger = logging.getLogger(__name__)


class SessionMessageHandler:
    """Owns the six-step message pipeline for one Session instance.

    The pipeline (in :meth:`handle` order):

    1. Ensure MCP servers are connected (deferred to caller).
    2. Fire ``UserPromptSubmit`` hook → maybe block the turn.
    3. Run guardrails → maybe prepend a warning prefix.
    4. Assemble the effective message (reminders + datetime).
    5. Dispatch to the team; capture the response.
    6. Fire ``Stop`` hook, retrying up to 3× on block.
    7. Auto-compact if the run pushed us over 80% context.
    """

    _STOP_HOOK_MAX_RETRIES = 3
    _STOP_HOOK_RESPONSE_PREVIEW = 500

    def __init__(
        self,
        *,
        hook_executor: HookExecutor,
        audit: AuditLogger,
        display: DisplayManager,
        guardrail_runner: GuardrailRunner,
        team_ref: Callable[[], Any],
        pending_reminders_drain: Callable[[], list[str]],
        compact_hook: Callable[[int, int], Awaitable[bool]],
        ensure_mcp: Callable[[], Awaitable[McpInitResult]],
        session_id: str,
        context_window: int,
        latch_input_tokens: Callable[[int], None],
        extract_response_text: Callable[[Any], str] | None = None,
    ) -> None:
        self._hook_executor = hook_executor
        self._audit = audit
        self._display = display
        self._guardrail_runner = guardrail_runner
        self._team_ref = team_ref
        self._drain_reminders = pending_reminders_drain
        self._compact_hook = compact_hook
        self._ensure_mcp = ensure_mcp
        self._session_id = session_id
        self._context_window = context_window
        self._latch_input_tokens = latch_input_tokens
        # Extractor injection — Session passes its own module's
        # ``extract_response_text`` binding so
        # ``patch("ember_code.core.session.core.extract_response_text")``
        # in tests still intercepts.
        self._extract_response_text = extract_response_text or _default_extract_response_text

    async def handle(self, message: str, **media_kwargs: Any) -> str:
        """Handle a single user message and return the response.

        Accepts optional media keyword arguments (images, audio,
        videos, files) which are forwarded directly to ``team.arun``.
        """
        await self._ensure_mcp()

        blocked = await self._check_user_prompt_hook(message)
        if blocked is not None:
            return blocked

        guardrail_prefix = await self._guardrail_prefix(message)
        effective_message = self._build_effective_message(message, guardrail_prefix)

        try:
            team = self._team_ref()
            response = await team.arun(effective_message, stream=False, **media_kwargs)
            response_text = self._extract_response_text(response)

            self._audit.log(
                AuditEntry.success(
                    session_id=self._session_id,
                    agent_name="ember",
                    tool_name="main_team",
                )
            )

            response_text = await self._retry_on_stop_hook_block(response_text)

            # Compact history if approaching context limit.
            metrics = getattr(getattr(team, "run_response", None), "metrics", None)
            if metrics:
                input_tokens = getattr(metrics, "input_tokens", 0) or 0
                self._latch_input_tokens(input_tokens)
                await self._compact_hook(input_tokens, self._context_window)

            return response_text

        except Exception as exc:
            return await self._handle_run_failure(exc)

    async def _check_user_prompt_hook(self, message: str) -> str | None:
        """Fire the ``UserPromptSubmit`` hook. Returns the blocked
        message when the hook denies, ``None`` when the turn should
        proceed. Blocked turns emit an audit entry so a policy denial
        is traceable — otherwise the user sees "blocked" with no
        record of why."""
        payload = UserPromptSubmitHookPayload(
            message=message,
            session_id=self._session_id,
        )
        hook_result = await self._hook_executor.execute(
            event=HookEvent.USER_PROMPT_SUBMIT.value,
            payload=payload.model_dump(),
        )
        if hook_result.should_continue:
            return None
        blocked_msg = hook_result.message or "Blocked by UserPromptSubmit hook."
        self._audit.log(
            AuditEntry.blocked(
                session_id=self._session_id,
                agent_name="session",
                tool_name="user_prompt",
                reason=blocked_msg,
            )
        )
        return blocked_msg

    async def _guardrail_prefix(self, message: str) -> str:
        """Run guardrail checks and produce a warning prefix (empty
        when disabled or all clean). Guardrails inform, don't block —
        the prefix is prepended to the effective message so the model
        sees the caveat before the user text.
        """
        if not self._guardrail_runner.enabled:
            return ""
        gr_results = await self._guardrail_runner.check(message)
        if not gr_results:
            return ""
        warnings = "; ".join(r.message for r in gr_results)
        logger.info("Guardrails triggered: %s", warnings)
        return (
            f"[GUARDRAIL WARNING] The following issues were detected in "
            f"the user message: {warnings}\n"
            f"Please be cautious and do not repeat or use any flagged content.\n\n"
        )

    def _build_effective_message(self, message: str, guardrail_prefix: str) -> str:
        """Assemble the message the model actually sees: any queued
        ``asyncRewake`` reminders (drained one-shot), a
        ``<system-context>`` datetime hint, and the guardrail prefix
        if any."""
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
        reminders = self._drain_reminders()
        reminders_block = ""
        if reminders:
            joined = "\n".join(reminders)
            reminders_block = f"<system-reminder>{joined}</system-reminder>\n"
        effective = (
            f"{reminders_block}"
            f"<system-context>Current datetime: {timestamp}</system-context>\n{message}"
        )
        if guardrail_prefix:
            effective = guardrail_prefix + effective
        return effective

    async def _retry_on_stop_hook_block(self, response_text: str) -> str:
        """Fire the ``Stop`` hook up to 3 times; feed rejection
        messages back to the agent to re-generate the response.

        A Stop hook that returns ``should_continue=False`` treats
        the agent's response as unacceptable — its ``message``
        becomes a critique the agent should address. We re-run
        the agent with that critique as a system message, then
        fire the hook again on the new response. Bounded at 3
        attempts so a persistently-rejecting hook doesn't loop
        forever. On the third failure we accept the response
        (the hook can still deny at a later stage if it's a
        hard-block invariant).
        """
        for _stop_attempt in range(self._STOP_HOOK_MAX_RETRIES):
            payload = StopHookPayload(
                session_id=self._session_id,
                response=response_text[: self._STOP_HOOK_RESPONSE_PREVIEW],
            )
            stop_result = await self._hook_executor.execute(
                event=HookEvent.STOP.value,
                payload=payload.model_dump(),
            )
            if stop_result.should_continue:
                break
            feedback = stop_result.message or "Response blocked by Stop hook."
            system_msg = (
                f"[SYSTEM] Your previous response was rejected by a Stop hook: "
                f"{feedback}\nPlease revise your response to address this issue."
            )
            team = self._team_ref()
            response = await team.arun(system_msg, stream=False)
            response_text = self._extract_response_text(response)
        return response_text

    async def _handle_run_failure(self, exc: Exception) -> str:
        """Common failure path: audit log, StopFailure hook fire
        (observation-only), formatted error string return.

        The StopFailure hook mirrors the Stop hook on the happy
        path — same payload shape family, same non-blocking
        semantics — so plugins can observe both success and
        failure with a single subscription pair.
        """
        error_msg = f"Error handling message: {exc}"
        self._display.print_error(error_msg)

        self._audit.log(
            AuditEntry.error(
                session_id=self._session_id,
                agent_name="session",
                tool_name="main_team",
                error=str(exc),
                error_type=type(exc).__name__,
            )
        )

        with contextlib.suppress(Exception):
            payload = StopFailureHookPayload(
                session_id=self._session_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            await self._hook_executor.execute(
                event=HookEvent.STOP_FAILURE.value,
                payload=payload.model_dump(),
            )
        return error_msg
