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

import uuid
from types import SimpleNamespace

import pytest

from ember_code.backend.command_handler import CommandHandler
from ember_code.backend.server import BackendServer
from ember_code.core.loop.limits import LOOP_DEFAULT_MAX_ITERATIONS, LOOP_HARD_CAP
from ember_code.core.loop.prompt import wrap_iteration_prompt
from ember_code.core.tools.loop import LoopTools

# ── Helpers ─────────────────────────────────────────────────────────


class _FakeProgressStore:
    """In-memory stand-in for :class:`LoopProgressStore`.

    Keyed by ``(run_id, key)`` like the real store, but skips the
    SQLite plumbing — these are unit tests of the tool surface, not
    of persistence. The real store is exercised separately in
    ``test_loop_store.py``.
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], str] = {}

    async def set(self, run_id: str, key: str, value: str) -> None:
        self._rows[(run_id, key)] = value

    async def get(self, run_id: str, key: str) -> str | None:
        return self._rows.get((run_id, key))

    async def list(self, run_id: str) -> list[tuple[str, str]]:
        return [(k, v) for (rid, k), v in self._rows.items() if rid == run_id]

    async def delete(self, run_id: str, key: str) -> bool:
        return self._rows.pop((run_id, key), None) is not None

    async def clear(self, run_id: str) -> int:
        keys = [k for k in self._rows if k[0] == run_id]
        for k in keys:
            del self._rows[k]
        return len(keys)


class _FakeSession:
    """Minimal Session stand-in carrying loop fields + helpers.

    The real :class:`Session` has dozens of subsystems we don't need
    here. We re-implement the four loop helpers (``start_loop`` /
    ``advance_loop`` / ``cancel_loop`` / ``resume_loop``) with the
    same semantics as the real Session — but without the SQLite
    persistence side-effect, since these unit tests are about the
    field state machine, not the store.
    """

    def __init__(self) -> None:
        self.pending_loop_prompt: str | None = None
        self.loop_iteration_index: int = 0
        self.loop_iterations_remaining: int = 0
        self.loop_run_id: str | None = None
        self.loop_paused: bool = False
        self.loop_cap_explicit: bool = False
        self._auto_extended_this_advance: bool = False
        # In-memory progress store stand-in keyed by (run_id, key)
        # — enough for ``loop_set_total`` and direct
        # ``loop_progress_*`` tool tests without paying for SQLite.
        self.loop_progress_store = _FakeProgressStore()

    async def start_loop(
        self,
        prompt: str,
        max_iter: int,
        *,
        immediate: bool,
        cap_explicit: bool,
    ) -> str:
        self.loop_run_id = str(uuid.uuid4())
        self.pending_loop_prompt = prompt
        self.loop_cap_explicit = cap_explicit
        self.loop_paused = False
        if immediate:
            self.loop_iteration_index = 1
            self.loop_iterations_remaining = max_iter - 1
        else:
            self.loop_iteration_index = 0
            self.loop_iterations_remaining = max_iter
        return self.loop_run_id

    async def advance_loop(self) -> dict | None:
        if self.pending_loop_prompt is None:
            return None
        if self.loop_paused:
            return None
        if self.loop_iterations_remaining <= 0:
            if self.loop_cap_explicit:
                total = self.loop_iteration_index
                await self.cancel_loop()
                return {"completed": True, "total_iterations": total}
            if self.loop_iteration_index >= LOOP_HARD_CAP:
                await self.pause_loop()
                return {
                    "safety_cap_paused": True,
                    "iteration": self.loop_iteration_index,
                }
            self.loop_iterations_remaining = min(
                LOOP_DEFAULT_MAX_ITERATIONS,
                LOOP_HARD_CAP - self.loop_iteration_index,
            )
            self._auto_extended_this_advance = True
        self.loop_paused = False
        self.loop_iterations_remaining -= 1
        self.loop_iteration_index += 1
        cap = (
            self.loop_iteration_index + self.loop_iterations_remaining
            if self.loop_cap_explicit
            else None
        )
        out = {
            "prompt": wrap_iteration_prompt(
                self.pending_loop_prompt, self.loop_iteration_index, cap
            ),
            "display_prompt": self.pending_loop_prompt,
            "iteration": self.loop_iteration_index,
            "remaining": self.loop_iterations_remaining,
            "cap_explicit": self.loop_cap_explicit,
        }
        if self._auto_extended_this_advance:
            out["auto_extended"] = True
            self._auto_extended_this_advance = False
        return out

    async def cancel_loop(self) -> bool:
        if self.pending_loop_prompt is None:
            return False
        self.pending_loop_prompt = None
        self.loop_iteration_index = 0
        self.loop_iterations_remaining = 0
        self.loop_run_id = None
        self.loop_paused = False
        self.loop_cap_explicit = False
        return True

    async def pause_loop(self) -> bool:
        if self.pending_loop_prompt is None:
            return False
        self.loop_paused = True
        return True

    async def resume_loop(self) -> str | None:
        if self.pending_loop_prompt is None or not self.loop_paused:
            return None
        self.loop_paused = False
        cap = (
            self.loop_iteration_index + self.loop_iterations_remaining
            if self.loop_cap_explicit
            else None
        )
        return wrap_iteration_prompt(self.pending_loop_prompt, self.loop_iteration_index, cap)


def _fake_session() -> _FakeSession:
    return _FakeSession()


# ── Slash command ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_loop_start_via_slash_sets_session_state():
    sess = _fake_session()
    handler = CommandHandler(sess)

    result = await handler.handle("/loop fix the typo in foo.py, bar.py")

    assert result.action == "run_prompt"
    # Slash-path content is wrapped with the autonomous-loop meta
    # instruction so the agent doesn't ask the user between
    # iterations; the original prompt is preserved verbatim *inside*
    # the wrapper. The user didn't supply a leading number so the
    # cap is *implicit* — the wrapper omits the ``total`` attribute
    # (the cap is just a safety net, not a target).
    assert "fix the typo in foo.py, bar.py" in result.content
    assert '<loop-iteration index="1">' in result.content
    assert "total=" not in result.content
    # The unwrapped prompt is shipped separately as ``display_content``
    # so the chat renders the bare prompt while the agent gets the
    # wrapped form.
    assert result.display_content == "fix the typo in foo.py, bar.py"
    # The session's cap_explicit reflects the absence of a leading
    # number — auto-extend behavior kicks in at cap-hit.
    assert sess.loop_cap_explicit is False
    # The session keeps the *unwrapped* prompt so the panel can
    # render it cleanly — only FE-bound prompts are wrapped.
    assert sess.pending_loop_prompt == "fix the typo in foo.py, bar.py"
    # Slash path uses ``immediate=True`` — iteration 1 is already
    # firing via the ``run_prompt`` action, so index=1, remaining=29
    # (sum is the configured cap).
    assert sess.loop_iteration_index == 1
    assert sess.loop_iterations_remaining == 29  # cap=30, iter 1 already in flight


@pytest.mark.asyncio
async def test_loop_start_with_explicit_cap():
    sess = _fake_session()
    handler = CommandHandler(sess)

    result = await handler.handle("/loop 5 do the thing")

    assert sess.pending_loop_prompt == "do the thing"
    # Same off-by-one as the default-cap test: iteration 1 has
    # already been booked via ``immediate=True`` so ``remaining``
    # is cap-1.
    assert sess.loop_iteration_index == 1
    assert sess.loop_iterations_remaining == 4
    # ``/loop N <prompt>`` is the *explicit* path — auto-extend
    # doesn't apply, and the wrapper carries the total.
    assert sess.loop_cap_explicit is True
    assert '<loop-iteration index="1" total="5">' in result.content


@pytest.mark.asyncio
async def test_loop_start_with_explicit_cap_x_suffix():
    sess = _fake_session()
    handler = CommandHandler(sess)

    await handler.handle("/loop 7x do the thing")

    assert sess.pending_loop_prompt == "do the thing"
    assert sess.loop_iteration_index == 1
    assert sess.loop_iterations_remaining == 6  # cap=7, iter 1 in flight


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
async def test_loop_no_args_opens_panel():
    """``/loop`` with no args now opens the TUI panel (action="loop")
    instead of printing a chat status block. Status info lives on
    the panel header which polls ``loop_status`` directly."""
    handler = CommandHandler(_fake_session())
    result = await handler.handle("/loop")
    assert result.action == "loop"


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
    # Slash-path start counts the immediately-firing iteration as
    # iteration 1 (immediate=True) so stopping right after start
    # reports ``1 iteration``, not zero.
    assert "1 iteration" in msg

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
    # Explicit cap → wrapper carries ``total="3"`` and termination
    # happens at the cap rather than auto-extending.
    sess.loop_cap_explicit = True
    backend = _FakeBackend(sess)

    desc = await backend.pop_pending_loop_iteration()

    # The descriptor's prompt is wrapped with the autonomous-loop
    # meta instruction (so the agent doesn't ask the user between
    # iterations) — the original prompt is preserved verbatim
    # inside the wrapper. ``display_prompt`` carries the unwrapped
    # form for chat rendering.
    assert desc["iteration"] == 1
    assert desc["remaining"] == 2
    assert "do X" in desc["prompt"]
    assert '<loop-iteration index="1" total="3">' in desc["prompt"]
    assert desc["display_prompt"] == "do X"
    assert sess.loop_iterations_remaining == 2
    assert sess.loop_iteration_index == 1
    # Session keeps the *unwrapped* prompt for the panel display.
    assert sess.pending_loop_prompt == "do X"


@pytest.mark.asyncio
async def test_pop_iteration_full_lifecycle_to_cap():
    """Walk the full state machine: arm cap=3, pop 3 times, then a 4th
    pop must emit the completion marker AND clear state; a 5th pop
    must return None (no double-render)."""
    sess = _fake_session()
    sess.pending_loop_prompt = "tick"
    sess.loop_iterations_remaining = 3
    sess.loop_cap_explicit = True  # Termination at cap, not auto-extend.
    backend = _FakeBackend(sess)

    # 3 successful pops, each decrementing remaining and incrementing
    # the index in lockstep. Prompt is wrapped with the
    # autonomous-loop meta on each iteration; the original "tick"
    # body is preserved verbatim inside.
    d1 = await backend.pop_pending_loop_iteration()
    d2 = await backend.pop_pending_loop_iteration()
    d3 = await backend.pop_pending_loop_iteration()
    assert (d1["iteration"], d1["remaining"]) == (1, 2)
    assert (d2["iteration"], d2["remaining"]) == (2, 1)
    assert (d3["iteration"], d3["remaining"]) == (3, 0)
    for d, n in ((d1, 1), (d2, 2), (d3, 3)):
        assert "tick" in d["prompt"]
        assert f'<loop-iteration index="{n}" total="3">' in d["prompt"]

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
    sess.loop_cap_explicit = True  # Terminate at cap.
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

    # 1. Slash command arms the loop. The returned content is the
    # wrapped iteration-1 prompt; iteration 1 is in flight via
    # ``immediate=True`` so the remaining counter is cap-1. The
    # explicit cap (``2``) flows through to the wrapper's
    # ``total`` attribute and to ``cap_explicit`` on the session.
    armed = await handler.handle("/loop 2 ping")
    assert armed.action == "run_prompt"
    assert "ping" in armed.content
    assert '<loop-iteration index="1" total="2">' in armed.content
    assert sess.loop_iteration_index == 1
    assert sess.loop_iterations_remaining == 1
    assert sess.loop_cap_explicit is True

    # 2. The continuation RPC fires iteration 2 (1 was already
    # booked by ``immediate=True``). After this, remaining=0.
    d2 = await backend.pop_pending_loop_iteration()
    assert "ping" in d2["prompt"]
    assert '<loop-iteration index="2" total="2">' in d2["prompt"]
    assert d2["iteration"] == 2
    assert d2["remaining"] == 0

    # 3. Next RPC tick — cap exhausted, returns the completion
    # marker (so the FE renders the summary) and clears state.
    completion = await backend.pop_pending_loop_iteration()
    assert completion == {"completed": True, "total_iterations": 2}
    assert sess.pending_loop_prompt is None

    # 4. A subsequent tick returns None — completion is one-shot.
    assert await backend.pop_pending_loop_iteration() is None

    # 5. After cap, ``/loop`` status reports no active loop again so
    # a fresh ``/loop`` can be started without "already active" error.
    # No-args ``/loop`` now opens the panel (action="loop"); status
    # text falls under the "no loop" markdown only on the in-chat
    # status helper, which we exercise via the panel-action contract
    # instead.
    panel_action = await handler.handle("/loop")
    assert panel_action.action == "loop"
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
    # Slash command already booked iteration 1 (immediate=True).
    assert sess.loop_iteration_index == 1
    # The next continuation pop produces iteration 2.
    desc = await backend.pop_pending_loop_iteration()
    assert desc is not None and desc["iteration"] == 2

    # User types something else → FE invokes cancel.
    assert await backend.cancel_pending_loop() is True

    # No more iterations.
    assert await backend.pop_pending_loop_iteration() is None
    assert sess.pending_loop_prompt is None


# ── Resume + paused-loop semantics ─────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_skips_paused_loop():
    """The cancel-guard's whole point: a paused loop (state loaded
    from disk on startup) must survive any non-/loop user input,
    or the user can't continue the loop they just restarted."""
    sess = _fake_session()
    sess.pending_loop_prompt = "interrupted work"
    sess.loop_iteration_index = 4
    sess.loop_iterations_remaining = 6
    sess.loop_paused = True
    backend = _FakeBackend(sess)

    # ``cancel_pending_loop`` is what ``process_message`` calls on
    # every non-/loop input. For a paused loop it must be a no-op.
    cancelled = await backend.cancel_pending_loop()
    assert cancelled is False
    assert sess.pending_loop_prompt == "interrupted work"
    assert sess.loop_paused is True


