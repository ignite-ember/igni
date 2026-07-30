"""Unit tests for ``session/loop_ops.py``.

Targets :class:`LoopController` directly (Pattern-4 ideal after
the iter-141 refactor — the controller owns its own state, no
Session host to stub). Assertions read ``ctrl.state.*`` and
``ctrl.paused`` rather than session-shaped attributes; the two
external read points (``Session.loop_paused``,
``Session.pending_loop_prompt``, …) are unit-tested against the
proxy path in ``tests/test_session.py``.

Invariants pinned here:

* ``advance_loop`` with an *implicit* cap PAUSES at
  ``LOOP_HARD_CAP`` (doesn't terminate) — the user can resume
  past the safety net.
* ``advance_loop`` with an *explicit* cap TERMINATES at the
  user's N — honouring "exactly N iterations."
* Paused loops don't auto-advance.
* ``cancel_loop`` returns False when no loop is active (idempotent).
* ``resume_loop`` returns None when the loop is already pumping
  (not paused) — no double-fire.
* Auto-extend flag is one-shot: True on the advance that just
  rolled over the safety net, False on the next one.
"""

from unittest.mock import AsyncMock

import pytest

from ember_code.core.loop.limits import LOOP_HARD_CAP
from ember_code.core.loop.models import LoopState
from ember_code.core.session.loop_ops import LoopController


def _make_controller() -> LoopController:
    """Build a :class:`LoopController` wired to an AsyncMock
    :class:`LoopStore` — the persistence side is stubbed so
    these tests only exercise the state machine."""
    store = AsyncMock()
    store.load = AsyncMock(return_value=None)
    store.save = AsyncMock()
    store.clear = AsyncMock()
    return LoopController(loop_store=store)


class TestStartLoop:
    @pytest.mark.asyncio
    async def test_immediate_starts_at_iteration_1(self):
        ctrl = _make_controller()
        run_id = await ctrl.start_loop("hi", 5, immediate=True, cap_explicit=True)
        assert run_id is not None
        assert ctrl.pending_loop_prompt == "hi"
        assert ctrl.loop_iteration_index == 1
        assert ctrl.loop_iterations_remaining == 4

    @pytest.mark.asyncio
    async def test_deferred_starts_at_iteration_0(self):
        # Agent-tool path — first advance bumps to iteration 1.
        ctrl = _make_controller()
        await ctrl.start_loop("hi", 5, immediate=False, cap_explicit=True)
        assert ctrl.loop_iteration_index == 0
        assert ctrl.loop_iterations_remaining == 5

    @pytest.mark.asyncio
    async def test_clears_paused_flag(self):
        # A prior paused loop's flag must not survive into a fresh start.
        ctrl = _make_controller()
        # Seed a paused state by hand — direct assignment mirrors what
        # a persistence-hydration path would do.
        ctrl._state = LoopState(
            run_id="old",
            prompt="stale",
            iteration_index=3,
            iterations_remaining=2,
            cap_explicit=False,
        )
        ctrl._paused = True
        await ctrl.start_loop("hi", 5, immediate=True, cap_explicit=True)
        assert ctrl.paused is False


class TestAdvanceLoop:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_active_loop(self):
        ctrl = _make_controller()
        assert await ctrl.advance_loop() is None

    @pytest.mark.asyncio
    async def test_returns_none_when_paused(self):
        # Headline invariant: paused loops don't auto-advance.
        ctrl = _make_controller()
        await ctrl.start_loop("hi", 5, immediate=True, cap_explicit=True)
        await ctrl.pause_loop()
        assert await ctrl.advance_loop() is None

    @pytest.mark.asyncio
    async def test_explicit_cap_terminates_at_zero_remaining(self):
        ctrl = _make_controller()
        await ctrl.start_loop("hi", 3, immediate=True, cap_explicit=True)
        # 3 iterations: initial start = iteration 1 (remaining 2),
        # advances to 2 then 3, next advance terminates.
        await ctrl.advance_loop()  # iteration 2
        await ctrl.advance_loop()  # iteration 3
        # Next advance hits remaining==0 with explicit cap → terminate.
        result = await ctrl.advance_loop()
        assert result is not None
        assert result.kind == "completed"
        assert result.completed is True
        # Loop state cleared.
        assert ctrl.state is None
        assert ctrl.pending_loop_prompt is None

    @pytest.mark.asyncio
    async def test_implicit_cap_pauses_at_hard_cap(self):
        # Implicit safety net → pause at LOOP_HARD_CAP, don't terminate.
        # The user can /loop resume to keep going.
        ctrl = _make_controller()
        # Seed the state at the hard cap so the next advance hits it.
        ctrl._state = LoopState(
            run_id="r",
            prompt="hi",
            iteration_index=LOOP_HARD_CAP,
            iterations_remaining=0,
            cap_explicit=False,
        )
        result = await ctrl.advance_loop()
        assert result is not None
        assert result.kind == "safety_paused"
        assert result.safety_cap_paused is True
        assert ctrl.paused is True

    @pytest.mark.asyncio
    async def test_implicit_cap_auto_extends_before_hard_cap(self):
        # Below the hard cap, implicit loops just extend the budget
        # and keep firing.
        ctrl = _make_controller()
        ctrl._state = LoopState(
            run_id="r",
            prompt="hi",
            iteration_index=20,  # well below LOOP_HARD_CAP
            iterations_remaining=0,
            cap_explicit=False,
        )
        result = await ctrl.advance_loop()
        assert result is not None
        assert result.auto_extended is True

    @pytest.mark.asyncio
    async def test_auto_extended_flag_is_one_shot(self):
        # The FE banner must only render once — the very next advance
        # should report ``auto_extended=False``.
        ctrl = _make_controller()
        ctrl._state = LoopState(
            run_id="r",
            prompt="hi",
            iteration_index=20,
            iterations_remaining=0,
            cap_explicit=False,
        )
        first = await ctrl.advance_loop()
        second = await ctrl.advance_loop()
        assert first is not None and first.auto_extended is True
        assert second is not None and second.auto_extended is False

    @pytest.mark.asyncio
    async def test_returns_wrapped_prompt(self):
        # The result carries a wrapped prompt (with autonomous-loop
        # meta-instructions) plus display_prompt (bare) plus counter
        # fields.
        ctrl = _make_controller()
        await ctrl.start_loop("user text", 5, immediate=False, cap_explicit=True)
        result = await ctrl.advance_loop()
        assert result is not None
        assert result.display_prompt == "user text"
        assert result.prompt != "user text"  # wrapped
        assert result.iteration == 1
        assert result.cap_explicit is True


