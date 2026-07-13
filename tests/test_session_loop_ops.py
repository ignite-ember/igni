"""Unit tests for ``session/loop_ops.py``.

Extracted in iter 140 — 7 free functions pumping session
``loop_*`` fields + ``loop_store``. Session integration coverage
lives in `test_plan_mode.py` / `test_plan_decisions.py` /
`test_stop_hook.py`; these tests pin the tricky state-machine
invariants in isolation:

* ``advance_loop`` with an *implicit* cap PAUSES at
  ``LOOP_HARD_CAP`` (doesn't terminate) — the user can resume
  past the safety net.
* ``advance_loop`` with an *explicit* cap TERMINATES at the
  user's N — honouring "exactly N iterations."
* Paused loops don't auto-advance.
* ``cancel_loop`` returns False when no loop is active (idempotent).
* ``resume_loop`` returns None when the loop is already pumping
  (not paused) — no double-fire.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ember_code.core.loop.limits import LOOP_HARD_CAP
from ember_code.core.session.loop_ops import (
    advance_loop,
    cancel_loop,
    pause_loop,
    resume_loop,
    start_loop,
)


def _bare_session():
    """Session-shaped stub carrying only what loop_ops mutates."""
    session = SimpleNamespace()
    session.pending_loop_prompt = None
    session.loop_run_id = None
    session.loop_iteration_index = 0
    session.loop_iterations_remaining = 0
    session.loop_cap_explicit = False
    session.loop_paused = False
    session.loop_store = SimpleNamespace()
    session.loop_store.load = AsyncMock(return_value=None)
    session.loop_store.save = AsyncMock()
    session.loop_store.clear = AsyncMock()
    return session


class TestStartLoop:
    @pytest.mark.asyncio
    async def test_immediate_starts_at_iteration_1(self):
        s = _bare_session()
        run_id = await start_loop(s, "hi", 5, immediate=True, cap_explicit=True)
        assert run_id is not None
        assert s.pending_loop_prompt == "hi"
        assert s.loop_iteration_index == 1
        assert s.loop_iterations_remaining == 4

    @pytest.mark.asyncio
    async def test_deferred_starts_at_iteration_0(self):
        # Agent-tool path — first advance bumps to iteration 1.
        s = _bare_session()
        await start_loop(s, "hi", 5, immediate=False, cap_explicit=True)
        assert s.loop_iteration_index == 0
        assert s.loop_iterations_remaining == 5

    @pytest.mark.asyncio
    async def test_clears_paused_flag(self):
        s = _bare_session()
        s.loop_paused = True
        await start_loop(s, "hi", 5, immediate=True, cap_explicit=True)
        assert s.loop_paused is False


class TestAdvanceLoop:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_active_loop(self):
        s = _bare_session()
        assert await advance_loop(s) is None

    @pytest.mark.asyncio
    async def test_returns_none_when_paused(self):
        # Headline invariant: paused loops don't auto-advance.
        s = _bare_session()
        s.pending_loop_prompt = "hi"
        s.loop_paused = True
        assert await advance_loop(s) is None

    @pytest.mark.asyncio
    async def test_explicit_cap_terminates_at_zero_remaining(self):
        s = _bare_session()
        await start_loop(s, "hi", 3, immediate=True, cap_explicit=True)
        # 3 iterations: initial start = iteration 1 (remaining 2),
        # advances to 2 then 3, next advance terminates.
        await advance_loop(s)  # iteration 2
        await advance_loop(s)  # iteration 3
        # Next advance hits remaining==0 with explicit cap → terminate.
        result = await advance_loop(s)
        assert result is not None
        assert result.completed is True
        # Loop state cleared.
        assert s.pending_loop_prompt is None

    @pytest.mark.asyncio
    async def test_implicit_cap_pauses_at_hard_cap(self):
        # Implicit safety net → pause at LOOP_HARD_CAP, don't terminate.
        # The user can /loop resume to keep going.
        s = _bare_session()
        s.pending_loop_prompt = "hi"
        s.loop_run_id = "r"
        s.loop_iteration_index = LOOP_HARD_CAP
        s.loop_iterations_remaining = 0
        s.loop_cap_explicit = False
        result = await advance_loop(s)
        assert result is not None
        assert result.safety_cap_paused is True
        assert s.loop_paused is True

    @pytest.mark.asyncio
    async def test_implicit_cap_auto_extends_before_hard_cap(self):
        # Below the hard cap, implicit loops just extend the budget
        # and keep firing.
        s = _bare_session()
        s.pending_loop_prompt = "hi"
        s.loop_run_id = "r"
        s.loop_iteration_index = 20  # well below LOOP_HARD_CAP
        s.loop_iterations_remaining = 0
        s.loop_cap_explicit = False
        result = await advance_loop(s)
        assert result is not None
        assert result.auto_extended is True

    @pytest.mark.asyncio
    async def test_returns_wrapped_prompt(self):
        # The result carries a wrapped prompt (with autonomous-loop
        # meta-instructions) plus display_prompt (bare) plus counter
        # fields.
        s = _bare_session()
        await start_loop(s, "user text", 5, immediate=False, cap_explicit=True)
        result = await advance_loop(s)
        assert result is not None
        assert result.display_prompt == "user text"
        assert result.prompt != "user text"  # wrapped
        assert result.iteration == 1
        assert result.cap_explicit is True


class TestCancelLoop:
    @pytest.mark.asyncio
    async def test_returns_false_when_no_loop(self):
        s = _bare_session()
        assert await cancel_loop(s) is False

    @pytest.mark.asyncio
    async def test_clears_all_state(self):
        s = _bare_session()
        await start_loop(s, "hi", 3, immediate=True, cap_explicit=True)
        assert await cancel_loop(s) is True
        assert s.pending_loop_prompt is None
        assert s.loop_iteration_index == 0
        assert s.loop_iterations_remaining == 0
        assert s.loop_run_id is None


class TestPauseLoop:
    @pytest.mark.asyncio
    async def test_returns_false_when_no_loop(self):
        s = _bare_session()
        assert await pause_loop(s) is False

    @pytest.mark.asyncio
    async def test_sets_paused_flag(self):
        s = _bare_session()
        await start_loop(s, "hi", 3, immediate=True, cap_explicit=True)
        assert await pause_loop(s) is True
        assert s.loop_paused is True


class TestResumeLoop:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_loop(self):
        s = _bare_session()
        assert await resume_loop(s) is None

    @pytest.mark.asyncio
    async def test_returns_none_when_not_paused(self):
        # Only-paused-loops-resume invariant — a pumping loop
        # doesn't accept a resume signal.
        s = _bare_session()
        await start_loop(s, "hi", 3, immediate=True, cap_explicit=True)
        assert await resume_loop(s) is None

    @pytest.mark.asyncio
    async def test_unpauses_and_returns_wrapped_prompt(self):
        s = _bare_session()
        await start_loop(s, "hi", 3, immediate=True, cap_explicit=True)
        await pause_loop(s)
        wrapped = await resume_loop(s)
        assert wrapped is not None
        # Wrapped, not raw.
        assert wrapped != "hi"
        assert s.loop_paused is False
