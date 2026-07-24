"""Plan-mode transaction coordinator — extracted from
:class:`PlanTool` so the tool becomes a thin Toolkit adapter and
the two multi-step transactions (enter plan mode / submit a
plan) live on a dedicated class.

Before the extract, :class:`PlanTool` reached into the session
with ``hasattr`` / ``getattr`` guards to sequence four
collaborators (permission evaluator, plan_store, todo_store,
broadcast_bus) plus a validator and a researcher spawner. That
was the audit's "utility-module-of-related-helpers offender":
a Toolkit masquerading as a coordinator.

The tool now delegates the ordering to
:class:`PlanTransactionCoordinator`. The coordinator owns:

* Instances of :class:`PlanConfidenceValidator` +
  :class:`PlanResearcherRunner` (composition — the tool doesn't
  hold them anymore).
* The session-shape refusal path (missing
  ``set_permission_mode`` / ``plan_store`` becomes a typed
  ``PlanEnterResult(ok=False, ...)`` / ``PlanExitResult(ok=False,
  ...)`` refusal, so raw ``AttributeError`` never leaks to the
  tool).
* The single reply-text render site for each transaction —
  callers pull ``.reply_text`` off the typed envelope and are
  done.
* The queue-post-run broadcast for ``plan_submitted`` + the
  eager ``permission_mode_changed`` follow-up broadcast.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ember_code.core.session.broadcast_schema import BroadcastEvent
from ember_code.core.tools.plan.researcher import PlanResearcherRunner
from ember_code.core.tools.plan.schemas import (
    PermissionModeChangedPayload,
    PlanEnterResult,
    PlanExitInput,
    PlanExitResult,
    PlanSubmittedPayload,
    PlanTaskInput,
)
from ember_code.core.tools.plan.validator import PlanConfidenceValidator
from ember_code.core.tools.todo import _coerce_items

if TYPE_CHECKING:
    from ember_code.core.session.core import Session

logger = logging.getLogger(__name__)


class PlanSessionShapeError(RuntimeError):
    """Raised inside the coordinator when the session is missing
    a required attribute (``set_permission_mode``, ``plan_store``,
    ``todo_store``, ``broadcast_bus``). Caught at the two public
    coordinator methods and packaged into a typed refusal
    envelope — the tool never sees the raw exception."""

    def __init__(self, missing: str, reply_text: str) -> None:
        super().__init__(f"session missing required attribute: {missing}")
        self.missing = missing
        self.reply_text = reply_text


class PlanTransactionCoordinator:
    """Owns the plan-mode enter / submit transactions against a
    :class:`Session`.

    Constructor takes the session so both public methods can read
    ``permission_evaluator``, ``plan_store``, ``todo_store``, and
    ``broadcast_bus`` without threading them through call sites.
    Follows the sibling ``PlanConfidenceValidator`` /
    ``PlanResearcherRunner`` / ``PlanCoordinator`` (in
    ``core/session/plan_ops.py``) constructor convention.

    IMPORTANT: ``__init__`` deliberately does NOT eagerly access
    ``session.todo_store`` / ``session.broadcast_bus`` — bare
    ``MagicMock(spec=["plan_store"])`` test fixtures pass through
    construction fine, and the shape checks belong at method
    entry only (wrapped so :class:`AttributeError` is caught once
    per public method and packaged into a typed refusal).

    Public API:

    * :meth:`enter` — ``PlanEnterResult`` for the ``enter_plan_mode``
      transaction (flip mode → reset attempts → broadcast the
      agent-attributed follow-up → optionally spawn the
      researcher).
    * :meth:`submit` — ``PlanExitResult`` for the
      ``exit_plan_mode`` transaction (confidence-gate → set
      plan → populate todos → queue the ``plan_submitted``
      broadcast).
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        # Compose the sibling collaborators. Both accept a bare
        # session and lazily read attributes — safe against
        # partial-construction test stubs.
        self._validator = PlanConfidenceValidator(session)
        self._researcher = PlanResearcherRunner(session)

    # ── enter_plan_mode transaction ────────────────────────────

    async def enter(self, reason: str, task: str) -> PlanEnterResult:
        """Run the enter-plan-mode transaction and return a
        typed envelope. See :class:`PlanEnterResult` for field
        semantics."""
        reason_clean = (reason or "").strip()
        task_clean = (task or "").strip()

        try:
            return await self._enter_transaction(reason_clean, task_clean)
        except PlanSessionShapeError as shape_err:
            # Session-shape refusals become typed envelopes so
            # the tool renders one string, not an AttributeError.
            return PlanEnterResult(
                ok=False,
                reason=f"session_missing_{shape_err.missing}",
                error=shape_err.reply_text,
                reply_text=shape_err.reply_text,
            )

    async def _enter_transaction(self, reason_clean: str, task_clean: str) -> PlanEnterResult:
        """Body of the enter transaction — factored out so
        :meth:`enter` can wrap it in the one AttributeError-catch
        site the coordinator promises."""
        set_mode = getattr(self._session, "set_permission_mode", None)
        if not callable(set_mode):
            # Test stubs may build a ``MagicMock(spec=[...])``
            # without ``set_permission_mode`` — raise the typed
            # shape error so :meth:`enter` renders the string.
            msg = "Error: session does not support runtime mode changes."
            raise PlanSessionShapeError(missing="set_permission_mode", reply_text=msg)

        mode_status = set_mode("plan")
        if "Error" in mode_status:
            # Evaluator refused the flip — propagate the raw
            # status verbatim (tests match on the "Error"
            # substring the evaluator produces).
            return PlanEnterResult(
                ok=False,
                mode_status=mode_status,
                reason="evaluator_refused",
                error=mode_status,
                reply_text=mode_status,
            )

        # Reset the per-plan-mode-session validation state so
        # the iteration counter starts fresh on every fresh entry.
        self._validator.reset()

        # Re-broadcast with the agent attribution + reason so
        # the FE can render a small info banner. The broadcast
        # bus is a construction invariant on Session, so no
        # ``hasattr`` feature-detect is needed.
        #
        # TODO(broadcast-bus): drop the ``.model_dump()`` at this
        # emit site once the BroadcastBus API accepts BaseModel
        # payloads and dumps internally (audit's Pattern-2
        # partial-application follow-up).
        payload = PermissionModeChangedPayload(
            mode="plan",
            source="agent",
            reason=reason_clean,
        )
        self._session.broadcast_bus.emit(
            BroadcastEvent(
                channel="permission_mode_changed",
                payload=payload.model_dump(),
            )
        )

        # Spawn the plan_researcher sub-agent on the task. The
        # researcher operates in plan mode itself (same evaluator
        # — file edits blocked) and produces a structured report
        # the caller turns into the final ``exit_plan_mode`` call.
        researcher_report = ""
        if task_clean:
            researcher_report = await self._researcher.run(task_clean)

        mode_flipped = "already" not in mode_status.lower()
        reply_text = self._render_enter_reply(
            reason_clean=reason_clean,
            researcher_report=researcher_report,
        )
        return PlanEnterResult(
            ok=True,
            mode_flipped=mode_flipped,
            mode_status=mode_status,
            researcher_report=researcher_report,
            reply_text=reply_text,
        )

    def _render_enter_reply(self, *, reason_clean: str, researcher_report: str) -> str:
        """Single render site for the ``enter_plan_mode`` reply
        text. Preserves the "Entered plan mode" substring the
        tests match on."""
        tail = f" ({reason_clean})" if reason_clean else ""
        header = f"Entered plan mode{tail}. File edits and mutating shell commands are now blocked."
        if researcher_report:
            return (
                f"{header}\n\nplan_researcher sub-agent report follows. "
                "Review it, refine if needed, then call "
                "exit_plan_mode(plan, tasks=[...]) with the final plan. "
                "If the report is thin, call enter_plan_mode again with "
                "a sharper task description for another research pass.\n\n"
                "---\n\n"
                f"{researcher_report}"
            )
        return (
            f"{header} Gather context (CodeIndex queries first, then file_read), "
            "then call exit_plan_mode(plan, tasks=[...]) with a concrete proposal. "
            "Tip: pass the user's request as `task=` next time and this tool will "
            "spawn the plan_researcher sub-agent for you automatically."
        )

    # ── exit_plan_mode transaction ─────────────────────────────

    def submit(self, plan: str, tasks: list | None) -> PlanExitResult:
        """Run the submit-plan transaction and return a typed
        envelope. See :class:`PlanExitResult` for field semantics.

        Signature accepts ``list | None`` (agent-produced dicts
        arrive raw from the LLM through the tool's public
        method); the coordinator constructs a strict
        :class:`PlanExitInput` internally from that shape."""
        plan_text = (plan or "").strip()
        if not plan_text:
            msg = (
                "Error: plan is empty. "
                "Pass a markdown-formatted plan describing what you intend to do."
            )
            return PlanExitResult(
                ok=False,
                reason="empty_plan",
                error=msg,
                reply_text=msg,
            )

        try:
            return self._submit_transaction(plan_text, tasks)
        except PlanSessionShapeError as shape_err:
            return PlanExitResult(
                ok=False,
                reason=f"session_missing_{shape_err.missing}",
                error=shape_err.reply_text,
                reply_text=shape_err.reply_text,
            )

    def _submit_transaction(self, plan_text: str, raw_tasks: list | None) -> PlanExitResult:
        """Body of the submit transaction — factored out so
        :meth:`submit` can wrap it in the one AttributeError-catch
        site the coordinator promises."""
        store = getattr(self._session, "plan_store", None)
        if store is None:
            msg = "Error: plan store not initialised on this session."
            raise PlanSessionShapeError(missing="plan_store", reply_text=msg)

        # ── Confidence check (row 50 enforcement) ─────────────
        # Reject submissions that aren't grounded in concrete
        # codebase facts. The rejection is bounded by an attempt
        # counter (owned by the validator) so the loop converges
        # even when the model can't satisfy us; after the cap we
        # accept whatever came in (the user still reviews +
        # can refine).
        verdict = self._validator.validate(plan_text, raw_tasks)
        if verdict.reject and verdict.attempts_remaining > 0:
            return PlanExitResult(
                ok=False,
                rejected=True,
                plan_rejection_feedback=verdict.feedback,
                reason="confidence_rejected",
                reply_text=verdict.feedback,
            )

        # Construct the strict input envelope for the todo
        # pipeline. Agent-supplied dicts round-trip through
        # PlanTaskInput.coerce_batch first so malformed rows
        # get named at the ``_coerce_items`` layer.
        exit_input = PlanExitInput(
            plan=plan_text,
            tasks=self._build_task_input_models(raw_tasks),
        )

        store.set_plan(exit_input.plan)

        # Structured tasks → TodoStore so the PlanCard can
        # render a live checklist alongside the prose plan.
        task_snapshot: list[dict[str, Any]] = []
        validation_errors: list[str] = []
        if raw_tasks:
            coerced_input = PlanTaskInput.coerce_batch(raw_tasks)
            items, errs = _coerce_items(coerced_input)
            validation_errors = errs
            todo_store = getattr(self._session, "todo_store", None)
            if items and todo_store is not None:
                todo_store.set(items)
                task_snapshot = todo_store.snapshot()

        # Broadcast so the FE can render the plan card inline +
        # show the approve/reject buttons. Deferred to AFTER the
        # run finishes: the PlanCard is the outcome of the run,
        # so it should land below the agent's closing reply in
        # the chat list, not mid-stream above it.
        #
        # TODO(broadcast-bus): drop the ``.model_dump()`` at this
        # emit site once the BroadcastBus API accepts BaseModel
        # payloads and dumps internally (audit's Pattern-2
        # partial-application follow-up).
        payload = PlanSubmittedPayload(plan=plan_text, tasks=list(task_snapshot))
        self._session.broadcast_bus.queue_post_run(
            BroadcastEvent(
                channel="plan_submitted",
                payload=payload.model_dump(),
            )
        )

        reply_text = self._render_submit_reply(
            task_snapshot=task_snapshot,
            validation_errors=validation_errors,
        )
        return PlanExitResult(
            ok=True,
            task_snapshot=task_snapshot,
            validation_errors=validation_errors,
            task_count=len(task_snapshot),
            reply_text=reply_text,
        )

    def _render_submit_reply(
        self,
        *,
        task_snapshot: list[dict[str, Any]],
        validation_errors: list[str],
    ) -> str:
        """Single render site for the ``exit_plan_mode`` reply
        text. Preserves the "Plan submitted" / "Tasks validation
        errors" substrings the tests match on."""
        reply = (
            "Plan submitted. Stop here — the user will review and either "
            "exit plan mode via `/plan` (to let you execute) or ask for "
            "changes. Do not continue executing in this turn."
        )
        if task_snapshot:
            reply += (
                f" ({len(task_snapshot)} structured task(s) populated; "
                "they'll tick off as you call todo_write during execution)."
            )
        if validation_errors:
            reply += f" Tasks validation errors (ignored): {'; '.join(validation_errors)}"
        return reply

    # ── Internal helpers ────────────────────────────────────────

    @staticmethod
    def _build_task_input_models(raw_tasks: list | None) -> list[PlanTaskInput]:
        """Best-effort coerce agent-supplied task dicts into
        :class:`PlanTaskInput` models for the strict
        :class:`PlanExitInput` boundary. Malformed rows are
        skipped here (the wire-shape errors surface later in
        :func:`_coerce_items` where they can name the specific
        problem). The wire-boundary payload for the todo
        pipeline still round-trips through
        :meth:`PlanTaskInput.coerce_batch` — this method exists
        purely so :class:`PlanExitInput` has a valid ``tasks``
        list at the coordinator hop."""
        models: list[PlanTaskInput] = []
        for entry in raw_tasks or []:
            if not isinstance(entry, dict):
                continue
            try:
                models.append(PlanTaskInput.model_validate(entry))
            except Exception:
                continue
        return models


__all__ = [
    "PlanSessionShapeError",
    "PlanTransactionCoordinator",
]
