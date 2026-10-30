"""After a restore igni answers first and tidies up after.

``docs/CLOUD_RESUME_TODO.md`` item 1. On a restored VM every page touched
before the first answer is one the answer waits for. So only what that
answer needs runs at once — the reseed, the pools swapped — and the rest
(the notification stream, the timers, the activity file, the credential)
waits for igni's first answer and the burst of requests a window sends
with it, or a second if nobody asks. Each restore's steps are timed and
logged as one ``resume timeline`` line. Meanwhile the collector does not
run and the log writer is not woken; and a restore wakes the restore
watch alone, not every watch of the run directory.
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import struct
import time
from pathlib import Path
from typing import Any

import pytest
from igni_cloud.guest.contract import CloudGuest
from igni_cloud.guest.files import AtomicFile
from igni_cloud.guest.paths import GuestPaths
from igni_cloud.guest.practice import FirstAnswerPractice
from igni_cloud.guest.resume import RestoreStep, RestoreWatch
from igni_cloud.guest.timeline import ResumeTimeline
from igni_cloud.guest.watch import DirectoryWatch
from igni_cloud.layer import CloudLayer
from igni_cloud.logs import StdoutLog

from ember_code.backend.host_backend import ObservedTransport
from ember_code.protocol import messages as msg
from ember_code.transport.websocket import WebSocketServerTransport


class GuestAgent:
    """Writes ``restored`` as ``post_restore`` does."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.generation = 0

    def restore(self) -> None:
        self.generation += 1
        AtomicFile(self.path).write(
            json.dumps({"generation": self.generation, "at_ms": 1, "clock_step_ms": 80.0}).encode()
        )


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(RestoreWatch, "LATER_SECONDS", 0.5)
    monkeypatch.setattr(RestoreWatch, "AFTER_ANSWER_SECONDS", 0.01)


def _steps(watch: RestoreWatch, ran: list[str]) -> None:
    def step(name: str) -> Any:
        async def run(_restored: Any) -> None:
            ran.append(name)

        run.__name__ = name
        return run

    watch.on_restore(step("pools"))
    watch.on_restore(step("reseed"), when=RestoreStep.AT_FIRST_ANSWER)
    watch.on_restore(step("notifications"), when=RestoreStep.LATER)
    watch.on_restore(step("timers"), when=RestoreStep.LATER)


async def test_what_can_wait_waits_for_the_first_answer(tmp_path: Path, quick: None) -> None:
    agent = GuestAgent(tmp_path / "restored")
    watch = RestoreWatch(agent.path, watch=False)
    ran: list[str] = []
    _steps(watch, ran)
    watch.start()
    try:
        agent.restore()
        checking = asyncio.create_task(watch.check())
        await asyncio.sleep(0.1)
        assert ran == ["pools"], "only marking the pools stale before an answer"
        assert watch.waiting_for_answer
        watch.answered()
        assert await asyncio.wait_for(checking, 1)
        assert ran == ["pools", "reseed", "notifications", "timers"]
        assert not watch.waiting_for_answer
    finally:
        await watch.stop()


async def test_with_nobody_asking_it_waits_a_moment_only(tmp_path: Path, quick: None) -> None:
    agent = GuestAgent(tmp_path / "restored")
    watch = RestoreWatch(agent.path, watch=False)
    ran: list[str] = []
    _steps(watch, ran)
    watch.start()
    try:
        agent.restore()
        assert await asyncio.wait_for(watch.check(), 2)
        assert ran == ["pools", "reseed", "notifications", "timers"]
    finally:
        await watch.stop()