@pytest.mark.asyncio
async def test_resume_returns_wrapped_prompt_and_unpauses():
    """``/loop resume`` (and the panel ``R`` key, and the agent's
    ``loop_resume`` tool) all funnel through ``Session.resume_loop``.
    The returned prompt must be wrapped so the resumed iteration
    carries the same autonomous-loop instructions every other
    iteration does."""
    sess = _fake_session()
    sess.pending_loop_prompt = "verify each section"
    sess.loop_iteration_index = 4
    sess.loop_iterations_remaining = 26
    sess.loop_paused = True
    sess.loop_cap_explicit = True  # Explicit cap → wrapper has total.

    prompt = await sess.resume_loop()
    assert prompt is not None
    assert "verify each section" in prompt
    assert '<loop-iteration index="4" total="30">' in prompt
    assert sess.loop_paused is False


@pytest.mark.asyncio
async def test_resume_implicit_cap_omits_total_in_wrapper() -> None:
    """Resumed iteration on an implicit-cap loop must NOT include
    the ``total`` attribute — the cap is a safety net, not a
    target the model should pace itself against."""
    sess = _fake_session()
    sess.pending_loop_prompt = "scan each file"
    sess.loop_iteration_index = 2
    sess.loop_iterations_remaining = 28
    sess.loop_paused = True
    sess.loop_cap_explicit = False

    prompt = await sess.resume_loop()
    assert prompt is not None
    assert '<loop-iteration index="2">' in prompt
    assert "total=" not in prompt


