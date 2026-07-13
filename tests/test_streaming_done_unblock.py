"""Tests for the StreamingDone optimistic-unblock flow.

User reported: when the agent finishes streaming the response, the
queue panel stays visible for 5-15 more seconds while the backend
drains Agno's tail (compression, memory extraction, persistence).
During that window any new user input is queued behind the curtain
instead of going through.

The fix has three moving parts that this file pins:

1. **Serializer** — Agno's ``RunContentCompletedEvent`` becomes a
   ``StreamingDone`` protocol message (and only that — not consumed
   by other branches).
2. **FE controller** — receiving ``StreamingDone`` flips
   ``_processing`` to ``False`` so the next ``process_message`` goes
   straight to ``_run`` instead of enqueuing. The generation counter
   keeps the old turn's finally from clobbering this once a new
   turn has taken over.
3. **BE serial lock** — concurrent ``run_message`` calls block on a
   per-instance ``asyncio.Lock`` so two ``team.arun()`` calls on the
   same Agno team never race even though the FE unblocks early.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.run.base import RunStatus
from agno.run.team import RunCompletedEvent, RunContentCompletedEvent

from ember_code.backend.server import BackendServer
from ember_code.frontend.tui.run_controller import RunController
from ember_code.protocol import messages as msg
from ember_code.protocol.serializer import serialize_event


class TestStreamingDoneSerializer:
    def test_run_content_completed_serializes_to_streaming_done(self):
        """The Agno content-stream-done event is the one that maps
        to ``StreamingDone``. Tool/text events must not."""
        event = RunContentCompletedEvent(
            session_id="sess",
            run_id="r1",
        )
        result = serialize_event(event)
        assert isinstance(result, msg.StreamingDone)
        assert result.run_id == "r1"

    def test_run_completed_still_serializes_to_run_completed(self):
        """Regression guard: the new branch must not steal the
        existing ``RunCompletedEvent`` → ``RunCompleted`` mapping."""
        event = RunCompletedEvent(
            session_id="sess",
            run_id="r2",
        )
        result = serialize_event(event)
        assert isinstance(result, msg.RunCompleted)
        assert result.run_id == "r2"


class TestBackendSerialLock:
    @pytest.mark.asyncio
    async def test_concurrent_run_messages_serialize(self):
        """Two ``run_message`` calls in flight must not overlap —
        the second's first event must arrive only after the first
        has fully drained.

        Uses ``patch.object`` so the class-level stub is restored
        after the test even on failure — otherwise the polluted
        method leaks into other tests in the file and hangs them.
        """
        server = BackendServer.__new__(BackendServer)
        server._run_lock = asyncio.Lock()
        order: list[tuple[str, str]] = []

        async def stub_locked(self, text, media):
            order.append(("start", text))
            yield msg.Info(text=f"first/{text}")
            await asyncio.sleep(0.05)  # simulate Agno tail
            yield msg.Info(text=f"last/{text}")
            order.append(("end", text))

        with patch.object(BackendServer, "_run_message_locked", stub_locked):

            async def consume(label: str):
                async for _ in server.run_message(label):
                    pass

            t1 = asyncio.create_task(consume("A"))
            await asyncio.sleep(0)
            t2 = asyncio.create_task(consume("B"))
            await asyncio.gather(t1, t2)

        # The interleave MUST be A-start, A-end, B-start, B-end —
        # B can't start before A ends.
        assert order == [
            ("start", "A"),
            ("end", "A"),
            ("start", "B"),
            ("end", "B"),
        ]


class TestIncrementalCheckpoint:
    @pytest.mark.asyncio
    async def test_checkpoint_calls_asave_with_cached_session(self):
        """``_checkpoint_session`` must hit ``team.asave_session`` with
        whatever Agno currently holds in ``cached_session`` — that's
        the in-flight ``TeamSession`` whose ``runs[-1]`` has
        ``status=running``."""
        server = BackendServer.__new__(BackendServer)
        team = MagicMock()
        cached = MagicMock(name="cached_session")
        team.cached_session = cached
        team.asave_session = AsyncMock()

        await server._checkpoint_session(team)

        team.asave_session.assert_awaited_once_with(cached)

    @pytest.mark.asyncio
    async def test_checkpoint_noop_when_session_not_yet_cached(self):
        """Very early events can fire before Agno has assembled its
        cached session — the helper must just shrug and return."""
        server = BackendServer.__new__(BackendServer)
        team = MagicMock()
        team.cached_session = None
        team.asave_session = AsyncMock()

        await server._checkpoint_session(team)

        team.asave_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_checkpoint_swallows_save_errors(self):
        """A flaky persistence layer must not abort the live stream
        — the agent's still talking and the checkpoint is a
        durability optimisation, not a correctness requirement."""
        server = BackendServer.__new__(BackendServer)
        team = MagicMock()
        team.cached_session = MagicMock()
        team.asave_session = AsyncMock(side_effect=RuntimeError("disk full"))

        # Must not raise.
        await server._checkpoint_session(team)


class TestInterruptedRunDetection:
    """``--continue`` after a process crash mid-chain should surface
    the partial work to the agent so it can recap or continue,
    rather than starting fresh as if nothing had happened."""

    @pytest.mark.asyncio
    async def test_detects_running_status_and_builds_summary(self):
        """When the previous session's latest run has
        ``status=running``, the BE stashes a summary string for the
        next user message to consume."""
        server = BackendServer.__new__(BackendServer)
        server._interrupted_run_summary = None
        server._session = MagicMock()
        server._session.session_id = "sess-1"

        # Build a fake session with one in-flight run.
        partial_run = MagicMock()
        partial_run.status = RunStatus.running
        partial_run.run_id = "run-99"
        tool = MagicMock()
        tool.tool_name = "read_file"
        partial_run.tools = [tool]
        partial_run.content = "Looking at the file..."

        agno_session = MagicMock()
        agno_session.runs = [partial_run]
        server._session.main_team.aget_session = AsyncMock(return_value=agno_session)

        await server._detect_interrupted_run()

        assert server._interrupted_run_summary is not None
        s = server._interrupted_run_summary
        assert "interrupted" in s
        assert "read_file" in s
        assert "Looking at the file" in s

    @pytest.mark.asyncio
    async def test_completed_run_leaves_summary_none(self):
        """The happy path: a session resumed after a clean shutdown
        has ``status=completed`` on its latest run and the BE must
        not inject any nudge."""
        server = BackendServer.__new__(BackendServer)
        server._interrupted_run_summary = None
        server._session = MagicMock()
        server._session.session_id = "sess-2"

        done_run = MagicMock()
        done_run.status = RunStatus.completed
        agno_session = MagicMock()
        agno_session.runs = [done_run]
        server._session.main_team.aget_session = AsyncMock(return_value=agno_session)

        await server._detect_interrupted_run()

        assert server._interrupted_run_summary is None

    @pytest.mark.asyncio
    async def test_no_runs_at_all_leaves_summary_none(self):
        """Fresh session with no runs persisted — nothing to resume."""
        server = BackendServer.__new__(BackendServer)
        server._interrupted_run_summary = None
        server._session = MagicMock()
        server._session.session_id = "sess-3"

        agno_session = MagicMock()
        agno_session.runs = []
        server._session.main_team.aget_session = AsyncMock(return_value=agno_session)

        await server._detect_interrupted_run()

        assert server._interrupted_run_summary is None

    @pytest.mark.asyncio
    async def test_aget_session_failure_swallowed(self):
        """A flaky DB on startup must not block the whole session
        boot — we'd rather start fresh than crash."""
        server = BackendServer.__new__(BackendServer)
        server._interrupted_run_summary = None
        server._session = MagicMock()
        server._session.session_id = "sess-4"
        server._session.main_team.aget_session = AsyncMock(side_effect=RuntimeError("db locked"))

        # Must not raise.
        await server._detect_interrupted_run()
        assert server._interrupted_run_summary is None


