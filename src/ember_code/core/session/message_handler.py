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
compact / plugin-reload / MCP-rebuild / prompt-refresh, so
storing a bare reference would go stale.

Same reasoning for ``session_id_ref`` and ``context_window_ref``:
both were captured by value and both went stale silently. ``/clear``
and ``/fork`` rotate the session id (``SessionIdentity.rotate``) and
``/model`` swaps the model's context window, and a handler holding
the construction-time copies kept stamping hook payloads and audit
rows with the dead id and thresholding compaction against the
previous model's window. Anything the session can change under us is
read through a callable, not copied.
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
from ember_code.core.hooks.extension_gate import (
    ExtensionHookGate,
    StopValue,
    UserPromptSubmitValue,
)
from ember_code.core.paths import MAIN_AGENT_NAME
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
        session_id_ref: Callable[[], str],
        context_window_ref: Callable[[], int],
        latch_input_tokens: Callable[[int], None],
        extract_response_text: Callable[[Any], str] | None = None,
        permission_evaluator_ref: Callable[[], Any] | None = None,
    ) -> None:
        self._hook_executor = hook_executor
        self._audit = audit
        self._display = display
        self._guardrail_runner = guardrail_runner
        self._team_ref = team_ref
        self._drain_reminders = pending_reminders_drain
        self._compact_hook = compact_hook
        self._ensure_mcp = ensure_mcp
        self._session_id_ref = session_id_ref
        self._context_window_ref = context_window_ref
        self._latch_input_tokens = latch_input_tokens
        #: Why the last :meth:`handle` failed, or ``""`` when it did not —
        #: what a headless run's exit code is made from.
        self.last_failure = ""
        # Extractor injection — Session passes its own module's
        # ``extract_response_text`` binding so
        # ``patch("ember_code.core.session.core.extract_response_text")``
        # in tests still intercepts.
        self._extract_response_text = extract_response_text or _default_extract_response_text
        # Read through a callable for the same reason as the refs above: the
        # session rebuilds its evaluator with the team.
        self._permission_evaluator_ref = permission_evaluator_ref or (lambda: None)

    async def handle(self, message: str, **media_kwargs: Any) -> str:
        """Handle a single user message and return the response.

        Accepts optional media keyword arguments (images, audio,
        videos, files) which are forwarded directly to ``team.arun``.
        """
        self.last_failure = ""
        await self._ensure_mcp()

        verdict = await ExtensionHookGate.ask(
            UserPromptSubmitValue(session_id=self._session_id_ref(), message=message),
            HookEvent.USER_PROMPT_SUBMIT,
        )
        if not verdict.allowed:
            return self._blocked(verdict.reason)
        message = str(verdict.value.get("message", message))
        blocked = await self._check_user_prompt_hook(message)
        if blocked is not None:
            return blocked

        guardrail_prefix = await self._guardrail_prefix(message)
        effective_message = self._build_effective_message(message, guardrail_prefix)

        try:
            team = self._team_ref()
            response = await team.arun(effective_message, stream=False, **media_kwargs)
            response = await self._resume_what_the_policy_decided(team, response)
            self._reject_paused_run(response)
            self._reject_failed_run(response)
            response_text = self._extract_response_text(response)

            self._audit.log(
                AuditEntry.success(
                    session_id=self._session_id_ref(),
                    agent_name=MAIN_AGENT_NAME,
                    tool_name="main_team",
                )
            )

            response_text, final_run = await self._retry_on_stop_hook_block(response_text, response)

            # Compact history if approaching context limit.
            #
            # Read off the value ``arun`` returned, not off the team. This was
            # ``getattr(getattr(team, "run_response", None), "metrics", None)``
            # and agno's ``Team``/``Agent`` have no ``run_response`` attribute
            # (2.6.12: the name exists only as a keyword parameter), so
            # ``metrics`` was None on every turn and neither the token latch nor
            # auto-compaction had ever run on this path. ``final_run`` is the
            # last RunOutput of the turn — a Stop-hook retry re-runs the agent,
            # and it is that run's input count the next threshold check needs.
            metrics = getattr(final_run, "metrics", None)
            if metrics:
                input_tokens = getattr(metrics, "input_tokens", 0) or 0
                self._latch_input_tokens(input_tokens)
                await self._compact_hook(input_tokens, self._context_window_ref())

            return response_text

        except Exception as exc:
            return await self._handle_run_failure(exc)

    #: How many times one turn may stop at a pause the policy settles before
    #: the handler stops resuming it. A turn that reads a dozen files pauses
    #: a dozen times; a run that loops is cut off rather than spun forever.
    _MAX_POLICY_RESUMES = 50

    async def _resume_what_the_policy_decided(self, team: Any, response: Any) -> Any:
        """Settle the pauses nobody needs to be asked about, and continue.

        Agno pauses every call to a tool built with ``requires_confirmation``
        — the shell, whenever ``shell_execute`` is ``ask`` — before the
        permission evaluator has looked at it. The backend the windowed
        clients use answers such a pause from the evaluator first
        (``backend/pause_handler.py``): plan mode's read-only commands, an
        allow rule, ``--accept-edits`` run, and a deny rule is refused,
        without a dialog. This path did not, so a headless ``igni -m`` with
        ``permissions.mode: plan`` stopped at ``cat README.md`` — the very
        read docs/SECURITY.md says plan mode lets through — and an allow
        rule was honoured in the window and not on the command line.

        The evaluator's verdict is applied exactly as the backend applies
        it: ALLOW confirms, DENY rejects with its reason (the model reads
        it), anything else is left paused and :meth:`_reject_paused_run`
        reports it. Nothing is allowed here that the evaluator did not
        allow.
        """
        evaluator = self._permission_evaluator_ref()
        if evaluator is None:
            return response
        from ember_code.core.config.permission_eval import PermissionDecision

        for _ in range(self._MAX_POLICY_RESUMES):
            if not _is_paused(response):
                return response
            requirements = list(getattr(response, "active_requirements", None) or [])
            if not requirements:
                return response
            verdicts = []
            for req in requirements:
                tool = getattr(req, "tool_execution", None)
                name = str(getattr(tool, "tool_name", "") or "")
                args = dict(getattr(tool, "tool_args", None) or {})
                try:
                    outcome = evaluator.evaluate_outcome(name, args)
                except Exception:  # noqa: BLE001 — an unanswerable pause, reported below
                    logger.warning("permission evaluation failed for %s", name, exc_info=True)
                    return response
                if outcome.decision not in (PermissionDecision.ALLOW, PermissionDecision.DENY):
                    return response
                verdicts.append((req, outcome))
            for req, outcome in verdicts:
                if outcome.decision is PermissionDecision.ALLOW:
                    req.confirm()
                else:
                    req.reject(note=f"Blocked: {outcome.reason}")
            response = await team.acontinue_run(
                run_response=response,
                requirements=list(getattr(response, "requirements", None) or []),
                session_id=self._session_id_ref(),
                stream=False,
            )
        return response

    @staticmethod
    def _reject_paused_run(response: Any) -> None:
        """Refuse to treat a confirmation pause as a finished turn.

        When a tool needs confirmation, Agno returns early with
        ``status=RunStatus.paused`` and content ``"I have tools to execute, but
        I need confirmation."`` — the tool never ran. Only ``backend/`` knows how
        to resolve a pause (over a client RPC), so on this path the placeholder
        was extracted, printed as the assistant's answer, and the process exited
        0. A turn that silently dropped the work it asked for is worse than a
        crash, so raise and let ``_handle_run_failure`` report it.

        Compared by name rather than against ``RunStatus`` so the module keeps
        its lazy-Agno property.
        """
        status = getattr(response, "status", None)
        if str(getattr(status, "value", status)).lower() != "paused":
            return
        paused = [
            getattr(call, "tool_name", None) or "?"
            for call in (getattr(response, "tools", None) or [])
            if getattr(call, "is_paused", False)
        ]
        raise RuntimeError(
            f"Run paused for confirmation on {paused or ['unknown tool']} but this "
            "session cannot resolve a pause; the tool did not run. Grant the tool "
            "up front (--auto-approve or a settings rule) or use a client that "
            "supports confirmations."
        )

    @staticmethod
    def _reject_failed_run(response: Any) -> None:
        """Refuse to treat a failed run as a reply.

        A non-streamed ``arun`` does not raise when the run fails — no
        model configured, the model's reply cut off, a provider error —
        it returns ``status=RunStatus.error`` with the error as its
        content. That content was printed as the assistant's answer and
        ``igni -m`` exited 0. Raise, so ``_handle_run_failure`` reports it
        as the failure it is. Compared by name, like
        :meth:`_reject_paused_run`.
        """
        status = getattr(response, "status", None)
        if str(getattr(status, "value", status)).lower() != "error":
            return
        content = getattr(response, "content", None)
        raise RuntimeError(content if isinstance(content, str) and content else "The run failed.")

    async def _check_user_prompt_hook(self, message: str) -> str | None:
        """Fire the ``UserPromptSubmit`` hook. Returns the blocked
        message when the hook denies, ``None`` when the turn should
        proceed. Blocked turns emit an audit entry so a policy denial
        is traceable — otherwise the user sees "blocked" with no
        record of why."""
        payload = UserPromptSubmitHookPayload(
            message=message,
            session_id=self._session_id_ref(),
        )
        hook_result = await self._hook_executor.execute(
            event=HookEvent.USER_PROMPT_SUBMIT.value,
            payload=payload.model_dump(),
        )
        if hook_result.should_continue:
            return None
        return self._blocked(hook_result.message or "Blocked by UserPromptSubmit hook.")

    def _blocked(self, blocked_msg: str) -> str:
        """A message a UserPromptSubmit hook stopped: audited, and returned as the reply."""
        self._audit.log(
            AuditEntry.blocked(
                session_id=self._session_id_ref(),
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
        return await self._guardrail_runner.warning_prefix(message)

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

    async def _retry_on_stop_hook_block(
        self, response_text: str, run_output: Any
    ) -> tuple[str, Any]:
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

        Returns the accepted text *and* the RunOutput that produced it.
        A retry re-runs the agent with the whole history plus a critique,
        so its input-token count is the larger one and the only one worth
        thresholding compaction on; returning text alone left the caller
        measuring a run that had been superseded.
        """
        for _stop_attempt in range(self._STOP_HOOK_MAX_RETRIES):
            payload = StopHookPayload(
                session_id=self._session_id_ref(),
                response=response_text[: self._STOP_HOOK_RESPONSE_PREVIEW],
            )
            feedback = await self._stop_feedback(payload, response_text)
            if feedback is None:
                break
            system_msg = (
                f"[SYSTEM] Your previous response was rejected by a Stop hook: "
                f"{feedback}\nPlease revise your response to address this issue."
            )
            team = self._team_ref()
            run_output = await team.arun(system_msg, stream=False)
            response_text = self._extract_response_text(run_output)
        return response_text, run_output

    async def _stop_feedback(self, payload: StopHookPayload, response_text: str) -> str | None:
        """Why the agent may not stop yet, or ``None`` when it may.

        The extensions that hook ``Stop`` are asked first, then the
        configured hooks; either one's block sends the agent back.
        """
        verdict = await ExtensionHookGate.ask(
            StopValue(session_id=payload.session_id, response=response_text), HookEvent.STOP
        )
        if not verdict.allowed:
            return verdict.reason
        stop_result = await self._hook_executor.execute(
            event=HookEvent.STOP.value,
            payload=payload.model_dump(),
        )
        if stop_result.should_continue:
            return None
        return stop_result.message or "Response blocked by Stop hook."

    async def _handle_run_failure(self, exc: Exception) -> str:
        """Common failure path: audit log, StopFailure hook fire
        (observation-only), formatted error string return.

        The StopFailure hook mirrors the Stop hook on the happy
        path — same payload shape family, same non-blocking
        semantics — so plugins can observe both success and
        failure with a single subscription pair.
        """
        error_msg = f"Error handling message: {exc}"
        self.last_failure = error_msg
        self._display.print_error(error_msg)

        self._audit.log(
            AuditEntry.error(
                session_id=self._session_id_ref(),
                agent_name="session",
                tool_name="main_team",
                error=str(exc),
                error_type=type(exc).__name__,
            )
        )

        with contextlib.suppress(Exception):
            payload = StopFailureHookPayload(
                session_id=self._session_id_ref(),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            await self._hook_executor.execute(
                event=HookEvent.STOP_FAILURE.value,
                payload=payload.model_dump(),
            )
        return error_msg


def _is_paused(response: Any) -> bool:
    """``status`` is paused — compared by name, so Agno stays a lazy import."""
    status = getattr(response, "status", None)
    return str(getattr(status, "value", status)).lower() == "paused"
