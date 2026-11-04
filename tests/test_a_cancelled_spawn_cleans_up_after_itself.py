"""A cancelled spawn still has to put its worktree back.

``SpawnRunner.run`` guarded the streaming await with ``except
asyncio.TimeoutError`` and ``except Exception`` and had no ``finally``.
``asyncio.CancelledError`` has been a ``BaseException`` since 3.8, so it
matched neither: a spawn cancelled mid-flight — session teardown, a
client disconnect, an outer ``wait_for`` — skipped both the
``SubagentStop`` hook (the FE's progress card never closes) and
``_on_finally`` (an ``isolation=worktree`` spawn leaves its git worktree
and branch on disk with nothing left holding a reference to them).

``AgentSpawn._on_finally`` was also an empty ``pass``, so even once the
template called it on every path there was nothing there to finalize.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ember_code.core.tools.orchestrate_budget import SpawnBudget
from ember_code.core.tools.orchestrate_spawn import AgentSpawn, SpawnRunner


class _Runner(SpawnRunner):
    """Minimal concrete runner whose ``_execute`` never returns."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.finally_calls = 0
        self.entered = asyncio.Event()

    async def _execute(self) -> tuple[str, list[str]]:
        self.entered.set()
        await asyncio.sleep(3600)
        return "unreachable", []

    def _agent_label(self) -> str:
        return "probe"

    def format_result(self, *, elapsed: float, result: str, activity: list[str]) -> str:
        return result

    def _on_finally(self) -> None:
        self.finally_calls += 1


def _runner(**overrides) -> _Runner:
    kwargs = dict(
        pool=SimpleNamespace(),
        settings=SimpleNamespace(orchestration=SimpleNamespace(sub_team_timeout=3600)),
        session_id="s1",
        hook_executor=None,
        on_progress=None,
        event_appender=None,
        subagent_registry=SimpleNamespace(),
        hitl_coordinator=None,
        budget=SpawnBudget(max_agents=10),
        current_depth=0,
        max_depth=3,
        project_dir=None,
        deps=SimpleNamespace(),
    )
    kwargs.update(overrides)
    return _Runner(**kwargs)


class _SpySandbox:
    def __init__(self) -> None:
        self.finalize_calls = 0

    def finalize(self) -> str:
        self.finalize_calls += 1
        return ""


async def test_cancellation_still_runs_the_cleanup_hook():
    runner = _runner()
    task = asyncio.create_task(runner.run())
    await asyncio.wait_for(runner.entered.wait(), timeout=5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert runner.finally_calls == 1, (
        "_on_finally never ran on the cancellation path — an isolated spawn's "
        "worktree and branch are left on disk with nobody holding a reference"
    )


async def test_cancellation_still_fires_subagent_stop():
    runner = _runner()
    task = asyncio.create_task(runner.run())
    await asyncio.wait_for(runner.entered.wait(), timeout=5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert runner._stop_fired is True, (
        "SubagentStop never fired, so the FE's team-progress card for this "
        "spawn stays open for the rest of the session"
    )


async def test_cancellation_still_propagates():
    """Cleaning up must not swallow the cancellation the caller asked for."""
    runner = _runner()
    task = asyncio.create_task(runner.run())
    await asyncio.wait_for(runner.entered.wait(), timeout=5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


async def test_the_success_path_still_runs_the_cleanup_hook_exactly_once():
    """The ``finally`` replaced three per-branch calls; not four, not two."""

    class _Fast(_Runner):
        async def _execute(self) -> tuple[str, list[str]]:
            self.entered.set()
            return "done", []

    runner = _Fast(
        pool=SimpleNamespace(),
        settings=SimpleNamespace(orchestration=SimpleNamespace(sub_team_timeout=3600)),
        session_id="s1",
        hook_executor=None,
        on_progress=None,
        event_appender=None,
        subagent_registry=SimpleNamespace(),
        hitl_coordinator=None,
        budget=SpawnBudget(max_agents=10),
        current_depth=0,
        max_depth=3,
        project_dir=None,
        deps=SimpleNamespace(),
    )

    assert await runner.run() == "done"
    assert runner.finally_calls == 1


def test_agent_spawn_finalizes_its_sandbox_from_the_cleanup_hook():
    """The hook the template now calls on every path has to do something."""
    spawn = AgentSpawn(
        agent_name="a",
        task="t",
        isolation="none",
        pool=SimpleNamespace(),
        settings=SimpleNamespace(orchestration=SimpleNamespace(sub_team_timeout=1)),
        session_id="s1",
        hook_executor=None,
        on_progress=None,
        event_appender=None,
        subagent_registry=SimpleNamespace(),
        hitl_coordinator=None,
        budget=SpawnBudget(max_agents=10),
        current_depth=0,
        max_depth=3,
        project_dir=None,
        deps=SimpleNamespace(),
    )
    sandbox = _SpySandbox()
    spawn._sandbox = sandbox

    spawn._on_finally()

    assert sandbox.finalize_calls == 1


def test_the_cleanup_hook_tolerates_a_spawn_that_never_built_a_sandbox():
    spawn = AgentSpawn(
        agent_name="a",
        task="t",
        isolation="none",
        pool=SimpleNamespace(),
        settings=SimpleNamespace(orchestration=SimpleNamespace(sub_team_timeout=1)),
        session_id="s1",
        hook_executor=None,
        on_progress=None,
        event_appender=None,
        subagent_registry=SimpleNamespace(),
        hitl_coordinator=None,
        budget=SpawnBudget(max_agents=10),
        current_depth=0,
        max_depth=3,
        project_dir=None,
        deps=SimpleNamespace(),
    )

    spawn._on_finally()  # must not raise
