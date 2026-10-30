"""A blocked tool call has to tell the model the truth about what is blocked.

Four separate holes in the permission seam, all of which end with the model
being told something false or the tool running when nobody approved it:

* the evaluator's reason was dropped on the tool path, so a plan-mode edit
  denial never mentioned ``exit_plan_mode(plan)`` — the one call that lifts it;
* every ``run_shell_command`` denial claimed "Shell access is off in this
  session", including plan mode, whose own rule is that read-only shell is fine;
* an ``ask`` rule under ``dontAsk`` / ``--strict`` turned a DENY into an
  executed call, because ASK falls through and nothing in that mode prompts;
* the notebook writers were in no guard set, so plan mode did not stop them
  and the protected-paths list did not see them.
"""

from __future__ import annotations

from typing import Any

import pytest

from ember_code.core.config.permission_eval import (
    PermissionDecision,
    PermissionEvaluator,
)
from ember_code.core.hooks.executor import HookExecutor
from ember_code.core.hooks.schemas import HookDefinition, HookResult
from ember_code.core.hooks.tool_hook import ToolEventHook


class _SilentExecutor(HookExecutor):
    """No hooks registered — the pipeline's ``_fire`` calls short-circuit
    and nothing spawns a subprocess."""

    def __init__(self) -> None:
        super().__init__({})

    def get_matching_hooks(self, event: str, target: str = "") -> list[HookDefinition]:
        return []

    async def execute(self, event: str, payload: dict[str, Any], target: str = "") -> HookResult:
        return HookResult(should_continue=True)


def _hook(evaluator: PermissionEvaluator | None = None, **kwargs: Any) -> ToolEventHook:
    return ToolEventHook(
        executor=_SilentExecutor(),
        session_id="s",
        permission_evaluator=evaluator,
        **kwargs,
    )


def _ran(name: str) -> Any:
    """A stand-in tool that records nothing but says it ran, so a test can
    tell "blocked" from "executed" by the returned string alone."""

    def fake(**kwargs: Any) -> str:
        return f"RAN {name}"

    return fake


# ── The evaluator's reason has to reach the model ──────────────────


@pytest.mark.asyncio
async def test_a_plan_mode_edit_denial_names_exit_plan_mode() -> None:
    """The reason is the only place ``exit_plan_mode(plan)`` appears.

    The tool path called ``evaluate`` and threw the reason away, so in the
    TUI and in headless ``-p`` — neither of which has a pause dialog to
    carry it instead — the model got "Blocked: permission policy denied
    'edit_file'." and no stated way to proceed.
    """
    hook = _hook(PermissionEvaluator.from_strings(mode="plan"))

    result = await hook(
        name="edit_file",
        func=_ran("edit_file"),
        args={"file_path": "x.py", "new_str": "1"},
    )

    assert "RAN" not in result
    assert "exit_plan_mode(plan)" in result


@pytest.mark.asyncio
async def test_a_deny_rule_denial_names_the_rule() -> None:
    """Same path, a different reason producer: the model should be able to
    tell "a rule you configured said no" from "the mode said no"."""
    hook = _hook(PermissionEvaluator.from_strings(deny=["run_shell_command(rm *)"]))

    result = await hook(
        name="run_shell_command",
        func=_ran("shell"),
        args={"command": "rm -rf build"},
    )

    assert "RAN" not in result
    assert "run_shell_command(rm *)" in result


# ── "Shell access is off" has to actually be true ──────────────────


@pytest.mark.asyncio
async def test_plan_mode_does_not_claim_the_shell_is_off() -> None:
    """Plan mode denies a *mutating* command and allows read-only ones.
    Telling the model the shell is off costs it every read it still had —
    and directly contradicts the reason printed one sentence earlier.
    """
    hook = _hook(PermissionEvaluator.from_strings(mode="plan"))

    result = await hook(
        name="run_shell_command",
        func=_ran("shell"),
        args={"command": "rm -rf build"},
    )

    assert "RAN" not in result
    assert "Shell access is off" not in result
    assert "Read-only shell calls" in result


