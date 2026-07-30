"""Typed payload models for the plan-mode toolkit.

Every broadcast the plan tool emits, plus the ``tasks`` argument
of ``exit_plan_mode``, is typed via a Pydantic model here.
Emit sites call ``.model_dump()`` at the :class:`BroadcastEvent`
boundary so the identity-of-``payload`` contract that
``broadcast_schema.py`` documents is preserved (each emit
produces a fresh dict; callbacks see the same live dict for the
duration of that emit).

Beyond the broadcast payloads, this module also hosts the two
typed envelopes :class:`PlanTransactionCoordinator` returns —
:class:`PlanEnterResult` for ``enter_plan_mode`` and
:class:`PlanExitResult` for ``exit_plan_mode``. :class:`PlanTool`
consumes those envelopes and renders the agent-visible reply
string from a single call site each, so the "Error: ..." /
"Plan submitted" / "Plan rejected" / "Entered plan mode"
substrings the tool used to build inline now live on the
coordinator side of the boundary as ``reply_text``. And
:class:`PlanExitInput` is the strict input model at the
coordinator ``submit`` boundary — ``PlanTool.exit_plan_mode``
keeps the agent-facing ``tasks: list | None`` (Agno reads that
annotation to build the LLM-visible JSON-Schema), but the
coordinator's internal boundary is strict.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class PermissionModeChangedPayload(BaseModel):
    """Payload of the ``permission_mode_changed`` broadcast the
    plan tool fires after switching into plan mode. The base flip
    is already broadcast by :class:`RuntimeModeCoordinator`;
    this follow-up carries the agent-attribution + reason string
    the FE renders as a small banner."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["default", "plan", "acceptEdits", "bypassPermissions"]
    source: Literal["agent", "user"]
    reason: str = ""


class PlanSubmittedPayload(BaseModel):
    """Payload of the ``plan_submitted`` broadcast queued by
    :meth:`PlanTool.exit_plan_mode`. ``tasks`` rides along so
    the FE seeds the PlanCard checklist on first render — later
    ``todos_updated`` pushes refresh statuses live."""

    model_config = ConfigDict(extra="ignore")

    plan: str
    tasks: list[dict[str, Any]] = Field(default_factory=list)


