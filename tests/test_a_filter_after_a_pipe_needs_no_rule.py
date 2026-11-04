"""A pure filter after a ``|`` is allowed without a rule of its own.

After F5 every command on a line needs its own allow rule, so
``pytest -q 2>&1 | tail -20`` — the shape a model adds to almost every
command — needed ``Bash(tail *)`` beside ``Bash(pytest *)``. A fixed list
of filters (:data:`PIPE_FILTERS`) now passes in pipe-tail position when
it only reads its input and writes its output: no file operand, no option
that writes, follows, recurses or runs a program.

Everything else is as F5 left it: the command before the pipe still needs
a rule, deny and ask rules still match any command on the line, and a
filter that is not after a ``|`` is an ordinary command.
"""

from __future__ import annotations

import pytest

from ember_code.core.config.permission_eval import PermissionDecision
from ember_code.core.config.permission_eval.pipe_filters import PIPE_FILTERS, is_pipe_filter
from ember_code.core.config.permission_eval.pipeline import PermissionEvaluator
from ember_code.core.config.permission_eval.read_only_shell import is_read_only


def _decide(command: str, **rules: list[str]) -> PermissionDecision:
    evaluator = PermissionEvaluator.from_strings(**rules)
    return evaluator.evaluate("run_shell_command", {"command": command})


ALLOW = {"allow": ["Bash(pytest *)", "Bash(ls *)", "Bash(cat *)", "Bash(git log *)"]}


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q 2>&1 | tail -20",
        "pytest -q | tail -n 5",
        "ls -la | head",
        "ls -la | head -n 20 | wc -l",
        "ls -la | sort | uniq -c | sort -rn",
        "ls -la | sort -k 2 -t ,",
        "cat x | grep -n foo",
        "cat x | grep -e foo -e bar",
        "cat x | rg -i 'a b'",
        "cat x | cut -d , -f 1",
        "cat x | tr a-z A-Z",
        "cat x | nl -ba",
        "cat x.json | jq '.items[]'",
        "cat x.json | jq --arg n v '.[$n]'",
        "git log --oneline | head -5",
    ],
)
def test_a_filter_after_an_allowed_command_runs(command: str):
    assert _decide(command, **ALLOW) is PermissionDecision.ALLOW


@pytest.mark.parametrize(
    "command",
    [
        # Not filters: they write, or run something.
        "ls -la | tee x",
        "cat x | sh",
        "ls -la | xargs rm",
        "cat x | bash -c 'rm -rf y'",
        "cat x | sed -i s/a/b/ y",
        "cat x | awk '{system(\"rm -rf y\")}'",
        "cat x | python3",
        # Listed, but with an option that writes, follows or runs.
        "ls -la | sort -o out.txt",
        "ls -la | sort --output=out.txt",
        "ls -la | sort --outp=out.txt",
        "ls -la | sort --compress-program=sh",
        "ls -la | uniq - out.txt",
        "ls -la | tail -f",
        "ls -la | rg --pre sh x",
        "ls -la | rg -z x",
        # Listed, but reading files of its own rather than its input.
        "ls -la | grep -r secret /",
        "ls -la | grep foo /etc/shadow",
        "ls -la | grep -f /etc/shadow",
        "ls -la | head /etc/shadow",
        "ls -la | jq . /etc/passwd",
        "ls -la | jq --rawfile s /etc/shadow .",
        "ls -la | wc --files0-from=list",
        # Interactive pagers are not filters.
        "ls -la | less",
        "ls -la | more",
    ],
)
def test_what_is_not_a_pure_filter_still_needs_a_rule(command: str):
    assert _decide(command, **ALLOW) is not PermissionDecision.ALLOW


def test_the_command_before_the_pipe_still_needs_its_rule():
    assert _decide("terraform plan | tail -20", **ALLOW) is not PermissionDecision.ALLOW
    assert _decide("ls -la | head", allow=[]) is not PermissionDecision.ALLOW


def test_a_filter_not_after_a_pipe_is_an_ordinary_command():
    # ``head`` reads a file here, and the line joins with ``&&``.
    assert _decide("head README.md", **ALLOW) is not PermissionDecision.ALLOW
    assert _decide("ls && head -5", **ALLOW) is not PermissionDecision.ALLOW
    assert _decide("ls; wc -l", **ALLOW) is not PermissionDecision.ALLOW


def test_every_command_after_the_pipe_is_still_checked():
    assert _decide("ls -la | head && rm -rf x", **ALLOW) is not PermissionDecision.ALLOW
    assert _decide("ls -la | head | sh", **ALLOW) is not PermissionDecision.ALLOW


def test_deny_and_ask_rules_still_match_the_filter():
    assert (
        _decide("ls -la | head", deny=["Bash(head*)"], allow=["Bash(ls *)"])
        is PermissionDecision.DENY
    )
    assert (
        _decide("ls -la | tail -3", ask=["Bash(tail *)"], allow=["Bash(ls *)"])
        is PermissionDecision.ASK
    )


def test_the_list_is_fixed_and_has_no_pagers_or_writers():
    assert set(PIPE_FILTERS) == {
        "head",
        "tail",
        "wc",
        "sort",
        "uniq",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "cut",
        "tr",
        "nl",
        "jq",
    }
    for name in ("tee", "xargs", "sed", "awk", "less", "more", "sh", "cat"):
        assert not is_pipe_filter(name)


def test_plan_mode_sort_refuses_an_abbreviated_output_flag():
    # GNU getopt accepts an unambiguous prefix of a long option, so
    # ``--outp=x`` is ``--output=x``; the read-only allow-list missed it.
    assert not is_read_only("ls | sort --outp=x")
    assert not is_read_only("sort --compress=sh x")
    assert is_read_only("ls | sort -rn")