@pytest.mark.asyncio
async def test_resume_returns_none_when_not_paused():
    """A running loop has nothing to resume — the caller surfaces
    "loop already running" instead."""
    sess = _fake_session()
    sess.pending_loop_prompt = "running"
    sess.loop_iteration_index = 2
    sess.loop_iterations_remaining = 8
    sess.loop_paused = False

    assert await sess.resume_loop() is None


@pytest.mark.asyncio
async def test_resume_returns_none_when_no_loop():
    sess = _fake_session()
    assert await sess.resume_loop() is None


# ── loop_set_total tool ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_loop_set_total_writes_to_progress_store() -> None:
    """The tool's whole purpose: stash the announced total under
    the reserved progress key so ``loop_status`` can surface it."""
    sess = _fake_session()
    sess.pending_loop_prompt = "process the files"
    sess.loop_run_id = "run-xyz"
    tools = LoopTools(sess)

    msg = await tools.loop_set_total(12)

    assert "12" in msg
    stored = await sess.loop_progress_store.get("run-xyz", LoopTools._ANNOUNCED_TOTAL_KEY)
    assert stored == "12"


@pytest.mark.asyncio
async def test_loop_set_total_rejects_no_loop() -> None:
    """Without an active loop there's no ``run_id`` to scope the
    write to — the call has no meaningful target. Surface an
    error rather than silently no-op'ing."""
    tools = LoopTools(_fake_session())
    msg = await tools.loop_set_total(5)
    assert msg.startswith("ERROR")
    assert "no loop" in msg.lower()


