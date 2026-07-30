"""Typed schemas for the HITL pause pipeline.

Extracted from :mod:`ember_code.backend.server_pause` — the previous
free-function module used raw tuples (``(kind, payload)`` on an
untyped ``asyncio.Queue``, ``(req, run_id)`` in the pending-req
dict, a naked ``(messages, auto_resolved, run_id)`` triple as the
return of ``handle_pause``) and a string literal (``"confirm"`` /
``"reject"``) to steer the auto-decision branch. Every one of those
untyped seams lives here as a Pydantic model / enum so mypy + Ruff
give us schema coverage at every boundary.

Sibling convention: mirrors :mod:`ember_code.backend.schemas_run` —
same file per top-level pipeline (run / pause), same
``ConfigDict(arbitrary_types_allowed=True)`` for the models that
carry opaque Agno requirement objects or protocol messages.

Consumers:

* :class:`AutoDecision` — replaces the ``decision: str`` literal in
  the old ``_apply_auto_decision``. ``PauseHandler`` dispatches to
  :meth:`_auto_confirm` / :meth:`_auto_reject` polymorphically off
  the enum value.
* :class:`PendingRequirement` — replaces the raw
  ``(req, run_id)`` tuple stashed in the pending-requirements dict.
* :class:`ApplyDecisionResult` — replaces the bool return of
  ``_apply_auto_decision`` so callers get a typed reason on the
  fallback path.
* :class:`PauseHandleResult` — replaces the 3-tuple return of
  ``handle_pause``.
* :class:`MuxEvent` / its four variants — replaces the
  ``asyncio.Queue`` of raw 2-tuples with a typed tagged union.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ember_code.protocol import messages as msg


class AutoDecision(str, Enum):
    """Verdict the ``PermissionEvaluator`` returned before HITL asked
    the user.

    * ``CONFIRM`` — evaluator says ALLOW; ``PauseHandler`` calls
      ``req.confirm()`` and appends to ``auto_resolved``.
    * ``REJECT`` — evaluator says DENY; ``PauseHandler`` calls
      ``req.reject(note=...)`` and appends to ``auto_resolved``.

    A missing decision (evaluator returned DEFER or raised) falls
    through to the user-prompt path — represented by ``None`` at
    the callsite, not a third enum value.
    """

    CONFIRM = "confirm"
    REJECT = "reject"


class PendingRequirement(BaseModel):
    """One entry in :class:`PendingRequirementsStore` — the Agno
    requirement plus the run_id it was paused under.

    ``arbitrary_types_allowed`` is on because ``req`` is an opaque
    Agno object (a private module symbol we treat structurally via
    :class:`schemas_hitl.RunRequirement` Protocol at the resolver
    seam).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    req: Any
    run_id: str | None = None


class ApplyDecisionResult(BaseModel):
    """Return of :meth:`PauseHandler._auto_confirm` /
    :meth:`PauseHandler._auto_reject`.

    ``ok=False`` means the underlying ``req.confirm()`` /
    ``req.reject()`` raised — the caller (``PauseHandler.handle``)
    falls back to the user prompt for that requirement. ``reason``
    carries the exception message so a diagnostic can be logged
    without re-catching."""

    ok: bool
    reason: str = ""


class PauseHandleResult(BaseModel):
    """Return of :meth:`PauseHandler.handle`.

    ``messages`` — what the multiplexer forwards to the FE. Either
    a single ``RunPaused`` (mixed / all-defer case) or empty (every
    req decided by the evaluator).

    ``auto_resolved`` — Agno requirement objects the evaluator
    confirmed or rejected. Caller resumes Agno with these via
    ``acontinue_run`` (all-auto case) or stashes them on the store's
    auto-resolved bucket for the eventual ``resolve_hitl_batch``
    (mixed case).

    ``run_id`` — the paused run; needed by the resume.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    messages: list[msg.Message] = Field(default_factory=list)
    auto_resolved: list[Any] = Field(default_factory=list)
    run_id: str | None = None


# ── Multiplexer queue variants ──────────────────────────────────────
#
# The old asyncio.Queue held raw 2-tuples: ('event', team_event),
# ('subagent_pause', entries), ('done', SENTINEL), ('error', exc).
# The four Pydantic classes below tag them by ``kind`` so the
# multiplexer's dispatch is real type-narrowing (``isinstance``) and
# not a string comparison on a positional tuple element.


class TeamStreamEvent(BaseModel):
    """A raw event pulled off the team's Agno stream — forwarded to
    the multiplexer for pause / done / serialize dispatch."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    kind: Literal["event"] = "event"
    event: Any


class SubagentPause(BaseModel):
    """A batch of sub-agent HITL entries drained from the
    coordinator. Wrapped in ``RunPaused`` and yielded to the FE."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    kind: Literal["subagent_pause"] = "subagent_pause"
    entries: list[Any] = Field(default_factory=list)


class MuxDone(BaseModel):
    """The team-drain hit end-of-stream. Multiplexer returns cleanly."""

    kind: Literal["done"] = "done"


class MuxError(BaseModel):
    """The team-drain raised. Multiplexer re-raises so the outer
    try/except in :meth:`HITLStreamMultiplexer.stream` gets it."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    kind: Literal["error"] = "error"
    exc: Any


# Union of the four variants above — the typed queue element for the
# multiplexer. Kept as a top-level alias so type hints stay short at
# the consumer.
MuxEvent = TeamStreamEvent | SubagentPause | MuxDone | MuxError
