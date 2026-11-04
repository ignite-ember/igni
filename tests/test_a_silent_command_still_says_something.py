"""A command that succeeds without printing must not return "".

``run_foregrounded`` ended on a bare ``return truncate(output)``, and
``output`` is empty for the large family of commands that succeed
silently — ``true``, ``cd``, ``mkdir -p``, ``touch``, ``export``,
``git add``. The model then received an empty tool result, which is
indistinguishable from a tool that failed to produce anything at all,
and the observed consequence is a re-run of a command that already
worked.

The non-zero branch had the same hole with a trailing colon-newline and
nothing after it.
"""

from __future__ import annotations

import pytest

from ember_code.core.tools.process_supervisor import (
    NO_OUTPUT_PLACEHOLDER,
    ProcessSupervisor,
)
from ember_code.core.tools.shell import EmberShellTools


@pytest.fixture
def tools(tmp_path):
    return EmberShellTools(base_dir=tmp_path, supervisor=ProcessSupervisor())


@pytest.mark.parametrize("command", ["true", "cd .", "mkdir -p made-a-dir", "touch a-file"])
async def test_a_silent_success_is_not_the_empty_string(tools, command):
    result = await tools.run_shell_command(command)

    assert result != "", f"{command!r} returned the empty string to the model"
    assert result == NO_OUTPUT_PLACEHOLDER


async def test_a_silent_failure_still_carries_its_exit_code(tools):
    result = await tools.run_shell_command("false")

    # The ``Command exited with code N:`` prefix is load-bearing — the
    # tool-result error detector matches on it (see
    # tests/test_tool_error_conventions_match_shell.py).
    assert result.startswith("Command exited with code 1:")
    assert result.endswith(NO_OUTPUT_PLACEHOLDER)


async def test_real_output_is_untouched(tools):
    """The placeholder must not displace anything a command did print."""
    assert await tools.run_shell_command("echo hi") == "hi"
