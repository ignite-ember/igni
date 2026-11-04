"""Plan mode runs only shell commands on a read-only allow-list (CC-36).

It used to ALLOW every shell command its mutation regex missed, and its
mode step ran before the ask list — so ``git push``, ``terraform apply``
and ``kubectl delete`` ran unasked, in the mode meant for looking.

Now a command runs in plan mode only if it is built from allow-listed
read-only commands. The obvious writers are still denied outright (with
the pointer to ``exit_plan_mode``); everything else is asked about. And
plan mode is never looser than ``default``: what an ask rule or
``safety.require_confirmation`` would ask about there, it asks about here.
"""

from __future__ import annotations

import pytest

from ember_code.core.config.permission_eval import PermissionDecision, PermissionEvaluator
from ember_code.core.config.settings import Settings

SHELL = "run_shell_command"


def _plan(**rules) -> PermissionEvaluator:
    return PermissionEvaluator.from_strings(mode="plan", **rules)


def _decide(ev: PermissionEvaluator, command: str) -> PermissionDecision:
    return ev.evaluate(SHELL, {"command": command})


@pytest.mark.parametrize(
    "command",
    [
        "ls",
        "ls -la src",
        "git status",
        "git log --oneline -10",
        "git diff HEAD~1",
        "git show HEAD",
        "git branch -a",
        "rg foo",
        "rg 'a|b' src",
        "grep -rn TODO src | head -20",
        "find . -name '*.py'",
        "cat README.md",
        "wc -l src/cli.py",
        "pwd",
        "ls -la 2>&1 | grep foo",
    ],
)
def test_read_only_commands_run(command):
    assert _decide(_plan(), command) is PermissionDecision.ALLOW


@pytest.mark.parametrize(
    "command",
    [
        "git push",
        "git push origin main",
        "terraform apply",
        "kubectl delete pod web-1",
        "curl -o out.bin https://example.test/x",
        "npm install",
        "git commit -m wip",
        "git branch new-feature",
        "git diff --output=patch.txt",
        "find . -name '*.pyc' -exec stat {} +",
        "find . -delete",
        "ls | xargs cat",
        "cat $(ls)",
        "ls && pwd",
        "python script.py",
    ],
)
def test_anything_else_is_asked_about(command):
    assert _decide(_plan(), command) is PermissionDecision.ASK


@pytest.mark.parametrize("command", ["rm notes.txt", "echo x > f", "ls; rm -rf x"])
def test_an_obvious_write_is_refused_and_says_how_to_proceed(command):
    """Stricter than asking: plan mode's existing contract for the shapes
    its mutation check recognises is a denial that points the agent at
    ``exit_plan_mode`` rather than a prompt for the person."""
    ev = _plan()
    assert _decide(ev, command) is PermissionDecision.DENY
    assert "exit_plan_mode" in ev.explain_deny(SHELL, {"command": command})


def test_an_ask_rule_outranks_a_read_only_allow():
    """Plan mode is never looser than default."""
    ev = _plan(ask=["Bash(git log*)"])
    assert _decide(ev, "git log") is PermissionDecision.ASK
    assert (
        PermissionEvaluator.from_strings(mode="default", ask=["Bash(git log*)"]).evaluate(
            SHELL, {"command": "git log"}
        )
        is PermissionDecision.ASK
    )


def test_an_ask_rule_outranks_a_read_tool_allow():
    ev = _plan(ask=["fetch_url"])
    assert ev.evaluate("fetch_url", {"url": "https://example.test"}) is PermissionDecision.ASK


def test_require_confirmation_still_asks_in_plan_mode():
    settings = Settings()
    settings.permissions.mode = "plan"
    deny, ask, allow = settings.permissions.category_rules()
    ev = _plan(deny=deny, ask=ask, allow=allow)
    for command in ("git push", "terraform apply", "kubectl delete pod x"):
        assert _decide(ev, command) is PermissionDecision.ASK, command


def test_the_automatic_modes_gain_no_prompts():
    """The tightening is plan mode's alone: bypass still runs what plan
    mode now asks about, and acceptEdits is unchanged."""
    bypass = PermissionEvaluator.from_strings(mode="bypassPermissions")
    for command in ("git push", "terraform apply", "npm install"):
        assert _decide(bypass, command) is PermissionDecision.ALLOW
    accept = PermissionEvaluator.from_strings(mode="acceptEdits")
    assert accept.evaluate("edit_file", {"file_path": "a.py"}) is PermissionDecision.ALLOW
    assert _decide(accept, "npm install") is PermissionDecision.DEFER
