"""Unit tests for :class:`PlanCoordinator` on ``session/plan_ops.py``.

The module now exposes only :class:`PlanCoordinator` — the four
back-compat free functions (``approve_plan`` / ``dismiss_plan`` /
``_record_plan_decision`` / ``_coord_for``) were deleted after
grep confirmed no live external callers. These tests drive the
coordinator methods directly on a bare Session stub with a
freshly-attached ``session.plan = PlanCoordinator(session)``.

Headline invariant pinned here: persist-before-flip-mode
ordering — the whole point of this code path existing (regression
from v0.7.x where a crash mid-flip left users with mode=default
and no recorded approval). The post-refactor semantic is
STRICTER than the pre-refactor "log-and-hope": if the DB write
fails on the approve path, the mode flip is aborted and the
coordinator returns ``PlanDecisionResult(ok=False)``.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.core.session.broadcast import BroadcastBus
from ember_code.core.session.plan_ops import PlanCoordinator
from ember_code.core.tools.plan import PlanDecision, PlanDecisionsBlob


def _bare_session(*, has_persistence: bool = True):
    """Session-shaped stub carrying only what plan_ops reads,
    with a freshly-attached :class:`PlanCoordinator`."""
    session = SimpleNamespace()
    session.plan_store = MagicMock()
    # ``plan_ops`` reads ``.decisions`` off the blob, so the stub
    # must return the typed model rather than the legacy dict.
    session.plan_store.decisions_snapshot = MagicMock(
        return_value=PlanDecisionsBlob(decisions={"r1": PlanDecision.APPROVED})
    )
    if has_persistence:
        session.persistence = SimpleNamespace()
        session.persistence.save_plan_decisions = AsyncMock()
    session.set_permission_mode = MagicMock(return_value="mode → default")
    session.broadcast_bus = BroadcastBus()
    session.plan = PlanCoordinator(session)
    return session


class TestApprovePlan:
    @pytest.mark.asyncio
    async def test_approves_and_flips_mode(self):
        s = _bare_session()
        out = await s.plan.approve("run-1")
        # The coordinator now hands :class:`PlanDecision` to the
        # store, not a raw string — type-precision matters here.
        s.plan_store.set_decision.assert_called_once_with("run-1", PlanDecision.APPROVED)
        s.set_permission_mode.assert_called_once_with("default")
        assert out.decision == PlanDecision.APPROVED
        assert out.run_id == "run-1"
        assert out.ok is True

    @pytest.mark.asyncio
    async def test_broadcasts_plan_decided(self):
        s = _bare_session()
        captured: list[tuple[str, dict]] = []
        s.broadcast_bus.register(lambda ch, p: captured.append((ch, p)))
        await s.plan.approve("run-1")
        assert len(captured) == 1
        channel, payload = captured[0]
        assert channel == "plan_decided"
        # StrEnum ``PlanDecision`` dumps as its .value so the wire
        # shape stays the historic ``{"decision": "approved"}``.
        assert payload == {"run_id": "run-1", "decision": "approved"}

    @pytest.mark.asyncio
    async def test_persist_before_flip_mode(self):
        # Headline invariant: save_plan_decisions MUST run before
        # set_permission_mode. A crash mid-flip should leave the DB
        # with the recorded approval so the restart doesn't
        # regress into "mode=default but no approval" — the exact
        # class-of-bug that motivated persisting decisions at all.
        s = _bare_session()
        order = []
        s.persistence.save_plan_decisions = AsyncMock(side_effect=lambda _: order.append("save"))
        s.set_permission_mode = MagicMock(side_effect=lambda _: order.append("flip") or "ok")
        await s.plan.approve("run-1")
        assert order == ["save", "flip"], (
            "save_plan_decisions must run BEFORE set_permission_mode — "
            "a crash between the two must never leave DB behind mode."
        )


class TestDismissPlan:
    @pytest.mark.asyncio
    async def test_dismisses_without_flipping_mode(self):
        # The whole point of dismiss: user wants to iterate on the
        # plan, so they stay IN plan mode.
        s = _bare_session()
        out = await s.plan.dismiss("run-1")
        s.plan_store.set_decision.assert_called_once_with("run-1", PlanDecision.DISMISSED)
        s.set_permission_mode.assert_not_called()
        assert out.decision == PlanDecision.DISMISSED
        assert out.mode_status == ""
        assert out.ok is True


class TestRecordPlanDecision:
    @pytest.mark.asyncio
    async def test_empty_run_id_returns_error_envelope(self):
        # Post-refactor: no more raise, callers get a Pattern-3
        # envelope with ``ok=False`` + a diagnostic ``error``.
        s = _bare_session()
        out = await s.plan._record("", PlanDecision.APPROVED, flip_mode=True)
        assert out.ok is False
        assert out.error is not None and "non-empty" in out.error
        # Guardrail: no side effects on the refusal path.
        s.plan_store.set_decision.assert_not_called()
        s.set_permission_mode.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_plan_store_returns_error_envelope(self):
        s = _bare_session()
        s.plan_store = None
        out = await s.plan._record("run-1", PlanDecision.APPROVED, flip_mode=True)
        assert out.ok is False
        assert out.error is not None and "plan_store" in out.error
        s.set_permission_mode.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_persistence_returns_error_envelope(self):
        # Persistence is now a required Session collaborator — the
        # old "log-and-continue" branch is gone. A session missing
        # persistence surfaces a clear error instead of silently
        # skipping the write.
        s = _bare_session(has_persistence=False)
        out = await s.plan._record("run-1", PlanDecision.APPROVED, flip_mode=True)
        assert out.ok is False
        assert out.error is not None and "persistence" in out.error
        # Store IS updated (the decision was recorded in memory)
        # but no flip and no broadcast happened.
        s.plan_store.set_decision.assert_called_once()
        s.set_permission_mode.assert_not_called()

    @pytest.mark.asyncio
    async def test_persist_failure_aborts_flip(self):
        # Behavioural tightening: previously a persist failure on
        # the approve path was swallowed and the mode still flipped.
        # That violated the persist-before-flip invariant — after
        # a restart the user would see mode=default with no
        # recorded approval, i.e. the original bug this whole
        # module exists to prevent. Post-refactor the coordinator
        # returns ``ok=False`` and refuses to flip.
        s = _bare_session()
        s.persistence.save_plan_decisions = AsyncMock(side_effect=RuntimeError("db down"))
        out = await s.plan._record("run-1", PlanDecision.APPROVED, flip_mode=True)
        s.set_permission_mode.assert_not_called()
        assert out.ok is False
        assert out.error is not None and "persist failed" in out.error

    @pytest.mark.asyncio
    async def test_persist_failure_on_dismiss_is_best_effort(self):
        # Dismiss path has no mode flip to guard, so a persist
        # failure is still best-effort — the in-memory store
        # reflects the decision, the FE gets the live broadcast,
        # and the next decision rewrites the blob anyway.
        s = _bare_session()
        s.persistence.save_plan_decisions = AsyncMock(side_effect=RuntimeError("db down"))
        out = await s.plan._record("run-1", PlanDecision.DISMISSED, flip_mode=False)
        assert out.ok is True
        assert out.decision == PlanDecision.DISMISSED