@pytest.mark.asyncio
async def test_loop_set_total_rejects_zero_or_negative() -> None:
    """Negative / zero totals would render nonsensical
    ``3 / -1`` in the panel — refuse them at the tool boundary."""
    sess = _fake_session()
    sess.pending_loop_prompt = "p"
    sess.loop_run_id = "run-1"
    tools = LoopTools(sess)
    assert (await tools.loop_set_total(0)).startswith("ERROR")
    assert (await tools.loop_set_total(-3)).startswith("ERROR")


@pytest.mark.asyncio
async def test_loop_set_total_is_idempotent_on_repeat() -> None:
    """Calling the tool twice replaces the value — useful when the
    agent's first count was wrong and it recounts on a later
    iteration."""
    sess = _fake_session()
    sess.pending_loop_prompt = "p"
    sess.loop_run_id = "run-1"
    tools = LoopTools(sess)

    await tools.loop_set_total(8)
    await tools.loop_set_total(12)

    stored = await sess.loop_progress_store.get("run-1", LoopTools._ANNOUNCED_TOTAL_KEY)
    assert stored == "12"


# ── Implicit-cap auto-extend ───────────────────────────────────────


@pytest.mark.asyncio
async def test_implicit_cap_auto_extends_at_cap_hit() -> None:
    """``/loop <prompt>`` (no leading number) treats the cap as a
    safety net, not a target. When ``remaining`` hits zero the loop
    auto-extends by another batch instead of terminating, and the
    descriptor carries a one-shot ``auto_extended`` flag so the FE
    can surface a chat banner."""
    sess = _fake_session()
    sess.pending_loop_prompt = "p"
    sess.loop_iteration_index = 3
    sess.loop_iterations_remaining = 0  # cap exhausted
    sess.loop_cap_explicit = False
    backend = _FakeBackend(sess)

    desc = await backend.pop_pending_loop_iteration()

    assert desc is not None
    assert desc.get("completed") is None or desc.get("completed") is False
    assert desc["auto_extended"] is True
    # The loop didn't terminate — iteration 4 is firing.
    assert desc["iteration"] == 4
    assert sess.pending_loop_prompt == "p"