async def test_each_restore_logs_its_timeline(
    tmp_path: Path, quick: None, caplog: pytest.LogCaptureFixture
) -> None:
    agent = GuestAgent(tmp_path / "restored")
    watch = RestoreWatch(agent.path, watch=False)
    _steps(watch, [])
    watch.start()
    try:
        agent.restore()
        with caplog.at_level(logging.INFO, logger="igni_cloud.guest.timeline"):
            checking = asyncio.create_task(watch.check())
            await asyncio.sleep(0.05)
            watch.answered()
            await asyncio.wait_for(checking, 1)
    finally:
        await watch.stop()
    records = [r for r in caplog.records if r.getMessage() == "resume timeline"]
    assert len(records) == 1
    timeline = records[0].fields["timeline"]  # type: ignore[attr-defined]
    assert timeline["generation"] == 1 and timeline["answered"]
    steps = [s["step"] for s in timeline["steps"]]
    assert steps[:3] == ["pools", "first answer", "reseed"]
    assert steps[-2:] == ["notifications", "timers"]
    assert all(s["minor_faults"] >= 0 and s["took_ms"] >= 0 for s in timeline["steps"])


async def test_a_restore_nobody_answers_is_logged_by_its_deadline(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ResumeTimeline, "DEADLINE_SECONDS", 0.05)
    with caplog.at_level(logging.INFO, logger="igni_cloud.guest.timeline"):
        timeline = ResumeTimeline(4)
        timeline.mark("reseed")
        await asyncio.sleep(0.1)
    assert timeline.done and not timeline.record.answered
    assert [r.getMessage() for r in caplog.records].count("resume timeline") == 1


def test_an_answer_lets_the_waiting_steps_run(tmp_path: Path) -> None:
    guest = CloudGuest(
        GuestPaths.from_env({GuestPaths.RUN_DIR_ENV: str(tmp_path)}),
        restores=RestoreWatch(tmp_path / "restored", watch=False),
        gc_freeze=False,
    )
    guest._sent(msg.PushNotification(channel="x", payload={}))
    assert not guest.restores._answered.is_set(), "only an answer to a request"
    guest._sent(msg.RPCResponse(id="r1", result={}))
    assert guest.restores._answered.is_set()


def test_a_window_attaching_during_a_resume_leaves_the_file_for_later(tmp_path: Path) -> None:
    guest = CloudGuest(
        GuestPaths.from_env({GuestPaths.RUN_DIR_ENV: str(tmp_path)}),
        restores=RestoreWatch(tmp_path / "restored", watch=False),
        gc_freeze=False,
    )
    rewrites: list[bool] = []
    guest._rewrite_now = lambda: rewrites.append(True)  # type: ignore[method-assign]
    guest.restores._waiting = True
    guest._connection_moved("attached", "ws-1")
    assert rewrites == [], "rewritten with the restore's later steps"
    guest.restores._waiting = False
    guest._connection_moved("detached", "ws-1")
    assert rewrites == [True]


def test_the_layers_own_logs_reach_standard_output() -> None:
    StdoutLog.reset()
    try:
        handler = CloudLayer().log_handler()
        assert handler in logging.getLogger("igni_cloud").handlers
        assert logging.getLogger("igni_cloud").getEffectiveLevel() <= logging.INFO
    finally:
        logging.getLogger("igni_cloud").removeHandler(handler)
        StdoutLog.reset()


class InotifyEvents:
    """``struct inotify_event`` records, as the kernel writes them, through a pipe."""

    def __init__(self, watch: DirectoryWatch) -> None:
        self.read, self.write = os.pipe()
        os.set_blocking(self.read, False)
        watch._fd = self.read

    def send(self, *names: str, mask: int = DirectoryWatch.IN_MOVED_TO) -> None:
        data = b""
        for name in names:
            raw = name.encode() + b"\0" * (16 - len(name) % 16)
            data += struct.pack("iIII", 1, mask, 0, len(raw)) + raw
        if not names:
            data = struct.pack("iIII", -1, mask, 0, 0)
        os.write(self.write, data)

    def close(self) -> None:
        os.close(self.read)
        os.close(self.write)


async def test_a_file_watch_is_woken_by_its_own_file_only(tmp_path: Path) -> None:
    directory = DirectoryWatch(tmp_path)
    restored = directory.file("restored")
    suspending = directory.file("suspending")
    events = InotifyEvents(directory)
    try:
        events.send(".activity.json.tmp", "activity.json")
        directory._readable()
        assert not restored._woken.is_set() and not suspending._woken.is_set(), (
            "igni's own activity file wakes nobody"
        )
        events.send("restored")
        directory._readable()
        assert restored._woken.is_set() and not suspending._woken.is_set()
        assert restored.woken_at is not None
        events.send(mask=DirectoryWatch.IN_Q_OVERFLOW)
        directory._readable()
        assert suspending._woken.is_set(), "events lost: every file may have changed"
    finally:
        directory._fd = None
        events.close()


