"""Background-process completion notifications.

When the agent backgrounds a long-running shell command and moves on,
the shell module fires registered subscribers with the exit info as
soon as the process terminates. The session subscribes a handler
that pushes a notice onto the queue used by ``QueueInjectorHook`` —
that's how the agent learns that work it kicked off in the
background has finished, without having to remember to poll.

These tests verify the subscribe/emit contract in isolation, with no
agent or session involved. The full BE wiring is tested live in the
TUI; here we just confirm the notification surface itself is sound.

All tests are ``async`` because the shell tool methods are now
``async def`` (the sync versions blocked the event loop for up to
``timeout`` seconds — see shell.py module docstring).
"""

from __future__ import annotations

import asyncio
import re

import pytest

from ember_code.core.tools.shell import (
    EmberShellTools,
    subscribe_to_process_completion,
    unsubscribe_from_process_completion,
)


@pytest.fixture
def collected_completions():
    """Record completions; auto-unsubscribe at test end."""
    seen: list[dict] = []
    seen_event = asyncio.Event()

    def _cb(info: dict) -> None:
        seen.append(info)
        seen_event.set()

    subscribe_to_process_completion(_cb)
    try:
        yield seen, seen_event
    finally:
        unsubscribe_from_process_completion(_cb)


async def _wait_for_finish(pid: int, timeout: float = 8.0) -> None:
    """Poll the registry until the process is finished or timeout."""
    from ember_code.core.tools.shell import _registry

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        mp = _registry.get(pid)
        if mp is not None and mp.finished:
            return
        await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_background_process_emits_completion(tmp_path, collected_completions):
    """A backgrounded ``echo`` should fire the subscriber once it exits."""
    seen, seen_event = collected_completions
    tools = EmberShellTools(base_dir=str(tmp_path))

    out = await tools.run_shell_command(args=["echo", "done"], background=True)
    # Either landed as "exited immediately" (which still counts —
    # reader task fires the callback in finally) or "running".
    assert "echo" in out or "Background" in out

    try:
        await asyncio.wait_for(seen_event.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        pytest.fail("completion never fired")

    info = seen[0]
    assert info["exit_code"] == 0
    assert "echo" in info["cmd"]
    assert info["duration_seconds"] >= 0
    assert "done" in info["output_tail"]


@pytest.mark.asyncio
async def test_foreground_process_does_not_emit(tmp_path, collected_completions):
    """A short foreground command (default ``background=False``)
    must NOT fire the completion subscriber. The agent already saw
    the result inline; a queue notification would be redundant noise."""
    seen, seen_event = collected_completions
    tools = EmberShellTools(base_dir=str(tmp_path))

    await tools.run_shell_command(args=["echo", "hi"], timeout=5)
    # Give the loop a tick in case the reader task is still wrapping up.
    await asyncio.sleep(0.2)
    assert seen == [], f"foreground command should not notify, got {seen}"


@pytest.mark.asyncio
async def test_read_process_output_is_idempotent_after_finish(tmp_path):
    """After a backgrounded process finishes, ``read_process_output``
    should keep returning the buffered output across repeated calls
    with different ``tail`` values.

    Uses ``sh -c "echo X; sleep 4"`` so the process is still alive
    after the 3s auto-watch in ``run_shell_command(background=True)``,
    which means the run reports a PID rather than "exited
    immediately" (which evicts the entry from the registry).
    """
    tools = EmberShellTools(base_dir=str(tmp_path))
    out = await tools.run_shell_command(
        args=["sh", "-c", "echo first_read_works; sleep 4"],
        background=True,
    )
    m = re.search(r"PID (\d+)", out)
    assert m, f"no PID in run output: {out!r}"
    pid = int(m.group(1))

    await _wait_for_finish(pid)

    first = await tools.read_process_output(pid, tail=100)
    assert "first_read_works" in first
    assert "Finished" in first

    # Second read with a smaller tail — the entry must still exist.
    second = await tools.read_process_output(pid, tail=10)
    assert "first_read_works" in second, "second read returned no output — entry was evicted"

    # Third read with a different tail — still works.
    third = await tools.read_process_output(pid, tail=1)
    assert "first_read_works" in third


@pytest.mark.asyncio
async def test_finished_process_evicted_after_ttl(tmp_path, monkeypatch):
    """First ``read_process_output`` after a process has finished
    arms a TTL task; once it fires, the entry should be gone from
    the registry. Patches ``_FINISHED_PROCESS_TTL_SECONDS`` to a
    fraction of a second so the test runs quickly."""
    from ember_code.core.tools import shell as _shell
    from ember_code.core.tools.shell import _registry

    monkeypatch.setattr(_shell, "_FINISHED_PROCESS_TTL_SECONDS", 0.3)

    tools = EmberShellTools(base_dir=str(tmp_path))
    out = await tools.run_shell_command(
        args=["sh", "-c", "echo hi; sleep 4"],
        background=True,
    )
    pid = int(re.search(r"PID (\d+)", out).group(1))
    await _wait_for_finish(pid)

    first = await tools.read_process_output(pid)
    assert "hi" in first

    # Before TTL fires, the entry is still there.
    assert _registry.get(pid) is not None

    # Wait past TTL — entry should be evicted. ``asyncio.sleep`` so
    # the eviction task (also running on the loop) gets to fire.
    await asyncio.sleep(0.6)
    assert _registry.get(pid) is None
    gone = await tools.read_process_output(pid)
    assert "No tracked process" in gone


@pytest.mark.asyncio
async def test_ttl_resets_on_subsequent_read(tmp_path, monkeypatch):
    """A second read before the TTL expires should reset it, so an
    actively-engaged agent doesn't lose the buffer mid-iteration."""
    from ember_code.core.tools import shell as _shell
    from ember_code.core.tools.shell import _registry

    monkeypatch.setattr(_shell, "_FINISHED_PROCESS_TTL_SECONDS", 0.4)

    tools = EmberShellTools(base_dir=str(tmp_path))
    out = await tools.run_shell_command(
        args=["sh", "-c", "echo hi; sleep 4"],
        background=True,
    )
    pid = int(re.search(r"PID (\d+)", out).group(1))
    await _wait_for_finish(pid)

    await tools.read_process_output(pid)  # arms task (0.4s)
    await asyncio.sleep(0.25)  # part-way through
    await tools.read_process_output(pid)  # resets task (0.4s again)
    await asyncio.sleep(0.25)  # original task would have fired by now

    # Reset means we should still be alive — only 0.25s into the new
    # window.
    assert _registry.get(pid) is not None
    # Wait out the new window plus slack.
    await asyncio.sleep(0.4)
    assert _registry.get(pid) is None


@pytest.mark.asyncio
async def test_unsubscribe_stops_emissions(tmp_path):
    """After ``unsubscribe_from_process_completion`` the callback
    must no longer be called — important for tests and for the BE's
    shutdown path so we don't leak into a process tree that's about
    to be killed anyway."""
    seen: list[dict] = []

    def _cb(info: dict) -> None:
        seen.append(info)

    subscribe_to_process_completion(_cb)
    unsubscribe_from_process_completion(_cb)

    tools = EmberShellTools(base_dir=str(tmp_path))
    await tools.run_shell_command(args=["echo", "after_unsub"], background=True)

    # Wait long enough for the reader task to finish.
    await asyncio.sleep(1.0)
    assert seen == []


@pytest.mark.asyncio
async def test_run_shell_command_does_not_block_loop(tmp_path):
    """The whole point of going async — while a shell tool is
    executing, the event loop must keep servicing other tasks. We
    verify by starting a slow shell call and a concurrent ticker;
    the ticker should advance during the shell wait. With the
    earlier sync impl, the ticker would freeze for the duration of
    the shell timeout."""
    tools = EmberShellTools(base_dir=str(tmp_path))

    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.05)
            ticks += 1

    ticker_task = asyncio.create_task(_ticker())
    # ``timeout=2`` gives the ticker ~40 ticks of headroom; even a
    # cautious threshold of 5 confirms the loop is alive.
    await tools.run_shell_command(args=["sh", "-c", "sleep 1.5"], timeout=2)
    await ticker_task

    assert ticks >= 5, f"event loop appeared frozen during shell command; ticks={ticks}"