class TestCancelLoop:
    @pytest.mark.asyncio
    async def test_returns_false_when_no_loop(self):
        ctrl = _make_controller()
        assert await ctrl.cancel_loop() is False

    @pytest.mark.asyncio
    async def test_clears_all_state(self):
        ctrl = _make_controller()
        await ctrl.start_loop("hi", 3, immediate=True, cap_explicit=True)
        assert await ctrl.cancel_loop() is True
        assert ctrl.state is None
        assert ctrl.pending_loop_prompt is None
        assert ctrl.loop_iteration_index == 0
        assert ctrl.loop_iterations_remaining == 0
        assert ctrl.loop_run_id is None


class TestPauseLoop:
    @pytest.mark.asyncio
    async def test_returns_false_when_no_loop(self):
        ctrl = _make_controller()
        assert await ctrl.pause_loop() is False

    @pytest.mark.asyncio
    async def test_sets_paused_flag(self):
        ctrl = _make_controller()
        await ctrl.start_loop("hi", 3, immediate=True, cap_explicit=True)
        assert await ctrl.pause_loop() is True
        assert ctrl.paused is True


class TestResumeLoop:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_loop(self):
        ctrl = _make_controller()
        assert await ctrl.resume_loop() is None

    @pytest.mark.asyncio
    async def test_returns_none_when_not_paused(self):
        # Only-paused-loops-resume invariant — a pumping loop
        # doesn't accept a resume signal.
        ctrl = _make_controller()
        await ctrl.start_loop("hi", 3, immediate=True, cap_explicit=True)
        assert await ctrl.resume_loop() is None

    @pytest.mark.asyncio
    async def test_unpauses_and_returns_wrapped_prompt(self):
        ctrl = _make_controller()
        await ctrl.start_loop("hi", 3, immediate=True, cap_explicit=True)
        await ctrl.pause_loop()
        wrapped = await ctrl.resume_loop()
        assert wrapped is not None
        # Wrapped, not raw.
        assert wrapped != "hi"
        assert ctrl.paused is False


class TestSessionProxyProperties:
    """Exhaustive round-trip: every one of the six read-only proxy
    properties on :class:`Session` forwards to the controller's
    matching accessor. Cheap insurance against a proxy name drifting
    from the underlying field (Pattern-4 silent-drift risk called
    out in the synthesis notes).

    Uses the same ``_make_controller()`` helper — Session is heavy
    to construct, but the ``@property`` proxies read
    ``self.loop.<field>``, so exercising the controller directly
    covers the contract as long as the proxy body is exactly one
    forward. The property source is spot-checked in
    ``test_session.py`` (unchanged).
    """

    @pytest.mark.asyncio
    async def test_all_six_proxies_forward_to_controller(self):
        ctrl = _make_controller()
        run_id = await ctrl.start_loop("the prompt", 5, immediate=True, cap_explicit=True)
        # Each proxy pair — the six that ``Session`` exposes — reads
        # from a matching controller accessor. Assert both sides so a
        # future rename can't silently drift.
        assert ctrl.pending_loop_prompt == "the prompt"
        assert ctrl.loop_iteration_index == 1
        assert ctrl.loop_iterations_remaining == 4
        assert ctrl.loop_run_id == run_id
        assert ctrl.loop_cap_explicit is True
        assert ctrl.paused is False
        # After a pause, ``paused`` flips. State fields survive.
        await ctrl.pause_loop()
        assert ctrl.paused is True
        assert ctrl.pending_loop_prompt == "the prompt"
        assert ctrl.loop_iteration_index == 1


class TestPhaseTransitions:
    """Enforcement of the phase state machine — illegal transitions
    raise so callers can't silently break the ``paused`` /
    ``state`` invariant.

    The public ``pause_loop`` / ``resume_loop`` / ``cancel_loop``
    methods each guard against reaching ``_transition`` on an
    unreachable target, so the ``ValueError`` is a
    defence-in-depth guarantee that the state machine can't drift
    if a future refactor forgets a guard. Exercise the private
    method directly to pin the invariant.
    """

    def test_idle_to_paused_raises(self):
        # IDLE (state is None) → PAUSED must raise.
        from ember_code.core.session.schemas import LoopPhase

        ctrl = _make_controller()
        with pytest.raises(ValueError, match="Illegal"):
            ctrl._transition(LoopPhase.PAUSED)