class PlanTaskInput(BaseModel):
    """One row in ``exit_plan_mode(tasks=[...])`` — the agent-
    facing shape of a plan-time task suggestion. Extras are
    ignored (not rejected) so a slightly wider agent-produced
    dict doesn't blow up the call; the strict validation happens
    downstream in :func:`_coerce_items` where the tasks land in
    :class:`TodoStore`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    content: str
    active_form: str = Field("", alias="activeForm")

    @classmethod
    def coerce_batch(cls, rows: Iterable[Any] | None) -> list[dict]:
        """Round-trip agent-supplied task dicts through this
        model so we validate the shape once (dropping malformed
        rows) before handing off to :func:`_coerce_items` for
        the final :class:`TodoItem` construction.

        The returned ``list[dict]`` preserves the wire shape the
        downstream :func:`_coerce_items` in ``core/tools/todo.py``
        expects (camelCase ``activeForm`` alias, string content).
        Non-dict entries pass through untouched so
        :func:`_coerce_items` emits the "not a dict" error for
        the agent — validation stays where it can name the
        specific problem.

        This is the data-owns-its-own-batch-construction fix
        for the audit's ``_coerce_task_input`` static-method
        offender: batch coercion of :class:`PlanTaskInput` rows
        belongs on :class:`PlanTaskInput`, not on the tool.
        """
        cleaned: list[dict] = []
        for entry in rows or []:
            if not isinstance(entry, dict):
                # Preserve non-dict rows so ``_coerce_items``
                # can emit the "not a dict" error for the agent.
                cleaned.append(entry)
                continue
            try:
                model = cls.model_validate(entry)
            except Exception:
                # Preserve the raw entry so downstream validation
                # can name the specific problem.
                cleaned.append(entry)
                continue
            cleaned.append(model.model_dump(by_alias=True))
        return cleaned


class PlanExitInput(BaseModel):
    """Strict input model at the :class:`PlanTransactionCoordinator`
    ``submit`` boundary. ``PlanTool.exit_plan_mode`` keeps
    ``tasks: list | None`` on its agent-facing signature (Agno
    reads that annotation to build the LLM-visible JSON-Schema,
    which shouldn't nest another Pydantic type); the coordinator
    accepts this typed envelope internally so its boundary is
    Rule-1-compliant."""

    model_config = ConfigDict(extra="ignore")

    plan: str
    tasks: list[PlanTaskInput] = Field(default_factory=list)


class PlanEnterResult(BaseModel):
    """Typed envelope :meth:`PlanTransactionCoordinator.enter`
    returns. Replaces the old "Error: ..." string channel with a
    structured ``ok`` + ``reason`` + ``reply_text`` shape so
    :class:`PlanTool` can derive its agent-visible string at a
    single render site.

    Fields:

    * ``ok`` — did the enter transaction complete? False when the
      session shape refuses the flip (no ``set_permission_mode``)
      or the evaluator reports an error string.
    * ``mode_flipped`` — did the permission mode actually change?
      A no-op "already in plan" is ``ok=True, mode_flipped=False``.
    * ``mode_status`` — the raw string
      :meth:`Session.set_permission_mode` returned (empty on
      refusal). Used to preserve the exact message on error paths.
    * ``reason`` — machine-readable refusal reason (empty on ok).
    * ``researcher_report`` — the plan_researcher sub-agent's
      report text when ``task=`` was provided and the spawn
      succeeded, else empty string.
    * ``error`` — human-readable error string on refusal, or
      empty on ok. Distinct from ``reason`` so callers can log
      the machine tag AND surface the prose.
    * ``reply_text`` — the string the tool returns to the agent
      verbatim. Contains the "Entered plan mode" / "Error: ..."
      substrings the tests match on.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    mode_flipped: bool = False
    mode_status: str = ""
    reason: str = ""
    researcher_report: str = ""
    error: str = ""
    reply_text: str


class PlanExitResult(BaseModel):
    """Typed envelope :meth:`PlanTransactionCoordinator.submit`
    returns. Replaces the old string-only reply with a structured
    shape carrying the store snapshot, validation errors, and the
    reject-vs-accepted signal.

    Fields:

    * ``ok`` — did the submit transaction commit? False on empty
      plan, missing plan_store on the session, or a confidence-
      rejected plan (which is a soft refusal, not an error).
    * ``rejected`` — narrower flag: True only when the confidence
      validator rejected the plan (as opposed to a hard error
      like empty plan). Callers can tell "try again with more
      research" apart from "you called me wrong".
    * ``task_snapshot`` — the TodoStore snapshot after commit
      (each dict has ``content``/``status``/``activeForm``).
    * ``validation_errors`` — malformed task rows that
      :func:`_coerce_items` dropped; surfaced in the reply so
      the agent can correct on the next call.
    * ``task_count`` — length of ``task_snapshot``; carried
      separately so the reply-text renderer doesn't need to
      re-count.
    * ``plan_rejection_feedback`` — the confidence-validator's
      rejection prose. Populated only when ``rejected=True``.
    * ``reason`` — machine-readable refusal reason (empty on ok).
    * ``error`` — human-readable error string on hard refusals.
    * ``reply_text`` — the string the tool returns to the agent
      verbatim. Contains "Plan submitted" / "Plan rejected" /
      "Error" substrings the tests match on.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    rejected: bool = False
    task_snapshot: list[dict[str, Any]] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    task_count: int = 0
    plan_rejection_feedback: str = ""
    reason: str = ""
    error: str = ""
    reply_text: str


__all__ = [
    "PermissionModeChangedPayload",
    "PlanEnterResult",
    "PlanExitInput",
    "PlanExitResult",
    "PlanSubmittedPayload",
    "PlanTaskInput",
]
