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
