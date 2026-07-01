"""Tests for the ``asyncRewake`` hook execution mode.

CC's third execution mode: a background hook that, when it exits
with code 2, "wakes" the agent by injecting its stderr/stdout as
a system reminder into the next turn. Ember's implementation
buffers wakes in ``Session._pending_reminders`` and drains them
at the top of ``handle_message``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from ember_code.core.hooks.executor import HookExecutor
from ember_code.core.hooks.loader import HookLoader
from ember_code.core.hooks.schemas import HookDefinition


async def _settle_background_tasks() -> None:
    """Give ``asyncio.create_task``-scheduled background hook coros
    one or two event-loop ticks to actually run. ``execute()``
    spawns them and returns immediately, so a test that wants to
    inspect their side effects has to yield first."""
    for _ in range(20):
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_async_rewake_exit_2_fires_rewake_callback() -> None:
    """A background command that exits 2 → ``rewake_callback`` is
    called with the hook's stderr+stdout (CC's contract). The
    foreground execute() result is non-blocking — the agent
    already moved on."""
    captured: list[str] = []
    hook = HookDefinition(
        type="command",
        command="echo 'wake me with this' >&2; exit 2",
        async_rewake=True,
    )
    executor = HookExecutor({"PreToolUse": [hook]}, rewake_callback=captured.append)
    result = await executor.execute("PreToolUse", payload={})
    assert result.should_continue is True
    await _settle_background_tasks()
    assert len(captured) == 1
    assert "wake me with this" in captured[0]


@pytest.mark.asyncio
async def test_async_rewake_exit_0_no_wake() -> None:
    """Background hook exits 0 → no wake. Quiet success path."""
    captured: list[str] = []
    hook = HookDefinition(
        type="command",
        command="echo ok; exit 0",
        async_rewake=True,
    )
    executor = HookExecutor({"PreToolUse": [hook]}, rewake_callback=captured.append)
    await executor.execute("PreToolUse", payload={})
    await _settle_background_tasks()
    assert captured == []


@pytest.mark.asyncio
async def test_async_rewake_exit_2_uses_systemMessage_when_present() -> None:
    """If the hook writes JSON to stdout with a ``systemMessage``
    field, that's preferred over stderr for the wake text — same
    precedence as a blocking command hook."""
    captured: list[str] = []
    body = json.dumps({"systemMessage": "from-json"})
    hook = HookDefinition(
        type="command",
        command=f"echo '{body}'; echo 'stderr-fallback' >&2; exit 2",
        async_rewake=True,
    )
    executor = HookExecutor({"PreToolUse": [hook]}, rewake_callback=captured.append)
    await executor.execute("PreToolUse", payload={})
    await _settle_background_tasks()
    assert len(captured) == 1
    assert captured[0] == "from-json"


@pytest.mark.asyncio
async def test_background_without_async_rewake_does_not_wake() -> None:
    """Plain ``background: True`` hook that exits 2 → still fire-
    and-forget, NO wake (the difference between the two modes)."""
    captured: list[str] = []
    hook = HookDefinition(
        type="command",
        command="echo 'should be ignored' >&2; exit 2",
        background=True,  # plain background, no async_rewake
    )
    executor = HookExecutor({"PreToolUse": [hook]}, rewake_callback=captured.append)
    await executor.execute("PreToolUse", payload={})
    await _settle_background_tasks()
    assert captured == []


@pytest.mark.asyncio
async def test_async_rewake_does_not_block_foreground_execute() -> None:
    """``async_rewake`` hooks should NOT delay ``execute()`` — they
    run in the background even if not explicitly marked
    ``background``. Otherwise the agent would block on a hook it
    was supposed to be woken by later.

    Uses a ``sleep 0.3`` (not the previous ``sleep 5``) so the
    subprocess actually finishes before pytest's asyncio teardown
    runs — Python 3.11/3.12's ``asyncio.run`` gathers still-
    pending tasks at loop shutdown and blocks on them, which
    hung this test's cleanup for the full CI job timeout. A
    tighter assertion (< 0.15s) preserves the "was execute
    non-blocking?" signal despite the shorter sleep: a
    foreground call would have taken ~300ms.
    """
    hook = HookDefinition(
        type="command",
        command="sleep 0.3; exit 2",  # would block ~300ms if foreground
        async_rewake=True,
    )
    executor = HookExecutor({"PreToolUse": [hook]}, rewake_callback=lambda _t: None)
    start = asyncio.get_running_loop().time()
    await executor.execute("PreToolUse", payload={})
    elapsed = asyncio.get_running_loop().time() - start
    assert elapsed < 0.15, f"expected fast return, took {elapsed:.2f}s"
    # Give the background subprocess time to finish so pytest's
    # asyncio teardown doesn't wait on it.
    await _settle_background_tasks()


@pytest.mark.asyncio
async def test_async_rewake_no_callback_degrades_to_background() -> None:
    """If the executor has no rewake_callback wired, the
    async_rewake hook behaves like a regular background hook —
    runs, no crash, no wake."""
    hook = HookDefinition(
        type="command",
        command="exit 2",
        async_rewake=True,
    )
    executor = HookExecutor({"PreToolUse": [hook]})  # no callback
    result = await executor.execute("PreToolUse", payload={})
    assert result.should_continue is True
    await _settle_background_tasks()


# ── Loader accepts both key variants ──────────────────────────────


def test_loader_accepts_asyncRewake_camelcase(tmp_path: Path, monkeypatch: Any) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".ember").mkdir()
    (fake_home / ".ember" / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {"type": "command", "command": "x", "asyncRewake": True},
                    ]
                }
            }
        )
    )
    with monkeypatch.context() as m:
        m.setattr(Path, "home", lambda: fake_home)
        hooks = HookLoader(tmp_path).load()
    assert hooks["PreToolUse"][0].async_rewake is True


def test_loader_accepts_async_rewake_snakecase(tmp_path: Path, monkeypatch: Any) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".ember").mkdir()
    (fake_home / ".ember" / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {"type": "command", "command": "x", "async_rewake": True},
                    ]
                }
            }
        )
    )
    with monkeypatch.context() as m:
        m.setattr(Path, "home", lambda: fake_home)
        hooks = HookLoader(tmp_path).load()
    assert hooks["PreToolUse"][0].async_rewake is True