@pytest.mark.asyncio
async def test_explicit_cap_terminates_at_cap_hit() -> None:
    """Mirror of the auto-extend test for the *explicit* path.
    ``/loop N <prompt>`` honours N as the intended total and
    terminates rather than extending."""
    sess = _fake_session()
    sess.pending_loop_prompt = "p"
    sess.loop_iteration_index = 3
    sess.loop_iterations_remaining = 0
    sess.loop_cap_explicit = True
    backend = _FakeBackend(sess)

    desc = await backend.pop_pending_loop_iteration()

    assert desc == {"completed": True, "total_iterations": 3}
    assert sess.pending_loop_prompt is None


@pytest.mark.asyncio
async def test_implicit_cap_pauses_at_hard_cap() -> None:
    """The hard ceiling now *pauses* (not terminates) for implicit
    loops. The user gets to decide via ``/loop resume`` whether
    legitimate long-running work should continue past the safety
    net. The counter doesn't move so the resumed iteration is the
    same one that was about to fire."""
    sess = _fake_session()
    sess.pending_loop_prompt = "p"
    sess.loop_iteration_index = LOOP_HARD_CAP
    sess.loop_iterations_remaining = 0
    sess.loop_cap_explicit = False
    backend = _FakeBackend(sess)

    desc = await backend.pop_pending_loop_iteration()

    assert desc == {
        "safety_cap_paused": True,
        "iteration": LOOP_HARD_CAP,
    }
    assert sess.loop_paused is True
    # Loop state survives — prompt + counter unchanged so resume
    # picks up exactly where the cap stopped it.
    assert sess.pending_loop_prompt == "p"
    assert sess.loop_iteration_index == LOOP_HARD_CAP


