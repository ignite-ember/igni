"""Four backend defects that only show up once you look past the happy path.

* A run that *fails* was recorded as completed. ``RunController._run_locked``
  has an ``except Exception`` branch whose whole job is to stamp the pending
  row interrupted so the FE can offer Retry/Discard — and nothing ever reached
  it, because ``HITLStreamMultiplexer.stream`` catches every stream exception
  and *yields* an ``Error`` message instead of raising, and an Agno
  ``RunErrorEvent`` arrives as a serialized ``RunError`` on the ordinary event
  path. Both end the run as a failure; both fell through to ``mark_completed``.

* Cancel reached into a process-wide foreground slot. One BE process hosts N
  sessions, ``supervisors.default()`` is shared by all of them, so Escape in
  an idle window killed whatever another window was running in the foreground.

* Background-process completions are narrated into a conversation. The bus is
  process-wide and only the boot bridge subscribes, so a build finishing in
  one window spoke into the boot session — possibly a different repository.

* ``asyncRewake`` hook reminders were drained only by the CLI's
  ``SessionMessageHandler``. On the backend run path the queue filled up and
  nobody ever read it, so a model-visible channel was silently absent on the
  path the portal actually uses.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ember_code.backend import run_controller as run_controller_mod
from ember_code.backend.prompt_builder import PromptBuilder
from ember_code.backend.push_bridge import PushNotificationBridge
from ember_code.backend.run_controller import RunController
from ember_code.backend.schemas_run import HookGateResult, PromptBuildResult, RunPhase
from ember_code.core.session.reminders import PendingReminderQueue
from ember_code.protocol import messages as msg

# ── Fakes ────────────────────────────────────────────────────────────


class _Journal:
    """Stands in for ``PendingMessageJournal`` and remembers the verdict."""

    def __init__(self) -> None:
        self.completed: list[str] = []
        self.interrupted: list[tuple[str, str, str | None]] = []

    async def drain_queued_drops(self) -> None:
        return None

    async def record(self, text: str) -> str:
        return "pending-1"

    async def mark_completed(self, pending_id: str) -> None:
        self.completed.append(pending_id)

    async def mark_interrupted(
        self, pending_id: str, reason: str, last_error: str | None = None
    ) -> None:
        self.interrupted.append((pending_id, reason, last_error))


class _HookGate:
    async def fire_user_prompt_submit(self, text: str) -> HookGateResult:
        return HookGateResult(should_continue=True)

    async def fire_stop(self) -> HookGateResult:
        return HookGateResult(should_continue=True)


class _Dispatcher:
    async def handle(self, proto, team) -> None:
        return None


class _PromptBuilder:
    async def build(self, text, media, *, interrupted_summary):
        return PromptBuildResult(message=text, media=None, info_messages=[])


def _controller(stream_messages: list[msg.Message]) -> tuple[RunController, _Journal]:
    """A ``RunController`` wired to fakes, streaming ``stream_messages``.

    Built via ``__new__`` — the collaborators ``_run_locked`` needs are set
    directly, which is the same bypass the crash-survival tests use. Building
    a real one would drag in a Session, an Agno team and a database.
    """

    async def _stream(_team_stream):
        for message in stream_messages:
            yield message

    journal = _Journal()
    controller = RunController.__new__(RunController)
    controller._phase = RunPhase.idle
    controller._interrupted_summary = None
    controller._pending_id_for_cancel = None
    controller._current_run_task = None
    controller._pending_journal = journal
    controller._hook_gate = _HookGate()
    controller._dispatcher = _Dispatcher()
    controller._prompt_builder = _PromptBuilder()
    controller._session = SimpleNamespace(
        main_team=SimpleNamespace(arun=lambda *a, **k: None),
        guardrail_runner=SimpleNamespace(warning_prefix=_no_prefix),
    )
    controller._backend = SimpleNamespace(
        _stream_with_subagent_hitl=_stream,
        _periodic_checkpoint=_forever,
    )
    controller._http_client_manager = SimpleNamespace(close_and_replace=_noop_async)
    return controller, journal


async def _no_prefix(text: str) -> str:
    return ""


async def _forever(team, *args, **kwargs) -> None:
    await asyncio.sleep(3600)


async def _noop_async(team) -> None:
    return None


async def _drain(controller: RunController) -> list[msg.Message]:
    return [proto async for proto in controller._run_locked("hello", None)]


# ── A failed run must not be recorded as a completed one ─────────────


class TestTheRunRecord:
    async def test_a_clean_stream_is_completed(self):
        controller, journal = _controller([msg.StreamingDone(run_id="r1")])

        await _drain(controller)

        assert journal.completed == ["pending-1"]
        assert journal.interrupted == []

    async def test_a_yielded_error_is_recorded_as_interrupted(self):
        """The mux's ``except`` branch yields this instead of raising."""
        controller, journal = _controller([msg.Error(text="model connection reset")])

        streamed = await _drain(controller)

        assert journal.completed == [], (
            "a failed run was stamped completed — the FE has no dangling row "
            "left to offer Retry/Discard on"
        )
        assert journal.interrupted == [("pending-1", "errored", "model connection reset")]
        assert controller._phase is RunPhase.errored
        # The FE still sees the error message; only the bookkeeping changed.
        assert any(isinstance(m, msg.Error) for m in streamed)

    async def test_an_agno_run_error_is_recorded_as_interrupted(self):
        """``RunErrorEvent`` is serialized onto the ordinary event path."""
        controller, journal = _controller([msg.RunError(error="tool loop exceeded")])

        await _drain(controller)

        assert journal.completed == []
        assert journal.interrupted == [("pending-1", "errored", "tool loop exceeded")]

    async def test_a_failed_run_does_not_clear_the_cancel_handle_late(self):
        controller, _ = _controller([msg.Error(text="boom")])

        await _drain(controller)

        assert controller._pending_id_for_cancel is None


