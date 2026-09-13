"""A `$` command is a background process like any other.

Typing `$ npm run dev` used to spawn a child of the BE that nothing in
the UI could see: the watcher listed nothing, no output streamed, no
button stopped it, and at 120 seconds it was killed. The same command
run by the agent got a watcher row, a live log, a kill button and an
auto-background on timeout, because it went through
``ProcessRegistry`` and the `$` path did not.

These tests pin three things: a short command still reads as a captured
one-shot, a long one is backgrounded rather than killed, and a finished
one stays in the history instead of being deleted at the moment it
becomes worth reading.
"""

from __future__ import annotations

import pytest

from ember_code.backend.captured_shell_runner import CapturedShellRunner
from ember_code.core.tools.process_supervisor import ProcessSupervisor


@pytest.fixture
def supervisor():
    """A private supervisor, so these rows cannot collide with another
    test's registry."""
    return ProcessSupervisor()


def _runner(tmp_path, supervisor, **kw):
    return CapturedShellRunner(project_dir=tmp_path, supervisor=supervisor, **kw)


async def test_a_quick_command_still_returns_its_output(tmp_path, supervisor):
    result = await _runner(tmp_path, supervisor).run("echo hello-there")

    assert "hello-there" in result.output
    assert result.exit_code == 0


async def test_a_failing_command_reports_its_exit_code(tmp_path, supervisor):
    """The FE renders this on the shell card, so it has to be the real
    code and not a flattened 0."""
    result = await _runner(tmp_path, supervisor).run("exit 7")

    assert result.exit_code == 7


async def test_a_finished_command_stays_in_the_history(tmp_path, supervisor):
    """The row survives the process.

    It used to be deleted the moment the command exited, which is the
    moment it becomes worth reading — a failed build vanished from the
    watcher before anyone could open it. Finished entries now stay until
    the registry's eviction TTL drops them.
    """
    await _runner(tmp_path, supervisor).run("echo done-and-gone")

    known = _known(supervisor)
    assert any("done-and-gone" in cmd for _, cmd in known), f"no history row: {known}"


async def test_the_history_row_reports_how_it_ended(tmp_path, supervisor):
    """A row that says nothing about the exit code is not history, it
    is a list of names."""
    from ember_code.backend.server_processes import ProcessesController

    await _runner(tmp_path, supervisor).run("exit 4")

    rows = ProcessesController(supervisor=supervisor).list()
    finished = [r for r in rows if not r.is_running]
    assert finished, f"the list RPC hid the finished process: {rows}"
    assert finished[0].exit_code == 4


async def test_a_long_command_is_backgrounded_not_killed(tmp_path, supervisor):
    """The behaviour that was missing. Before this, the process was
    killed at the timeout and the user got "(timed out)"."""
    runner = _runner(tmp_path, supervisor, timeout_seconds=1)

    result = await runner.run("sleep 30")

    assert "backgrounded as PID" in result.output
    assert "timed out" not in result.output
    assert result.exit_code == 0

    pids = [pid for pid, _ in _known(supervisor)]
    assert pids, "the backgrounded process is not in the registry"

    for pid in pids:
        await _stop(supervisor, pid)


async def test_the_backgrounded_command_is_visible_to_the_watcher(tmp_path, supervisor):
    """The watcher reads the registry. A row has to be there, with the
    command on it, or the panel says "no processes" while one runs."""
    runner = _runner(tmp_path, supervisor, timeout_seconds=1)

    await runner.run("sleep 30")

    commands = [cmd for _, cmd in _known(supervisor)]
    assert any("sleep 30" in c for c in commands), f"watcher would show: {commands}"

    for pid, _ in _known(supervisor):
        await _stop(supervisor, pid)


def _known(supervisor):
    """(pid, cmd) for everything the watcher would list."""
    return [(pid, cmd) for pid, cmd, *_ in supervisor.registry.all_known()]


async def _stop(supervisor, pid):
    mp = supervisor.registry.get(pid)
    if mp is not None and mp.is_running():
        mp.kill()
    supervisor.registry.remove(pid)
