"""Plan-mode decision recording — coordinator class only.

Owns the persist-then-mode-flip flow for a user's approve /
dismiss decision on a plan the agent submitted via
``exit_plan_mode``. Session composes one
:class:`PlanCoordinator` instance in
``Session._init_per_session_scratch``; the thin ``Session``
delegators forward to it.

Side-effect ordering matters and is now an ENFORCED contract
rather than a "log-and-hope": the persistence write happens
BEFORE the mode flip so a crash mid-flip doesn't leave the user
with ``mode=default`` and no recorded approval — the original
bug that motivated persisting these decisions in the first
place. If the DB write fails on the approve path, the mode
flip is ABORTED and the coordinator returns
:class:`PlanDecisionResult` with ``ok=False``. This is a
semantic tightening of the pre-refactor "swallow-and-flip"
behaviour and matches the top-of-file invariant.

The wire shapes — :class:`PlanDecisionResult` (return envelope)
and :class:`PlanDecidedBroadcast` (broadcast payload) — live in
:mod:`~ember_code.core.session.schemas` alongside sibling
coordinator wire types (:class:`CompactResult`,
:class:`LoopAdvance`). The re-export at the bottom of this
module keeps ``from ember_code.core.session.plan_ops import
PlanDecisionResult`` working for legacy callers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ember_code.core.session.broadcast_schema import BroadcastEvent
from ember_code.core.session.schemas import (
    PlanDecidedBroadcast,
    PlanDecisionResult,
)
from ember_code.core.tools.plan import PlanDecision

if TYPE_CHECKING:
    from ember_code.core.session.core import Session

logger = logging.getLogger(__name__)


class PlanCoordinator:
    """Owns the persist-then-mode-flip flow for plan decisions.

    Constructor holds a reference to the session so it can reach
    the ``plan_store`` / ``persistence`` / ``broadcast_bus`` /
    ``set_permission_mode`` bound method at call time. Session
    composes one instance in ``_init_per_session_scratch``.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    async def approve(self, run_id: str) -> PlanDecisionResult:
        """Record the user's approval and flip out of plan mode."""
        return await self._record(run_id, PlanDecision.APPROVED, flip_mode=True)

    async def dismiss(self, run_id: str) -> PlanDecisionResult:
        """Record the user's dismissal (they want to iterate on the
        plan, so we do NOT flip the mode)."""
        return await self._record(run_id, PlanDecision.DISMISSED, flip_mode=False)

    async def _record(
        self,
        run_id: str,
        decision: PlanDecision,
        *,
        flip_mode: bool,
    ) -> PlanDecisionResult:
        """Common body for :meth:`approve` / :meth:`dismiss`.

        Returns a :class:`PlanDecisionResult` envelope on every
        path — including validation refusals and DB failures on
        the flip path. Callers key on ``result.ok`` to decide
        whether to surface a success confirmation or an inline
        error to the FE.
        """
        if not run_id:
            return PlanDecisionResult(
                run_id=run_id,
                decision=decision,
                ok=False,
                error="run_id must be non-empty",
            )
        session = self._session
        store = getattr(session, "plan_store", None)
        if store is None:
            return PlanDecisionResult(
                run_id=run_id,
                decision=decision,
                ok=False,
                error="plan_store not initialised on this session",
            )
        store.set_decision(run_id, decision)
        # Persist BEFORE flipping mode so a crash mid-flip doesn't
        # leave the user with mode=default but no recorded approval —
        # the original bug that motivated persisting these decisions.
        # ``decisions_snapshot`` returns a typed :class:`PlanDecisionsBlob`
        # which persistence dumps at its own boundary — the caller-side
        # dict comprehension is gone (Rule 1).
        persistence = getattr(session, "persistence", None)
        if persistence is None:
            return PlanDecisionResult(
                run_id=run_id,
                decision=decision,
                ok=False,
                error="persistence not initialised on this session",
            )
        blob = store.decisions_snapshot()
        try:
            await persistence.save_plan_decisions(blob)
        except Exception as exc:
            # On the approve path, the persist-before-flip invariant
            # is load-bearing: if we flipped the mode without a
            # recorded approval, a restart would surface the original
            # "mode=default with pending plan" bug. Abort the flip
            # and surface the error via the Pattern-3 envelope.
            if flip_mode:
                logger.warning(
                    "plan decision persist failed on approve path — "
                    "mode flip aborted to preserve invariant: %s",
                    exc,
                )
                return PlanDecisionResult(
                    run_id=run_id,
                    decision=decision,
                    ok=False,
                    error=f"persist failed, mode flip aborted: {exc}",
                )
            # Dismiss path — no mode flip to guard, best-effort
            # persistence loss is acceptable (the next decision
            # rewrites the blob anyway).
            logger.debug("plan decision persist failed: %s", exc)
        mode_status = ""
        if flip_mode:
            mode_status = session.set_permission_mode("default")
        # ``mode="json"`` collapses :class:`PlanDecision` StrEnum
        # to its ``.value`` string so downstream broadcast
        # consumers (transport adapters, TS clients) see the
        # historic ``"decision": "approved"`` wire shape rather
        # than an enum instance.
        session.broadcast_bus.emit(
            BroadcastEvent(
                channel="plan_decided",
                payload=PlanDecidedBroadcast(run_id=run_id, decision=decision).model_dump(
                    mode="json"
                ),
            )
        )
        return PlanDecisionResult(
            run_id=run_id,
            decision=decision,
            mode_status=mode_status,
        )


# ── Legacy re-export ─────────────────────────────────────────
#
# :class:`PlanDecisionResult` now lives in
# :mod:`~ember_code.core.session.schemas` alongside sibling
# coordinator wire types. The re-export below keeps existing
# ``from ember_code.core.session.plan_ops import
# PlanDecisionResult`` imports working.

__all__ = ["PlanCoordinator", "PlanDecisionResult"]
