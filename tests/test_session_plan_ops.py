"""Unit tests for ``session/plan_ops.py``.

Extracted from Session in iter 142. `test_plan_decisions.py`
covers the Session-method delegation; these tests pin the
free-function API's contract in isolation, especially the
persist-before-flip-mode ordering that was the whole point of
this code path existing (regression from v0.7.x where a
crash mid-flip left users with mode=default and no recorded
approval).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.core.session.plan_ops import (
    _record_plan_decision,
    approve_plan,
    dismiss_plan,
)


def _bare_session(*, has_persistence: bool = True):
    """Session-shaped stub carrying only what plan_ops reads."""
    session = SimpleNamespace()
    session.plan_store = MagicMock()
    session.plan_store.decisions_snapshot = MagicMock(return_value={"r1": "approved"})
    if has_persistence:
        session.persistence = SimpleNamespace()
        session.persistence.save_plan_decisions = AsyncMock()
    session.set_permission_mode = MagicMock(return_value="mode → default")
    session.broadcast = MagicMock()
    return session


class TestApprovePlan:
    @pytest.mark.asyncio
    async def test_approves_and_flips_mode(self):
        s = _bare_session()
        out = await approve_plan(s, "run-1")
        s.plan_store.set_decision.assert_called_once_with("run-1", "approved")
        s.set_permission_mode.assert_called_once_with("default")
        assert out.decision == "approved"
        assert out.run_id == "run-1"

    @pytest.mark.asyncio
    async def test_broadcasts_plan_decided(self):
        s = _bare_session()
        await approve_plan(s, "run-1")
        s.broadcast.assert_called_once()
        channel, payload = s.broadcast.call_args.args
        assert channel == "plan_decided"
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
        s.persistence.save_plan_decisions = AsyncMock(
            side_effect=lambda _: order.append("save")
        )
        s.set_permission_mode = MagicMock(side_effect=lambda _: order.append("flip") or "ok")
        await approve_plan(s, "run-1")
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
        out = await dismiss_plan(s, "run-1")
        s.plan_store.set_decision.assert_called_once_with("run-1", "dismissed")
        s.set_permission_mode.assert_not_called()
        assert out.decision == "dismissed"
        assert out.mode_status == ""


class TestRecordPlanDecision:
    @pytest.mark.asyncio
    async def test_empty_run_id_raises(self):
        s = _bare_session()
        with pytest.raises(ValueError, match="non-empty"):
            await _record_plan_decision(s, "", "approved", flip_mode=True)

    @pytest.mark.asyncio
    async def test_missing_plan_store_raises(self):
        s = _bare_session()
        s.plan_store = None
        with pytest.raises(RuntimeError, match="plan_store not initialised"):
            await _record_plan_decision(s, "run-1", "approved", flip_mode=True)

    @pytest.mark.asyncio
    async def test_persist_failure_does_not_stop_flip(self):
        # If the DB write fails, the mode flip should STILL happen —
        # session state is what the user sees in the UI, and the
        # persist failure gets logged but doesn't abort.
        s = _bare_session()
        s.persistence.save_plan_decisions = AsyncMock(side_effect=RuntimeError("db down"))
        out = await _record_plan_decision(s, "run-1", "approved", flip_mode=True)
        # set_permission_mode still fired.
        s.set_permission_mode.assert_called_once()
        assert out.decision == "approved"

    @pytest.mark.asyncio
    async def test_no_persistence_attr_is_ok(self):
        # A Session that doesn't have a persistence attr (test-double
        # / partial init) should still record + flip without raising.
        s = _bare_session(has_persistence=False)
        out = await _record_plan_decision(s, "run-1", "approved", flip_mode=True)
        assert out.decision == "approved"