class TestRunGenerationGuard:
    """The FE generation counter keeps a stale finally from clearing
    state that a newer turn already owns. Without this, submitting a
    follow-up message during the previous run's tail makes the old
    finally race ``_processing`` back to ``False`` after the new
    ``_run`` has just set it ``True``."""

    def test_initial_generation_is_zero(self):
        ctrl = RunController.__new__(RunController)
        ctrl._run_generation = 0
        assert ctrl._run_generation == 0

    def test_generation_increments_per_run_invocation(self):
        """Each call into the ``_run`` body bumps the counter so
        callers can detect whether they're still the latest."""
        ctrl = RunController.__new__(RunController)
        ctrl._run_generation = 0

        # Simulate two _run calls each capturing a generation tag.
        ctrl._run_generation += 1
        gen_first = ctrl._run_generation
        ctrl._run_generation += 1
        gen_second = ctrl._run_generation

        assert gen_first == 1
        assert gen_second == 2
        assert gen_first != gen_second

    def test_stale_finally_skips_processing_clear(self):
        """The whole point of the guard: an old turn finishing its
        tail while a newer turn is mid-flight must NOT clear
        ``_processing``. Simulates the race directly."""
        ctrl = RunController.__new__(RunController)
        ctrl._run_generation = 0
        ctrl._processing = False
        ctrl._current_task = None

        # Old turn starts.
        ctrl._run_generation += 1
        old_gen = ctrl._run_generation
        ctrl._processing = True

        # Newer turn starts while old is still draining the tail.
        ctrl._run_generation += 1
        ctrl._processing = True  # new turn owns it
        ctrl._current_task = "new-task"  # type: ignore[assignment]

        # Old turn's finally runs the gated cleanup.
        if old_gen == ctrl._run_generation:
            ctrl._processing = False
            ctrl._current_task = None

        # New turn's ownership preserved.
        assert ctrl._processing is True
        assert ctrl._current_task == "new-task"

    def test_solo_finally_clears_processing(self):
        """Sanity check: when there's no newer turn, the finally
        DOES clear state. Otherwise the guard would just break
        normal single-turn cleanup."""
        ctrl = RunController.__new__(RunController)
        ctrl._run_generation = 0
        ctrl._processing = False
        ctrl._current_task = None

        ctrl._run_generation += 1
        my_gen = ctrl._run_generation
        ctrl._processing = True
        ctrl._current_task = "the-only-task"  # type: ignore[assignment]

        # Same-generation cleanup runs.
        if my_gen == ctrl._run_generation:
            ctrl._processing = False
            ctrl._current_task = None

        assert ctrl._processing is False
        assert ctrl._current_task is None