# ── Cancel must not reach into another session's foreground shell ────


class _FakeSupervisor:
    def __init__(self) -> None:
        self.cancelled = 0

    def cancel_foreground(self) -> bool:
        self.cancelled += 1
        return True


class _FakeRegistry:
    def __init__(self, supervisor: _FakeSupervisor) -> None:
        self._supervisor = supervisor

    def default(self) -> _FakeSupervisor:
        return self._supervisor


@pytest.fixture
def fake_supervisor(monkeypatch) -> _FakeSupervisor:
    supervisor = _FakeSupervisor()
    monkeypatch.setattr(run_controller_mod, "supervisors", _FakeRegistry(supervisor))
    return supervisor


def _cancellable(phase: RunPhase) -> RunController:
    controller = RunController.__new__(RunController)
    controller._phase = phase
    controller._pending_id_for_cancel = None
    controller._current_run_task = None
    controller._session = SimpleNamespace(main_team=SimpleNamespace(run_id=None))
    return controller


class TestCancelAndTheSharedForegroundSlot:
    def test_an_idle_session_leaves_the_slot_alone(self, fake_supervisor):
        """Escape in a window with no run must not kill another window's build.

        ``supervisors.default()`` is one object for the whole BE process, and
        a foreground shell only exists while the run that spawned it is
        blocked on it — so a session that is not streaming cannot be its
        owner. Killing it anyway is how an ``alembic upgrade`` in another
        repository died mid-migration with no indication why.
        """
        _cancellable(RunPhase.idle).cancel_run()

        assert fake_supervisor.cancelled == 0

    def test_a_finished_session_leaves_the_slot_alone(self, fake_supervisor):
        _cancellable(RunPhase.done).cancel_run()

        assert fake_supervisor.cancelled == 0

    def test_a_streaming_session_still_kills_its_own_shell(self, fake_supervisor):
        _cancellable(RunPhase.streaming).cancel_run()

        assert fake_supervisor.cancelled == 1


# ── Background-process completions are conversation content ──────────


class _NullTransport:
    async def send(self, message) -> None:
        return None


