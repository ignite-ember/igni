"""``read_process_output`` promises reads after the process has finished.

Its docstring says the agent may call it "both before and after the
process has finished", and it arms a 10-minute eviction TTL on the first
read of a finished entry specifically so that stays true.

``await_process`` and ``watch_process`` used to end their exited branch
with ``registry.remove(pid)``, which is not a soft mark — it pops the pid
out of the map and cancels any eviction task. So the documented
follow-up read answered "No tracked process with PID …": the tail the
agent had just been shown was unreachable the moment it was shown, and
the TTL the docstring describes could never be reached from the two
tools most likely to be the last thing called on a process.
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


#: Long enough to outlive ``run_shell_command``'s 3 s startup window —
#: a command that finishes inside it is reaped there and never gets a
#: pid, which is the right behaviour and useless for these tests.
_LONG_ENOUGH = 5


def _pid_from(started: str) -> int:
    match = re.search(r"PID (\d+)", started)
    assert match, f"no pid in: {started!r}"
    return int(match.group(1))


async def test_output_survives_await_process(tools):
    started = await tools.run_shell_command(f"sleep {_LONG_ENOUGH}; echo done-ok", background=True)
    pid = _pid_from(started)

    waited = await tools.await_process(pid, timeout_seconds=60)
    assert "Exited with code 0" in waited

    again = await tools.read_process_output(pid)

    assert "No tracked process" not in again, (
        "await_process deleted the registry entry, so read_process_output's "
        "documented post-completion read cannot happen"
    )
    assert "Finished — exit code 0" in again
    assert "done-ok" in again


async def test_output_survives_watch_process(tools):
    started = await tools.run_shell_command(
        f"sleep {_LONG_ENOUGH}; echo watched-ok", background=True
    )
    pid = _pid_from(started)

    watched = await tools.watch_process(pid, seconds=30)
    assert "Exited with code 0" in watched

    again = await tools.read_process_output(pid)

    assert "No tracked process" not in again, (
        "watch_process deleted the registry entry on the exited branch"
    )
    assert "Finished — exit code 0" in again


async def test_the_finished_entry_is_on_the_eviction_clock_not_immortal(tools):
    """Keeping the row is only defensible because it expires.

    A zero TTL evicts on the next loop tick, which is what proves the
    entry went to ``arm_eviction`` rather than simply being leaked.
    """
    tools._supervisor.registry.set_ttl_seconds(0)
    started = await tools.run_shell_command(f"sleep {_LONG_ENOUGH}", background=True)
    pid = _pid_from(started)

    await tools.await_process(pid, timeout_seconds=60)
    # Let the armed eviction task run.
    for _ in range(10):
        if tools._supervisor.registry.get(pid) is None:
            break
        await asyncio.sleep(0.05)

    assert tools._supervisor.registry.get(pid) is None, (
        "the finished entry outlived its TTL — it was kept, not armed"
    )