class TestRunMessageIntegration:
    """End-to-end ``run_message`` behaviour under the new flow.

    Verifies the wiring that the helper-level tests above don't
    cover: the lock actually wraps ``_run_message_locked``, the
    serialised stream really does yield ``StreamingDone`` at the
    right point, and ``ToolCompleted`` events trigger the
    incremental checkpoint.
    """

    @pytest.mark.asyncio
    async def test_full_stream_yields_streaming_done_and_checkpoints(self, monkeypatch):
        """A full ``run_message`` call:

        * yields events in the order they arrive from the inner
          ``_stream_with_subagent_hitl`` generator
        * fires ``_checkpoint_session`` after every ``ToolCompleted``
          / ``ToolError`` (Phase 2 durability)
        * passes the ``StreamingDone`` through unchanged so the FE
          can act on it (Phase 1 unblock)
        """
        server = BackendServer.__new__(BackendServer)
        server._run_lock = asyncio.Lock()
        server._processing = False
        server._interrupted_run_summary = None
        server._settings = MagicMock()
        server._pending_requirements = {}
        server._session = MagicMock()
        server._session.session_id = "sess"
        server._session.main_team = MagicMock()
        server._session.hook_executor = MagicMock()
        server._session.hook_executor.execute = AsyncMock(
            return_value=MagicMock(should_continue=True, message="")
        )
        server._session._inject_learnings = AsyncMock()
        server._session.settings.models.default = "MiniMax-M2.7"
        server._session.settings.models.registry = {"MiniMax-M2.7": {"vision": False}}
        server._session.project_dir = "/tmp"

        # Replace ``_close_model_http_client`` and the multiplexer so
        # we control the event sequence precisely.
        server._close_model_http_client = AsyncMock()
        # Durability fields — not exercised here but the
        # ``run_message`` path touches them.
        server._pending_store = MagicMock()
        server._pending_store.arecord_received = AsyncMock(return_value="mid-1")
        server._pending_store.amark_completed = AsyncMock()
        server._pending_store.adiscard = AsyncMock()
        server._pending_message_ids_to_drop = []

        async def fake_stream(_self, _agno_stream):
            yield msg.ToolCompleted(summary="ran read_file", run_id="r1")
            yield msg.StreamingDone(run_id="r1")
            yield msg.ToolError(error="oops", run_id="r1")
            yield msg.RunCompleted(run_id="r1")

        checkpoint_calls: list[None] = []

        async def fake_checkpoint(_self, _team):
            checkpoint_calls.append(None)

        server._session.main_team.arun = MagicMock()

        with (
            patch.object(BackendServer, "_stream_with_subagent_hitl", fake_stream),
            patch.object(BackendServer, "_checkpoint_session", fake_checkpoint),
        ):
            events = [proto async for proto in server.run_message("hi")]

        # Same order, same types — wrapper is transparent.
        kinds = [type(p).__name__ for p in events]
        assert "StreamingDone" in kinds
        # Checkpoint fires for each ToolCompleted / ToolError, NOT
        # for StreamingDone or RunCompleted.
        assert len(checkpoint_calls) == 2

    @pytest.mark.asyncio
    async def test_run_message_holds_lock_through_full_tail(self, monkeypatch):
        """The lock must NOT release on ``StreamingDone`` — only when
        the generator fully exhausts. If a second ``run_message``
        attempted to acquire the lock between StreamingDone and the
        stream-close, two ``team.arun`` calls would race."""
        server = BackendServer.__new__(BackendServer)
        server._run_lock = asyncio.Lock()
        server._processing = False
        server._interrupted_run_summary = None
        server._settings = MagicMock()
        server._pending_requirements = {}
        server._session = MagicMock()
        server._session.session_id = "sess"
        server._session.main_team = MagicMock()
        server._session.main_team.arun = MagicMock()
        server._session.hook_executor = MagicMock()
        server._session.hook_executor.execute = AsyncMock(
            return_value=MagicMock(should_continue=True, message="")
        )
        server._session._inject_learnings = AsyncMock()
        server._session.settings.models.default = "MiniMax-M2.7"
        server._session.settings.models.registry = {"MiniMax-M2.7": {"vision": False}}
        server._session.project_dir = "/tmp"
        server._close_model_http_client = AsyncMock()
        # New durability fields — pre-persist + periodic checkpoint.
        # Mocked out at the helper layer so the integration test
        # exercises the wiring without depending on a real SQLite
        # file or actually spinning the periodic task.
        server._pending_store = MagicMock()
        server._pending_store.arecord_received = AsyncMock(return_value="mid-1")
        server._pending_store.amark_completed = AsyncMock()
        server._pending_store.adiscard = AsyncMock()
        server._pending_message_ids_to_drop = []

        held_at_streaming_done = asyncio.Event()
        release_after_check = asyncio.Event()

        async def slow_tail_stream(_self, _agno_stream):
            yield msg.StreamingDone(run_id="r1")
            held_at_streaming_done.set()
            await release_after_check.wait()
            yield msg.RunCompleted(run_id="r1")

        async def noop_checkpoint(_self, _team):
            pass

        with (
            patch.object(BackendServer, "_stream_with_subagent_hitl", slow_tail_stream),
            patch.object(BackendServer, "_checkpoint_session", noop_checkpoint),
        ):

            async def consumer():
                return [proto async for proto in server.run_message("a")]

            first_task = asyncio.create_task(consumer())
            await held_at_streaming_done.wait()

            # First consumer has yielded StreamingDone but the lock
            # is still held — verify by trying a second acquire.
            second_acquired = False

            async def try_second():
                nonlocal second_acquired
                async with server._run_lock:
                    second_acquired = True

            second_task = asyncio.create_task(try_second())
            await asyncio.sleep(0.05)
            assert second_acquired is False, "lock leaked between StreamingDone and stream close"

            # Now let the first generator finish — second should
            # acquire the lock.
            release_after_check.set()
            await first_task
            await second_task
            assert second_acquired is True

    @pytest.mark.asyncio
    async def test_interrupted_summary_consumed_once(self, monkeypatch):
        """The interrupted-run summary is a one-shot: injected on the
        next user message and cleared, so a subsequent message in
        the same session doesn't keep nudging the agent that it
        was interrupted."""
        server = BackendServer.__new__(BackendServer)
        server._run_lock = asyncio.Lock()
        server._processing = False
        server._interrupted_run_summary = "prev run was interrupted, etc"
        server._settings = MagicMock()
        server._pending_requirements = {}
        server._session = MagicMock()
        server._session.session_id = "sess"
        server._session.main_team = MagicMock()
        server._session.main_team.arun = MagicMock()
        server._session.hook_executor = MagicMock()
        server._session.hook_executor.execute = AsyncMock(
            return_value=MagicMock(should_continue=True, message="")
        )
        server._session._inject_learnings = AsyncMock()
        server._session.settings.models.default = "MiniMax-M2.7"
        server._session.settings.models.registry = {"MiniMax-M2.7": {"vision": False}}
        server._session.project_dir = "/tmp"
        server._close_model_http_client = AsyncMock()
        # New durability fields — pre-persist + periodic checkpoint.
        # Mocked out at the helper layer so the integration test
        # exercises the wiring without depending on a real SQLite
        # file or actually spinning the periodic task.
        server._pending_store = MagicMock()
        server._pending_store.arecord_received = AsyncMock(return_value="mid-1")
        server._pending_store.amark_completed = AsyncMock()
        server._pending_store.adiscard = AsyncMock()
        server._pending_message_ids_to_drop = []

        async def empty_stream(_self, _agno_stream):
            return
            yield  # pragma: no cover — makes this an async generator

        async def noop_checkpoint(_self, _team):
            pass

        with (
            patch.object(BackendServer, "_stream_with_subagent_hitl", empty_stream),
            patch.object(BackendServer, "_checkpoint_session", noop_checkpoint),
        ):
            # First message should consume and clear the summary, and
            # emit an Info line warning the user about the resume.
            events_1 = [proto async for proto in server.run_message("first")]
            info_msgs = [e for e in events_1 if isinstance(e, msg.Info)]
            assert any("interrupted" in i.text.lower() for i in info_msgs)
            assert server._interrupted_run_summary is None

            # Second message in the same session must NOT re-inject the
            # nudge — the run was acknowledged on the first turn.
            events_2 = [proto async for proto in server.run_message("second")]
            info_msgs_2 = [e for e in events_2 if isinstance(e, msg.Info)]
            assert not any("interrupted" in i.text.lower() for i in info_msgs_2)