def _bridge(queue: list[str], live_sessions: int) -> PushNotificationBridge:
    bridge = PushNotificationBridge(transport=_NullTransport(), loop=None, queue=queue)
    bridge.set_live_session_count(lambda: live_sessions)
    return bridge


_EXIT_EVENT = {
    "pid": 8080,
    "cmd": "npm run build",
    "exit_code": 0,
    "duration_seconds": 12.0,
    "output_tail": "done",
}


class TestBackgroundCompletionInjection:
    def test_a_single_session_still_hears_about_its_own_process(self):
        queue: list[str] = []

        _bridge(queue, live_sessions=1)._on_process_done(dict(_EXIT_EVENT))

        assert len(queue) == 1
        assert "npm run build" in queue[0]

    def test_a_pooled_backend_does_not_narrate_into_the_boot_session(self):
        """With a pool live there is no owner on the event to check against.

        The bus is process-wide and only the boot bridge subscribes, so every
        window's build completion was injected as a synthetic user message in
        the boot session's conversation — a different window, potentially a
        different repository.
        """
        queue: list[str] = []

        _bridge(queue, live_sessions=2)._on_process_done(dict(_EXIT_EVENT))

        assert queue == [], (
            "a background process finished somewhere in this BE and its output "
            "was spoken into the boot session's conversation"
        )

    def test_a_pooled_runtime_gets_its_own_queue(self):
        """``for_transport`` used to hand the parent's queue to every runtime.

        A queue is a conversation: whatever lands in it is injected as a
        synthetic user message on that session's next turn.
        """
        boot_queue: list[str] = []
        runtime_queue: list[str] = []
        boot = PushNotificationBridge(transport=_NullTransport(), loop=None, queue=boot_queue)

        sub = boot.for_transport(_NullTransport(), queue=runtime_queue)
        sub._on_process_done(dict(_EXIT_EVENT))

        assert boot_queue == []
        assert len(runtime_queue) == 1


# ── asyncRewake reminders reach the model on the backend path ────────


class _ReminderSession:
    """The slice of ``Session`` :class:`PromptBuilder` touches."""

    def __init__(self, project_dir) -> None:
        self.project_dir = project_dir
        self.settings = SimpleNamespace(models=SimpleNamespace(registry={}, default="m"))
        self.reminders = PendingReminderQueue()

    async def inject_learnings(self) -> None:
        return None

    def _drain_pending_reminders(self) -> list[str]:
        return self.reminders.drain()


class TestAsyncRewakeRemindersOnTheBackendPath:
    async def test_a_queued_reminder_reaches_the_prompt(self, tmp_path):
        session = _ReminderSession(tmp_path)
        session.reminders.queue("the deploy finished")
        builder = PromptBuilder(session)

        built = await builder.build("what is up", None, interrupted_summary=None)

        assert "<system-reminder>the deploy finished</system-reminder>" in built.message, (
            "an asyncRewake hook fired and the model never saw it — this is the "
            "only path the portal uses"
        )

    async def test_it_is_one_shot(self, tmp_path):
        session = _ReminderSession(tmp_path)
        session.reminders.queue("once")
        builder = PromptBuilder(session)

        first = await builder.build("a", None, interrupted_summary=None)
        second = await builder.build("b", None, interrupted_summary=None)

        assert "once" in first.message
        assert "system-reminder" not in second.message

    async def test_an_empty_queue_adds_nothing(self, tmp_path):
        builder = PromptBuilder(_ReminderSession(tmp_path))

        built = await builder.build("a", None, interrupted_summary=None)

        assert "system-reminder" not in built.message

    async def test_the_reminder_block_precedes_the_system_context(self, tmp_path):
        """Same shape and same position as the CLI path builds."""
        session = _ReminderSession(tmp_path)
        session.reminders.queue("note")
        builder = PromptBuilder(session)

        message = (await builder.build("a", None, interrupted_summary=None)).message

        assert message.index("<system-reminder>") < message.index("<system-context>")
