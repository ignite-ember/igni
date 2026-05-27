"""Tests for the in-session ``/loop`` primitive.

Covers three surfaces that all read/write the same Session fields:

  1. **Slash command** — ``/loop <prompt>``, ``/loop stop``, ``/loop``
     status, dispatched through ``CommandHandler``.
  2. **Agent tool** — ``loop_start`` / ``loop_stop`` / ``loop_status``
     on ``LoopTools``, called by the LLM in plain conversation.
  3. **RPC iteration counter** — ``BackendServer.pop_pending_loop_iteration``
     and ``cancel_pending_loop``, called by the FE run controller
     after each turn to fire the next iteration or cancel the loop.

The Session is heavyweight (knowledge index, learning machine, model
registry, …) so we use a tiny stub that just carries the four fields
``/loop`` cares about. That keeps these unit tests fast and free of
chroma / HTTP side effects.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ember_code.backend.command_handler import CommandHandler
from ember_code.backend.server import BackendServer
from ember_code.core.tools.loop import LoopTools

# ── Helpers ─────────────────────────────────────────────────────────


def _fake_session() -> SimpleNamespace:
    """Bare Session stand-in carrying just the loop fields.

    The real :class:`Session` has dozens of subsystems we don't need
    to exercise here. The command + tool only read/write the four
    loop fields, so a SimpleNamespace is sufficient.
    """
    return SimpleNamespace(
        pending_loop_prompt=None,
        loop_iteration_index=0,
        loop_iterations_remaining=0,
    )


# ── Slash command ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_loop_start_via_slash_sets_session_state():
    sess = _fake_session()
    handler = CommandHandler(sess)

    result = await handler.handle("/loop fix the typo in foo.py, bar.py")

    assert result.action == "run_prompt"
    assert result.content == "fix the typo in foo.py, bar.py"
    assert sess.pending_loop_prompt == "fix the typo in foo.py, bar.py"
    assert sess.loop_iteration_index == 0
    assert sess.loop_iterations_remaining == 30  # default cap


@pytest.mark.asyncio
async def test_loop_start_with_explicit_cap():
    sess = _fake_session()
    handler = CommandHandler(sess)

    await handler.handle("/loop 5 do the thing")

    assert sess.pending_loop_prompt == "do the thing"
    assert sess.loop_iterations_remaining == 5


@pytest.mark.asyncio
async def test_loop_start_with_explicit_cap_x_suffix():
    sess = _fake_session()
    handler = CommandHandler(sess)

    await handler.handle("/loop 7x do the thing")

    assert sess.pending_loop_prompt == "do the thing"
    assert sess.loop_iterations_remaining == 7


@pytest.mark.asyncio
async def test_loop_refuses_zero_or_negative_cap():
    handler = CommandHandler(_fake_session())
    result = await handler.handle("/loop 0 the prompt")
    assert result.kind == "error"


@pytest.mark.asyncio
async def test_loop_refuses_cap_above_hard_ceiling():
    handler = CommandHandler(_fake_session())
    result = await handler.handle("/loop 999 the prompt")
    assert result.kind == "error"
    assert "hard cap" in result.content.lower()


@pytest.mark.asyncio
async def test_loop_refuses_empty_prompt():
    handler = CommandHandler(_fake_session())
    result = await handler.handle("/loop 5")  # cap but no prompt
    assert result.kind == "error"
    assert "prompt" in result.content.lower()


@pytest.mark.asyncio
async def test_loop_refuses_starting_on_active_loop():
    sess = _fake_session()
    handler = CommandHandler(sess)

    await handler.handle("/loop first prompt")
    result = await handler.handle("/loop second prompt")

    assert result.kind == "error"
    assert "already" in result.content.lower()
    # First loop's state must be intact.
    assert sess.pending_loop_prompt == "first prompt"


@pytest.mark.asyncio
async def test_loop_stop_clears_state():
    sess = _fake_session()
    handler = CommandHandler(sess)

    await handler.handle("/loop fix the bug")
    sess.loop_iteration_index = 3  # simulate three iterations done
    sess.loop_iterations_remaining = 27

    result = await handler.handle("/loop stop")

    assert result.kind == "info"
    assert "3" in result.content
    assert sess.pending_loop_prompt is None
    assert sess.loop_iterations_remaining == 0


@pytest.mark.asyncio
async def test_loop_stop_when_nothing_active_is_safe():
    handler = CommandHandler(_fake_session())
    result = await handler.handle("/loop stop")
    assert result.kind == "info"
    assert "no loop" in result.content.lower()


@pytest.mark.asyncio
async def test_loop_status_reports_active():
    sess = _fake_session()
    handler = CommandHandler(sess)
    await handler.handle("/loop 10 my prompt")
    sess.loop_iteration_index = 2
    sess.loop_iterations_remaining = 8

    result = await handler.handle("/loop")
    assert result.kind == "info"
    assert "2" in result.content
    assert "8" in result.content


@pytest.mark.asyncio
async def test_loop_status_reports_inactive():
    handler = CommandHandler(_fake_session())
    result = await handler.handle("/loop")
    assert result.kind == "info"
    assert "no loop" in result.content.lower()


# ── Agent tool ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_loop_start_tool_sets_state():
    sess = _fake_session()
    tools = LoopTools(sess)

    msg = await tools.loop_start("repeat me", max_iterations=5)

    assert "armed" in msg.lower() or "5" in msg
    assert sess.pending_loop_prompt == "repeat me"
    assert sess.loop_iterations_remaining == 5
    assert sess.loop_iteration_index == 0


@pytest.mark.asyncio
async def test_loop_start_tool_rejects_active_loop():
    sess = _fake_session()
    sess.pending_loop_prompt = "first"
    sess.loop_iterations_remaining = 10
    tools = LoopTools(sess)

    msg = await tools.loop_start("second", max_iterations=3)

    assert msg.startswith("ERROR")
    assert sess.pending_loop_prompt == "first"  # untouched


@pytest.mark.asyncio
async def test_loop_start_tool_rejects_empty_prompt():
    tools = LoopTools(_fake_session())
    assert (await tools.loop_start("", max_iterations=5)).startswith("ERROR")
    assert (await tools.loop_start("   ", max_iterations=5)).startswith("ERROR")


@pytest.mark.asyncio
async def test_loop_start_tool_rejects_bad_caps():
    tools = LoopTools(_fake_session())
    assert (await tools.loop_start("x", max_iterations=0)).startswith("ERROR")
    assert (await tools.loop_start("x", max_iterations=-1)).startswith("ERROR")
    assert (await tools.loop_start("x", max_iterations=10_000)).startswith("ERROR")


@pytest.mark.asyncio
async def test_loop_stop_tool_clears_state():
    sess = _fake_session()
    sess.pending_loop_prompt = "active"
    sess.loop_iteration_index = 4
    sess.loop_iterations_remaining = 6
    tools = LoopTools(sess)

    msg = await tools.loop_stop()

    assert "4" in msg
    assert sess.pending_loop_prompt is None
    assert sess.loop_iterations_remaining == 0


@pytest.mark.asyncio
async def test_loop_stop_tool_safe_when_idle():
    tools = LoopTools(_fake_session())
    msg = await tools.loop_stop()
    assert "no loop" in msg.lower()


@pytest.mark.asyncio
async def test_loop_status_tool_reports_state():
    sess = _fake_session()
    tools = LoopTools(sess)

    assert "no loop" in (await tools.loop_status()).lower()

    sess.pending_loop_prompt = "x"
    sess.loop_iteration_index = 1
    sess.loop_iterations_remaining = 9
    status = await tools.loop_status()
    assert "1" in status
    assert "9" in status


# ── Slash + tool see the same state ────────────────────────────────


@pytest.mark.asyncio
async def test_slash_and_tool_share_session_state():
    """The two surfaces must read/write the SAME fields so behaviour
    is consistent regardless of how the loop was started/stopped."""
    sess = _fake_session()
    handler = CommandHandler(sess)
    tools = LoopTools(sess)

    # Start via slash, stop via tool.
    await handler.handle("/loop 8 repeat me")
    assert sess.pending_loop_prompt == "repeat me"
    msg = await tools.loop_stop()
    assert sess.pending_loop_prompt is None
    assert "0" in msg  # zero iterations done before stop

    # Start via tool, stop via slash.
    await tools.loop_start("again", max_iterations=4)
    assert sess.pending_loop_prompt == "again"
    result = await handler.handle("/loop stop")
    assert sess.pending_loop_prompt is None
    assert result.kind == "info"


# ── RPC iteration counter (BackendServer) ───────────────────────────
#
# These methods are what the FE run controller calls after each turn
# to fire the next iteration or cancel the loop. They mutate Session
# fields and contain the loop's state machine — uncovered in the
# slash + tool tests because those test only "arm the state", not
# "consume it tick by tick".


class _FakeBackend:
    """Stand-in for ``BackendServer`` that only carries ``_session``.

    The real backend pulls in the scheduler, RPC plumbing, hooks, and
    so on. We only need the two ``loop``-related methods, which read
    ``self._session`` and mutate its loop fields — duck-typed access
    is enough.
    """

    def __init__(self, session: SimpleNamespace) -> None:
        self._session = session

    # Bind the actual methods so any change in the real BackendServer
    # is exercised here too.
    pop_pending_loop_iteration = BackendServer.pop_pending_loop_iteration
    cancel_pending_loop = BackendServer.cancel_pending_loop


@pytest.mark.asyncio
async def test_pop_iteration_returns_none_when_no_loop():
    backend = _FakeBackend(_fake_session())
    assert await backend.pop_pending_loop_iteration() is None


@pytest.mark.asyncio
async def test_pop_iteration_returns_descriptor_and_decrements():
    sess = _fake_session()
    sess.pending_loop_prompt = "do X"
    sess.loop_iterations_remaining = 3
    sess.loop_iteration_index = 0
    backend = _FakeBackend(sess)

    desc = await backend.pop_pending_loop_iteration()

    assert desc == {"prompt": "do X", "iteration": 1, "remaining": 2}
    assert sess.loop_iterations_remaining == 2
    assert sess.loop_iteration_index == 1
    # Loop must still be active until cap is hit.
    assert sess.pending_loop_prompt == "do X"


@pytest.mark.asyncio
async def test_pop_iteration_full_lifecycle_to_cap():
    """Walk the full state machine: arm cap=3, pop 3 times, then a 4th
    pop must emit the completion marker AND clear state; a 5th pop
    must return None (no double-render)."""
    sess = _fake_session()
    sess.pending_loop_prompt = "tick"
    sess.loop_iterations_remaining = 3
    backend = _FakeBackend(sess)

    # 3 successful pops, each decrementing remaining and incrementing
    # the index in lockstep.
    d1 = await backend.pop_pending_loop_iteration()
    d2 = await backend.pop_pending_loop_iteration()
    d3 = await backend.pop_pending_loop_iteration()
    assert d1 == {"prompt": "tick", "iteration": 1, "remaining": 2}
    assert d2 == {"prompt": "tick", "iteration": 2, "remaining": 1}
    assert d3 == {"prompt": "tick", "iteration": 3, "remaining": 0}

    # 4th pop hits the cap — returns the completion marker and clears.
    d4 = await backend.pop_pending_loop_iteration()
    assert d4 == {"completed": True, "total_iterations": 3}
    assert sess.pending_loop_prompt is None
    assert sess.loop_iterations_remaining == 0

    # 5th pop: state is cleared, so we get None (the FE renders
    # nothing — no double "Loop completed" message).
    assert await backend.pop_pending_loop_iteration() is None


@pytest.mark.asyncio
async def test_pop_iteration_completion_marker_only_fires_once():
    """The completion marker is one-shot. Re-calling after it has
    fired must return None so the FE doesn't render the summary
    twice across two ``_check_loop_continuation`` ticks."""
    sess = _fake_session()
    sess.pending_loop_prompt = "x"
    sess.loop_iterations_remaining = 1
    backend = _FakeBackend(sess)

    # First pop = iteration 1 descriptor.
    assert (await backend.pop_pending_loop_iteration())["iteration"] == 1
    # Second pop = completion marker (cap exhausted).
    second = await backend.pop_pending_loop_iteration()
    assert second is not None
    assert second.get("completed") is True
    # Third pop = nothing.
    assert await backend.pop_pending_loop_iteration() is None
    assert await backend.pop_pending_loop_iteration() is None


@pytest.mark.asyncio
async def test_cancel_pending_loop_returns_false_when_idle():
    backend = _FakeBackend(_fake_session())
    assert await backend.cancel_pending_loop() is False


@pytest.mark.asyncio
async def test_cancel_pending_loop_clears_state_and_returns_true():
    sess = _fake_session()
    sess.pending_loop_prompt = "ongoing"
    sess.loop_iteration_index = 2
    sess.loop_iterations_remaining = 8
    backend = _FakeBackend(sess)

    assert await backend.cancel_pending_loop() is True
    assert sess.pending_loop_prompt is None
    assert sess.loop_iterations_remaining == 0


# ── End-to-end: command → RPC → cap exhaustion ─────────────────────


@pytest.mark.asyncio
async def test_full_flow_command_arms_rpc_drains_cap_terminates():
    """The intended flow: user runs ``/loop 2 ping``, FE-side RPC pops
    iterations one at a time, and after 2 pops the loop is naturally
    over with state cleared."""
    sess = _fake_session()
    handler = CommandHandler(sess)
    backend = _FakeBackend(sess)

    # 1. Slash command arms the loop.
    armed = await handler.handle("/loop 2 ping")
    assert armed.action == "run_prompt"
    assert armed.content == "ping"
    assert sess.loop_iterations_remaining == 2

    # 2. First iteration's RPC tick.
    d1 = await backend.pop_pending_loop_iteration()
    assert d1["prompt"] == "ping"
    assert d1["iteration"] == 1
    assert d1["remaining"] == 1

    # 3. Second iteration's RPC tick — last one inside the cap.
    d2 = await backend.pop_pending_loop_iteration()
    assert d2["prompt"] == "ping"
    assert d2["iteration"] == 2
    assert d2["remaining"] == 0

    # 4. Third RPC tick — cap exhausted, returns the completion
    # marker (so the FE renders the summary) and clears state.
    completion = await backend.pop_pending_loop_iteration()
    assert completion == {"completed": True, "total_iterations": 2}
    assert sess.pending_loop_prompt is None

    # 5. A subsequent tick returns None — completion is one-shot.
    assert await backend.pop_pending_loop_iteration() is None

    # 6. After cap, ``/loop`` status reports no active loop again so
    # a fresh ``/loop`` can be started without "already active" error.
    status = await handler.handle("/loop")
    assert "no loop" in status.content.lower()
    fresh = await handler.handle("/loop new prompt")
    assert fresh.action == "run_prompt"
    assert sess.pending_loop_prompt == "new prompt"


@pytest.mark.asyncio
async def test_user_input_cancellation_via_rpc():
    """If the user types a non-/loop message mid-loop, the FE calls
    ``cancel_pending_loop`` to interrupt. After that, the next RPC
    pop must return None — the loop is fully cancelled, not just
    skipped one tick."""
    sess = _fake_session()
    handler = CommandHandler(sess)
    backend = _FakeBackend(sess)

    await handler.handle("/loop 5 keep going")
    # One iteration runs.
    assert await backend.pop_pending_loop_iteration() is not None
    assert sess.loop_iteration_index == 1

    # User types something else → FE invokes cancel.
    assert await backend.cancel_pending_loop() is True

    # No more iterations.
    assert await backend.pop_pending_loop_iteration() is None
    assert sess.pending_loop_prompt is None


# ── Help is registered ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_help_loop_topic_exists():
    """``/help loop`` must return the help markdown — wiring check."""
    handler = CommandHandler(_fake_session())
    result = await handler.handle("/help loop")
    assert result.kind == "markdown"
    body = result.content.lower()
    # Spot-check key bits the user needs to know about.
    assert "/loop" in body
    assert "stop" in body
    assert "iteration" in body