@pytest.mark.asyncio
async def test_a_scoped_deny_rule_does_not_claim_the_shell_is_off() -> None:
    """``deny: ["Bash(rm *)"]`` takes away one command, not the shell."""
    hook = _hook(PermissionEvaluator.from_strings(deny=["Bash(rm *)"]))

    result = await hook(
        name="run_shell_command",
        func=_ran("shell"),
        args={"command": "rm -rf build"},
    )

    assert "RAN" not in result
    assert "Shell access is off" not in result


@pytest.mark.asyncio
async def test_a_blanket_shell_denial_still_names_the_alternative() -> None:
    """The case the paragraph was written for — ``--read-only`` / ``--strict``
    emit a bare deny rule for the shell, and igni reads files through the
    shell, so the model's first move is the thing it cannot do."""
    hook = _hook(PermissionEvaluator.from_strings(deny=["run_shell_command"]))

    result = await hook(
        name="run_shell_command",
        func=_ran("shell"),
        args={"command": "ls"},
    )

    assert "Shell access is off" in result
    assert "another of your tools" in result


# ── An ask nobody will answer is a deny, not an approval ───────────


def test_an_ask_rule_under_dont_ask_denies() -> None:
    """``--strict`` selects ``dontAsk``. Marking a tool ``ask`` there is the
    intuitive way to say "be careful with this"; it used to make the run
    *less* safe than writing no rule at all, because an unmatched tool hits
    step 6 and is denied while an ASK falls through and runs."""
    evaluator = PermissionEvaluator.from_strings(
        mode="dontAsk",
        ask=["run_shell_command(git push*)"],
    )

    assert (
        evaluator.evaluate("run_shell_command", {"command": "git push --force origin main"})
        is PermissionDecision.DENY
    )


def test_the_same_ask_rule_still_asks_where_someone_can_answer() -> None:
    """The fix is about the absence of a prompt surface, not about ask rules."""
    evaluator = PermissionEvaluator.from_strings(
        mode="default",
        ask=["run_shell_command(git push*)"],
    )

    assert (
        evaluator.evaluate("run_shell_command", {"command": "git push --force origin main"})
        is PermissionDecision.ASK
    )


@pytest.mark.asyncio
async def test_an_ask_rule_under_dont_ask_does_not_execute_the_tool() -> None:
    """End to end: the decision above has to survive the hook stage, which
    is where ASK was being converted into "run it"."""
    hook = _hook(
        PermissionEvaluator.from_strings(
            mode="dontAsk",
            ask=["run_shell_command(git push*)"],
        )
    )

    result = await hook(
        name="run_shell_command",
        func=_ran("shell"),
        args={"command": "git push --force origin main"},
    )

    assert "RAN" not in result
    assert "Blocked" in result


# ── Plan mode's banner has to hold for the notebook toolkit ────────


@pytest.mark.parametrize(
    "function_name",
    ["notebook_edit_cell", "notebook_add_cell", "notebook_remove_cell"],
)
def test_plan_mode_denies_notebook_cell_writes(function_name: str) -> None:
    """``NotebookEdit`` was in the edit set, but the evaluator is handed the
    Agno function name and never the catalog name, so all three writers fell
    through to DEFER — in a mode whose banner says it does not edit files.
    """
    evaluator = PermissionEvaluator.from_strings(mode="plan")

    assert (
        evaluator.evaluate(function_name, {"file_path": "analysis.ipynb"})
        is PermissionDecision.DENY
    )


@pytest.mark.asyncio
async def test_the_protected_paths_list_sees_notebook_writes() -> None:
    """Defense in depth is supposed to be the layer that holds when the mode
    machinery does not; it was missing the same three functions."""
    hook = _hook(protected_paths=["secrets.*"])

    result = await hook(
        name="notebook_edit_cell",
        func=_ran("notebook"),
        args={"file_path": "secrets.ipynb", "cell_index": 0, "new_source": "x"},
    )

    assert "RAN" not in result
    assert "protected path" in result.lower()
