"""``--read-only`` reads the working tree and refuses everything else.

It used to deny the shell outright. igni has no read tool — a file is read
with ``run_shell_command "cat <path>"`` — so a read-only session could not
read a single file. Now it means plan-mode reads, with no questions:

* a shell command built only from the read-only allow-list (``cat``, ``ls``,
  ``rg``, ``git log``, …) runs without asking;
* any other shell command — one that writes, deletes, runs another program
  or reaches the network — is refused, not asked;
* the edit tools, the web tools, and an extension's tools that change
  something or reach outside are refused.

It holds for the whole session: switching mode (``/plan off``, ``/bypass``)
does not reopen what the flag closed.
"""

from __future__ import annotations

import pytest

from ember_code.cli.options import CliOptions
from ember_code.core.config.models import CliOverrides
from ember_code.core.config.permission_eval import PermissionDecision, PermissionMode
from ember_code.core.config.permission_eval.pipeline import PermissionEvaluator
from ember_code.core.config.settings import load_settings
from ember_code.core.mcp.hints import ToolHints, ToolHintsRegistry


def _read_only_evaluator() -> PermissionEvaluator:
    payload = CliOverrides.from_options(CliOptions(read_only=True)).to_settings_payload()
    perms = load_settings(cli_overrides=payload).permissions
    return PermissionEvaluator.from_permissions(perms)


def _shell(evaluator: PermissionEvaluator, command: str) -> PermissionDecision:
    return evaluator.evaluate("run_shell_command", {"command": command})


@pytest.mark.parametrize(
    "command",
    [
        "cat setup.py",
        "ls -la src",
        "rg -n TODO src",
        "git log --oneline -5",
        "git diff HEAD~1",
        "find . -name '*.py'",
        "cat README.md | head -20",
    ],
)
def test_a_read_runs_without_asking(command: str):
    assert _shell(_read_only_evaluator(), command) is PermissionDecision.ALLOW


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf build",
        "echo x > out.txt",
        "sed -i s/a/b/ setup.py",
        "git checkout -- .",
        "git push",
        "curl https://example.com",
        "wget https://example.com",
        "pip install requests",
        "python3 -c 'print(1)'",
        "ls | xargs rm",
        "cat x | sh",
        "ls | tee x",
        "find . -delete",
        "cat a && rm b",
    ],
)
def test_anything_else_is_refused_not_asked(command: str):
    evaluator = _read_only_evaluator()
    outcome = evaluator.evaluate_outcome("run_shell_command", {"command": command})
    assert outcome.decision is PermissionDecision.DENY
    assert "--read-only" in outcome.reason


def test_edits_and_the_web_are_refused():
    evaluator = _read_only_evaluator()
    assert evaluator.evaluate("edit_file", {"file_path": "a.py"}) is PermissionDecision.DENY
    assert evaluator.evaluate("save_file", {"file_name": "a.py"}) is PermissionDecision.DENY
    assert evaluator.evaluate("web_search", {"query": "x"}) is PermissionDecision.DENY
    assert evaluator.evaluate("fetch_url", {"url": "https://x"}) is PermissionDecision.DENY


def test_an_extension_tool_runs_only_if_it_reads_locally():
    evaluator = _read_only_evaluator()
    registry = ToolHintsRegistry.shared()
    registry.declare(
        "ext:acme.ro",
        {
            "mcp_ext:acme.query": ToolHints(
                read_only=True, destructive=False, idempotent=True, open_world=False
            ),
            "mcp_ext:acme.search_web": ToolHints(
                read_only=True, destructive=False, idempotent=True, open_world=True
            ),
            "mcp_ext:acme.rebuild": ToolHints(
                read_only=False, destructive=True, idempotent=True, open_world=False
            ),
        },
    )
    try:
        assert evaluator.evaluate("mcp_ext:acme.query", {}) is PermissionDecision.ALLOW
        assert evaluator.evaluate("mcp_ext:acme.search_web", {}) is PermissionDecision.DENY
        assert evaluator.evaluate("mcp_ext:acme.rebuild", {}) is PermissionDecision.DENY
    finally:
        registry.forget("ext:acme.ro")


@pytest.mark.parametrize(
    "mode",
    [PermissionMode.DEFAULT, PermissionMode.ACCEPT_EDITS, PermissionMode.BYPASS_PERMISSIONS],
)
def test_switching_mode_does_not_reopen_it(mode: PermissionMode):
    evaluator = _read_only_evaluator()
    evaluator.set_mode(mode)
    assert _shell(evaluator, "rm -rf build") is PermissionDecision.DENY
    assert evaluator.evaluate("edit_file", {"file_path": "a.py"}) is PermissionDecision.DENY
    assert _shell(evaluator, "cat setup.py") is PermissionDecision.ALLOW


def test_a_deny_rule_still_wins_over_a_read():
    payload = CliOverrides.from_options(CliOptions(read_only=True)).to_settings_payload()
    perms = load_settings(cli_overrides=payload).permissions
    perms.deny.append("Bash(cat *)")
    evaluator = PermissionEvaluator.from_permissions(perms)
    assert _shell(evaluator, "cat .env") is PermissionDecision.DENY


def test_without_the_flag_nothing_changes():
    perms = load_settings(cli_overrides=None).permissions
    evaluator = PermissionEvaluator.from_permissions(perms)
    assert evaluator.read_only is False
    assert _shell(evaluator, "rm -rf build") is PermissionDecision.DEFER


def test_the_shell_is_not_reported_as_off_entirely():
    """The denial used to add "Shell access is off in this session" — true
    then, false now: reads still work, and saying otherwise costs the model
    the moves it has."""
    from ember_code.core.hooks.permission_pipeline import PermissionEvaluatorStage

    stage = PermissionEvaluatorStage(_read_only_evaluator(), firer=None)  # type: ignore[arg-type]
    assert stage._tool_is_off_entirely("run_shell_command") is False


def test_the_session_evaluator_carries_the_flag(tmp_path):
    from unittest.mock import MagicMock

    from ember_code.core.session.tool_hook_factory import ToolEventHookFactory

    payload = CliOverrides.from_options(CliOptions(read_only=True)).to_settings_payload()
    factory = ToolEventHookFactory(
        settings=load_settings(cli_overrides=payload),
        rules_index=MagicMock(),
        project_dir=tmp_path,
        hook_executor_ref=MagicMock(),
        session_id_ref=lambda: "s",
    )
    assert factory.permission_evaluator.read_only is True
    assert factory.permission_evaluator.mode is PermissionMode.PLAN
