"""A shell result that is not the whole output says so, with the counts.

``run_shell_command`` returns the last ``tail`` lines of a command's
output (100 by default). It used to cut silently: against the real
igni-1.0-4b, ``cat`` of a 6,000-line log came back as its last 100
lines with nothing to say the rest existed, and the model reported a
WARN line near the end as "the ERROR entry" — the error was at line
4,217.

The cut is now announced inside the result — how many lines and
characters were left out, and how to see them — with the
``... (N characters truncated) ...`` marker the fine-tuning corpus uses.
Output that fits is returned exactly as before.
"""

from __future__ import annotations

import re

import pytest

from ember_code.core.hooks.executor import HookExecutor
from ember_code.core.hooks.tool_hook import ToolEventHook
from ember_code.core.tools.output_tail import OutputTail
from ember_code.core.tools.process_supervisor import ProcessSupervisor
from ember_code.core.tools.shell import EmberShellTools

#: The 6,000-line log of the run that found this, one ERROR in the middle.
LOG_LINES = [
    "ERROR db: connection refused" if n == 4217 else f"INFO request {n} ok" for n in range(1, 6001)
]


@pytest.fixture
def tools(tmp_path):
    (tmp_path / "app.log").write_text("\n".join(LOG_LINES) + "\n")
    (tmp_path / "short.log").write_text("\n".join(LOG_LINES[:50]) + "\n")
    return EmberShellTools(base_dir=tmp_path, supervisor=ProcessSupervisor())


def _omitted_chars(lines: list[str]) -> int:
    return sum(len(line) + 1 for line in lines)


async def test_a_cut_output_says_how_much_was_left_out_and_how_to_see_it(tools):
    result = await tools.run_shell_command("cat app.log", timeout=30)

    assert result.startswith(
        "[Output truncated: 6000 lines; the first 5900 are not shown, only the last 100. "
    )
    assert "grep -n" in result.splitlines()[0]
    assert f"... ({_omitted_chars(LOG_LINES[:5900])} characters truncated) ...\n\n" in result
    assert result.endswith("\n".join(LOG_LINES[-100:]))
    assert "ERROR" not in result


async def test_an_output_that_fits_is_returned_unchanged(tools):
    result = await tools.run_shell_command("cat short.log", timeout=30)

    assert result == "\n".join(LOG_LINES[:50])


async def test_a_larger_tail_shows_more_and_says_what_is_still_left_out(tools):
    result = await tools.run_shell_command("cat app.log", timeout=30, tail=2000)

    assert "the first 4000 are not shown, only the last 2000" in result
    assert "ERROR db: connection refused" in result


async def test_the_cut_result_is_still_wrapped_as_untrusted_output(tools):
    hook = ToolEventHook(HookExecutor({}), session_id="s1")

    result = await hook(
        name="run_shell_command",
        func=tools.run_shell_command,
        args={"command": "cat app.log", "timeout": 30},
    )

    match = re.match(r"^<untrusted_tool_output nonce=([0-9a-f]+)>\n\[Output truncated: ", result)
    assert match, result[:120]
    assert result.endswith(f"\n</untrusted_tool_output nonce={match.group(1)}>")


def test_lines_the_buffer_already_dropped_are_counted():
    tail = OutputTail.of(["c", "d", "e"], 2, dropped_lines=2, dropped_chars=4)

    assert (tail.total_lines, tail.omitted_lines, tail.omitted_chars) == (5, 3, 6)
    assert tail.render("More.").endswith("... (6 characters truncated) ...\n\nd\ne")


def test_nothing_left_out_renders_the_lines_alone():
    assert OutputTail.of(["a", "b"], 100).render("More.") == "a\nb"


# ── A process that outlived a restart ───────────────────────────────


def _restored(kind: str, tmp_path, lines: list[str]):
    """A process read back from its log after a restart, of ``kind``."""
    import os
    import time

    from ember_code.core.tools.finished_process import FinishedProcess
    from ember_code.core.tools.orphan_process import OrphanProcess
    from ember_code.core.tools.process_log import ProcessLogStore

    store = ProcessLogStore(tmp_path)
    pid = os.getpid()
    path = store.path(pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    if kind == "orphan":
        return pid, OrphanProcess(
            pid=pid, cmd="serve", started_epoch=int(time.time()), pgid=None, log_store=store
        )
    return pid, FinishedProcess(
        pid=pid, cmd="build", started_epoch=0, finished_epoch=5, exit_code=1, log_store=store
    )


@pytest.mark.parametrize("kind", ["orphan", "finished"])
async def test_a_process_from_before_a_restart_says_when_it_is_cut(kind, tmp_path):
    """read_process_output on a process the previous backend started read
    its log file and cut it to the last ``tail`` lines with nothing to say
    so. A finished one read back nothing at all: it asked the log store
    for a method it does not have, and the error was taken for "no log"."""
    supervisor = ProcessSupervisor()
    shell = EmberShellTools(base_dir=tmp_path, supervisor=supervisor)
    pid, process = _restored(kind, tmp_path, LOG_LINES)
    supervisor.registry.add(process)

    result = await shell.read_process_output(pid)

    assert (
        "[Output truncated: 6000 lines; the first 5900 are not shown, only the last 100. " in result
    )
    assert f"... ({_omitted_chars(LOG_LINES[:5900])} characters truncated) ..." in result
    assert result.rstrip().endswith(LOG_LINES[-1])


@pytest.mark.parametrize("kind", ["orphan", "finished"])
async def test_a_restored_log_that_fits_is_returned_whole(kind, tmp_path):
    supervisor = ProcessSupervisor()
    shell = EmberShellTools(base_dir=tmp_path, supervisor=supervisor)
    pid, process = _restored(kind, tmp_path, LOG_LINES[:50])
    supervisor.registry.add(process)

    result = await shell.read_process_output(pid)

    assert "Output truncated" not in result
    assert "\n".join(LOG_LINES[:50]) in result
