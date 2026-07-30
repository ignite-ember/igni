"""Turn Agno RunPausedEvents into protocol messages + auto-decisions.

Extracted from :mod:`ember_code.backend.server_pause` — the previous
free function ``handle_pause`` was 77 LoC of procedural logic
reaching into ``backend._session.permission_evaluator`` and
``backend._pending_requirements``. This class takes both as
constructor args so the reach-back is gone and the state seams are
explicit.

The ``_apply_auto_decision`` free helper had a string-dispatch
smell (``if decision == "confirm": ... else: ...``) that the audit
flagged as offender #7. It's replaced here by two private methods
(:meth:`_auto_confirm` / :meth:`_auto_reject`), selected
polymorphically off the :class:`AutoDecision` enum inside
:meth:`handle`.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from ember_code.backend.pending_requirements_store import PendingRequirementsStore
from ember_code.backend.schemas_pause import (
    ApplyDecisionResult,
    AutoDecision,
    PauseHandleResult,
    PendingRequirement,
)
from ember_code.core.config.permission_eval import PermissionDecision
from ember_code.protocol import messages as msg
from ember_code.protocol.agno_tool_formatter import default_registry

logger = logging.getLogger(__name__)


class PauseHandler:
    """Convert a RunPausedEvent into FE messages + auto-resolutions.

    Why do this at all: Agno's ``requires_confirmation`` gate pauses
    every "ask"-level tool indiscriminately. Without this pre-step,
    plan-mode-deny, acceptEdits-allow, bypass-allow, and matching
    ``deny:`` rules could never short-circuit the dialog — the user
    would see an approval prompt for tools the policy already decided
    about.
    """

    def __init__(
        self,
        evaluator: Any | None,
        store: PendingRequirementsStore,
    ) -> None:
        """Bind the evaluator + store the handler operates on.

        ``evaluator`` may be ``None`` (defensive — some test
        fixtures wire a session without a ``permission_evaluator``);
        in that case every req falls through to the user prompt.
        """
        self._evaluator = evaluator
        self._store = store

    def handle(self, event: Any) -> PauseHandleResult:
        """Route each requirement through the evaluator, deferring
        only what the policy left undecided.

        Returns:
        * ``messages`` — what to forward to the FE. Either a single
          ``RunPaused`` (when at least one req still needs the user)
          or empty (when the evaluator decided every req).
        * ``auto_resolved`` — Agno requirement objects the evaluator
          already confirmed or rejected. Caller resumes Agno with
          these via ``acontinue_run`` (all-auto case) or stashes them
          on the store's auto-resolved bucket for the eventual
          ``resolve_hitl_batch`` (mixed case).
        * ``run_id`` — the paused run; needed by the resume.
        """
        run_id_raw = getattr(event, "run_id", None)
        run_id = str(run_id_raw) if run_id_raw else None
        requirements: list[msg.HITLRequest] = []
        auto_resolved: list[Any] = []

        for req in getattr(event, "active_requirements", []) or []:
            req_id = str(uuid.uuid4())[:8]
            tool_exec = getattr(req, "tool_execution", None)
            raw_name = str(getattr(tool_exec, "tool_name", "") if tool_exec else "")
            tool_args = dict(getattr(tool_exec, "tool_args", {}) if tool_exec else {})

            classified = self._classify(raw_name, tool_args)

            if classified is not None:
                auto_decision, reason = classified
                result = self._apply(req, auto_decision, raw_name, run_id, reason)
                if result.ok:
                    auto_resolved.append(req)
                    continue
                # Fall through to the user-prompt path on any
                # ``req.confirm() / req.reject()`` failure.

            # Defer: ask the user.
            self._store.register(req_id, PendingRequirement(req=req, run_id=run_id))
            requirements.append(
                msg.HITLRequest(
                    requirement_id=req_id,
                    tool_name=raw_name,
                    friendly_name=default_registry().friendly_name(raw_name),
                    tool_args=tool_args,
                )
            )

        messages: list[msg.Message] = []
        if requirements:
            messages.append(msg.RunPaused(run_id=run_id or "", requirements=requirements))
        return PauseHandleResult(
            messages=messages,
            auto_resolved=auto_resolved,
            run_id=run_id,
        )

    # ── Private helpers ─────────────────────────────────────────────

    def _classify(
        self, raw_name: str, tool_args: dict[str, Any]
    ) -> tuple[AutoDecision, str] | None:
        """Ask the evaluator for its verdict and translate to the enum.

        Returns ``None`` on DEFER, on evaluator absence, or on any
        evaluator exception — all three collapse to the user-prompt
        path. On DENY/ALLOW returns ``(AutoDecision, reason)``: the
        reason string comes from the evaluator outcome in the same
        pass so it can't drift from the decision (the two-scan drift
        the audit flagged when this used to call ``evaluate`` and
        ``explain_deny`` separately).
        """
        if self._evaluator is None:
            return None
        try:
            outcome = self._evaluator.evaluate_outcome(raw_name, tool_args)
        except Exception as exc:
            logger.warning(
                "permission_evaluator.evaluate(%s) raised %s — falling back to user prompt",
                raw_name,
                exc,
            )
            return None
        if outcome.decision is PermissionDecision.DENY:
            return AutoDecision.REJECT, outcome.reason
        if outcome.decision is PermissionDecision.ALLOW:
            return AutoDecision.CONFIRM, ""
        return None

    def _apply(
        self,
        req: Any,
        decision: AutoDecision,
        raw_name: str,
        run_id: str | None,
        reason: str,
    ) -> ApplyDecisionResult:
        """Polymorphic dispatch on the enum. Kept as a two-line
        method so a future third variant is a one-branch add."""
        if decision is AutoDecision.CONFIRM:
            return self._auto_confirm(req, raw_name, run_id)
        return self._auto_reject(req, raw_name, run_id, reason)

    def _auto_confirm(self, req: Any, raw_name: str, run_id: str | None) -> ApplyDecisionResult:
        """Confirm a requirement on the caller's behalf."""
        try:
            req.confirm()
        except Exception as exc:
            logger.warning(
                "auto-confirm raised for %s: %s — falling back to user prompt",
                raw_name,
                exc,
            )
            return ApplyDecisionResult(ok=False, reason=str(exc))
        logger.info("Auto-confirmed %s by permission policy (run_id=%s)", raw_name, run_id)
        return ApplyDecisionResult(ok=True)

    def _auto_reject(
        self, req: Any, raw_name: str, run_id: str | None, reason: str
    ) -> ApplyDecisionResult:
        """Reject a requirement on the caller's behalf."""
        try:
            req.reject(note=f"Blocked: {reason}")
        except Exception as exc:
            logger.warning(
                "auto-reject raised for %s: %s — falling back to user prompt",
                raw_name,
                exc,
            )
            return ApplyDecisionResult(ok=False, reason=str(exc))
        logger.info("Auto-rejected %s (%s) run_id=%s", raw_name, reason, run_id)
        return ApplyDecisionResult(ok=True)