def test_the_directory_is_watched_until_its_last_file_lets_go(tmp_path: Path) -> None:
    directory = DirectoryWatch(tmp_path)
    restored = directory.file("restored")
    suspending = directory.file("suspending")
    events = InotifyEvents(directory)
    try:
        restored.close()
        assert directory.watching
        suspending.close()
        assert not directory.watching
    finally:
        os.close(events.write)


def test_the_guest_watches_its_run_directory_once(tmp_path: Path) -> None:
    guest = CloudGuest(
        GuestPaths.from_env({GuestPaths.RUN_DIR_ENV: str(tmp_path)}), gc_freeze=False
    )
    watches = {
        guest.binding._directory.directory,  # type: ignore[union-attr]
        guest.restores._directory.directory,  # type: ignore[union-attr]
        guest.suspends._directory.directory,  # type: ignore[union-attr]
    }
    assert len(watches) == 1


async def test_the_collector_waits_for_the_burst(tmp_path: Path, quick: None) -> None:
    agent = GuestAgent(tmp_path / "restored")
    watch = RestoreWatch(agent.path, watch=False)
    collecting: list[bool] = []

    async def step(_restored: Any) -> None:
        collecting.append(gc.isenabled())

    watch.on_restore(step)
    watch.on_restore(step, when=RestoreStep.LATER)
    watch.start()
    assert gc.isenabled()
    try:
        agent.restore()
        checking = asyncio.create_task(watch.check())
        await asyncio.sleep(0.05)
        watch.answered()
        await asyncio.wait_for(checking, 1)
    finally:
        await watch.stop()
    assert collecting == [False, True], "no collection before the burst is through, then as usual"
    assert gc.isenabled()


def test_the_timeline_says_where_the_time_before_noticing_went() -> None:
    now_ms = time.time() * 1000
    timeline = ResumeTimeline(
        7,
        at_ms=int(now_ms - 50),
        written_ns=int((now_ms - 30) * 1e6),
        woken_at=time.perf_counter() - 0.01,
    )
    timeline.mark("step")
    record = timeline.record
    timeline.finish()
    assert record.written_after_ms is not None and 15 <= record.written_after_ms <= 25
    assert record.woke_after_write_ms is not None and 10 <= record.woke_after_write_ms <= 30
    assert record.noticed_after_ms is not None and record.noticed_after_ms >= 49
    assert record.steps[0].cpu_ms >= 0


def test_the_log_writer_waits_for_the_first_answer(capsys: pytest.CaptureFixture[str]) -> None:
    StdoutLog.reset()
    log = StdoutLog.attach("igni_cloud.tests.held")
    try:
        StdoutLog.hold()
        log.info("first")
        log.info("second")
        StdoutLog.flush()
        assert capsys.readouterr().out == "", "nothing written while held"
        StdoutLog.release()
        StdoutLog.flush()
        lines = [json.loads(line)["message"] for line in capsys.readouterr().out.splitlines()]
        assert lines == ["first", "second"]
    finally:
        log.removeHandler(StdoutLog.handler())
        StdoutLog.reset()


async def test_the_warm_base_practises_its_first_answer_and_keeps_nothing() -> None:
    transport = WebSocketServerTransport(port=0)
    await transport.start()
    try:
        result = await FirstAnswerPractice(ObservedTransport(transport)).run()
        assert result.ok, result.error
        assert result.welcome_ms is not None and result.answer_ms is not None
        assert result.welcome_ms <= result.answer_ms
        assert transport.client_count == 0
        assert not transport._connected.is_set(), "a window is still awaited"
        assert transport._event_seq == 0
        assert transport._inbox.empty(), "the request was answered, not left queued"
    finally:
        await transport.close()


async def test_a_practice_that_cannot_run_says_why() -> None:
    result = await FirstAnswerPractice(None).run()
    assert not result.ok and "no port" in result.error
