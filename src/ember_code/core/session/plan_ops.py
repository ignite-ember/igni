"""Plan-mode decision recording.

Extracted from :mod:`ember_code.core.session.core` — the three
methods that persist a user's approve / dismiss decision on a
plan the agent submitted via ``exit_plan_mode``. Each takes the
session as an explicit argument.

Side-effect ordering matters: the persistence write happens
BEFORE the mode flip so a crash mid-flip doesn't leave the user
with ``mode=default`` and no recorded approval — which is the
original bug that motivated persisting these decisions in the
first place.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from ember_code.core.session.core import Session

logger = logging.getLogger(__name__)


class PlanDecisionResult(BaseModel):
    """Return shape for :func:`approve_plan` /
    :func:`dismiss_plan`. Surfaced to the FE via the
    ``APPROVE_PLAN`` / ``DISMISS_PLAN`` RPCs; the transport
    serializer auto-converts via ``model_dump()`` so the wire
    keeps its dict shape without callers having to remember."""

    run_id: str
    decision: str
    mode_status: str


async def approve_plan(session: "Session", run_id: str) -> PlanDecisionResult:
    """Record the user's approval and flip out of plan mode.

    See :func:`_record_plan_decision` for the ordering of side
    effects. Returns a :class:`PlanDecisionResult`.
    """
    return await _record_plan_decision(session, run_id, "approved", flip_mode=True)


async def dismiss_plan(session: "Session", run_id: str) -> PlanDecisionResult:
    """Record the user's dismissal (they want to iterate on the
    plan, so we do NOT flip the mode)."""
    return await _record_plan_decision(session, run_id, "dismissed", flip_mode=False)


async def _record_plan_decision(
    session: "Session",
    run_id: str,
    decision: str,
    *,
    flip_mode: bool,
) -> PlanDecisionResult:
    if not run_id:
        raise ValueError("run_id must be non-empty")
    store = getattr(session, "plan_store", None)
    if store is None:
        raise RuntimeError("plan_store not initialised on this session")
    store.set_decision(run_id, decision)
    # Persist BEFORE flipping mode so a crash mid-flip doesn't
    # leave the user with mode=default but no recorded approval —
    # the original bug that motivated persisting these decisions.
    if hasattr(session, "persistence"):
        try:
            await session.persistence.save_plan_decisions(store.decisions_snapshot())
        except Exception as exc:
            logger.debug("plan decision persist failed: %s", exc)
    mode_status = ""
    if flip_mode:
        mode_status = session.set_permission_mode("default")
    session.broadcast(
        "plan_decided",
        {"run_id": run_id, "decision": decision},
    )
    return PlanDecisionResult(
        run_id=run_id,
        decision=decision,
        mode_status=mode_status,
    )