@pytest.mark.asyncio
async def test_paused_loop_short_circuits_advance() -> None:
    """A paused loop must NOT auto-advance on the next
    ``_check_loop_continuation`` tick — otherwise the cap-reached
    pause (and the on-error pause) would be defeated by the very
    next FE poll firing the next iteration."""
    sess = _fake_session()
    sess.pending_loop_prompt = "p"
    sess.loop_iteration_index = 5
    sess.loop_iterations_remaining = 25
    sess.loop_paused = True
    backend = _FakeBackend(sess)

    desc = await backend.pop_pending_loop_iteration()
    assert desc is None
    # Counters untouched.
    assert sess.loop_iteration_index == 5
    assert sess.loop_iterations_remaining == 25
    assert sess.loop_paused is True


@pytest.mark.asyncio
async def test_pause_loop_does_not_advance_counter() -> None:
    """``pause_loop`` is the canonical pause helper for both the
    cap-reached path and the on-error path. The counter must NOT
    move — resume re-fires the same iteration."""
    sess = _fake_session()
    sess.pending_loop_prompt = "p"
    sess.loop_iteration_index = 7
    sess.loop_iterations_remaining = 3

    ok = await sess.pause_loop()

    assert ok is True
    assert sess.loop_paused is True
    # Counter untouched — resume retries iteration 7.
    assert sess.loop_iteration_index == 7
    assert sess.loop_iterations_remaining == 3


@pytest.mark.asyncio
async def test_implicit_cap_descriptor_omits_total_attr() -> None:
    """The wrapper should NOT include ``total="N"`` when the cap is
    implicit — the model would otherwise treat N as a target and
    pace itself against a number that's just a safety net."""
    sess = _fake_session()
    sess.pending_loop_prompt = "scan it"
    sess.loop_iteration_index = 0
    sess.loop_iterations_remaining = LOOP_DEFAULT_MAX_ITERATIONS
    sess.loop_cap_explicit = False
    backend = _FakeBackend(sess)

    desc = await backend.pop_pending_loop_iteration()
    assert desc is not None
    assert '<loop-iteration index="1">' in desc["prompt"]
    assert "total=" not in desc["prompt"]
    assert desc["cap_explicit"] is False


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
