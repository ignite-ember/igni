"""``await_process`` — wait for a background process, however long it takes.

The existing ``watch_process`` clamps its window to 30 seconds
(``max(1, min(seconds, 30))``), so waiting for a long build means
calling it in a loop: a turn every half minute, tokens spent on
sleeping, and the agent's attention split between the thing it is
waiting for and the act of waiting.

This tool waits until the process is actually finished and reports the
exit code. It is the one to reach for when the next step depends on the
result — a build, a test run, a migration — as opposed to looking in on
something that is meant to keep running.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from ember_code.core.tools.process_supervisor import ProcessSupervisor
from ember_code.core.tools.shell import EmberShellTools


@pytest.fixture
def tools(tmp_path):
    """A toolkit on its own supervisor, so pids cannot collide with
    another test's registry."""
    return EmberShellTools(base_dir=tmp_path, supervisor=ProcessSupervisor())


#: Long enough to outlive ``run_shell_command``'s startup window —
#: a command that finishes inside it is reaped there and never gets a
#: pid, which is the right behaviour and useless for these tests.
_LONG_ENOUGH = 8


def _pid_from(started: str) -> int:
    match = re.search(r"PID (\d+)", started)
    assert match, f"no pid in: {started!r}"
    return int(match.group(1))


async def test_it_waits_for_a_slow_success_and_reports_code_zero(tools):
    """The point of the tool: a process that outlives every
    ``watch_process`` window still gets waited on in one call."""
    started = await tools.run_shell_command(
        f"sleep {_LONG_ENOUGH} && echo done-ok", background=True
    )
    pid = _pid_from(started)

    result = await tools.await_process(pid, timeout_seconds=60)

    assert "Exited with code 0" in result
    assert "succeeded" in result
    assert "done-ok" in result


async def test_a_failure_is_reported_as_a_failure(tools):
    """The agent branches on this text, so the non-zero case has to be
    unambiguous — not merely the absence of the word success."""
    started = await tools.run_shell_command(f"sleep {_LONG_ENOUGH}; exit 3", background=True)
    pid = _pid_from(started)

    result = await tools.await_process(pid, timeout_seconds=60)

    assert "Exited with code 3" in result
    assert "failed" in result


async def test_it_returns_as_soon_as_the_process_exits(tools):
    """Not "sleeps for the timeout and then checks". A short command
    with an hour-long timeout must come back when the command does, or
    the tool is useless for anything interactive."""
    started = await tools.run_shell_command(f"sleep {_LONG_ENOUGH}", background=True)
    pid = _pid_from(started)

    began = asyncio.get_running_loop().time()
    await tools.await_process(pid, timeout_seconds=3600)
    waited = asyncio.get_running_loop().time() - began

    assert waited < _LONG_ENOUGH + 10, f"waited {waited:.1f}s for a {_LONG_ENOUGH}s process"


async def test_a_wait_that_times_out_leaves_the_process_alone(tools):
    """Giving up on waiting is not the same as giving up on the
    process. A server that is supposed to keep running must still be
    running after the wait expires, and the agent must be told which
    of the two happened."""
    started = await tools.run_shell_command("sleep 30", background=True)
    pid = _pid_from(started)

    result = await tools.await_process(pid, timeout_seconds=1)

    assert "Still running" in result
    assert "Exited" not in result
    mp = tools._supervisor.registry.get(pid)
    assert mp is not None and mp.is_running(), "the process was killed by a timed-out wait"
    await tools.stop_process(pid)


async def test_an_unknown_pid_says_so_rather_than_hanging(tools):
    result = await tools.await_process(999_999, timeout_seconds=5)
    assert "No tracked process" in result


async def test_the_tool_is_registered(tools):
    """It has to be on the toolkit, or the agent never sees it.

    ``async def`` tools land in ``async_functions``; the sync ones are
    in ``functions``. Checking only the latter finds nothing and passes
    for the wrong reason, so both are searched.
    """
    registered = {**getattr(tools, "functions", {}), **getattr(tools, "async_functions", {})}
    assert "await_process" in registered, f"registered: {sorted(registered)}"