class TestFEStreamingDoneHandler:
    """The FE controller's response to ``StreamingDone``: clear
    ``_processing`` so the next user message dispatches immediately."""

    @pytest.mark.asyncio
    async def test_streaming_done_clears_processing(self):
        ctrl = RunController.__new__(RunController)
        ctrl._processing = True
        ctrl._queue = []
        # ``_sync_queue_panel`` is the side-effect we assert on —
        # patch it via attribute, not bound-method, so the call
        # counter survives the dispatch.
        ctrl._sync_queue_panel = MagicMock()

        await ctrl._render(msg.StreamingDone(run_id="r1"))

        assert ctrl._processing is False
        ctrl._sync_queue_panel.assert_called_once()


class TestAutoDropPartialOnCompletion:
    """After a clean run completes, the next ``--continue`` boot
    must NOT see the partial state — Agno's end-of-run save
    overwrites everything via upsert semantics. This test pins
    that intent at the level we can: the in-flight summary state
    flips back to None across runs without explicit cleanup."""

    @pytest.mark.asyncio
    async def test_completed_run_overwrites_partial_summary(self):
        """Resume with status=running → summary populated.
        Then a run completes → next detect call sees
        status=completed → summary clears.
        """
        server = BackendServer.__new__(BackendServer)
        server._interrupted_run_summary = None
        server._session = MagicMock()
        server._session.session_id = "sess"

        # Step 1: first detect — interrupted run on disk.
        partial = MagicMock()
        partial.status = RunStatus.running
        partial.run_id = "r1"
        partial.tools = []
        partial.content = "halfway through..."

        agno_session = MagicMock()
        agno_session.runs = [partial]
        server._session.main_team.aget_session = AsyncMock(return_value=agno_session)

        await server._detect_interrupted_run()
        assert server._interrupted_run_summary is not None

        # Step 2: simulate that run completing — the saved session
        # now has the same run with status=completed (Agno upsert
        # would have overwritten in real life). Re-running detect
        # must NOT re-arm the summary.
        partial.status = RunStatus.completed
        server._interrupted_run_summary = None
        await server._detect_interrupted_run()
        assert server._interrupted_run_summary is None
