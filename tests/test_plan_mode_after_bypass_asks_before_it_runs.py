"""Plan mode is read-only whatever the session started in.

A session started in ``bypassPermissions`` (``--auto-approve``, or a
user config) builds its shell tool without an approval pause: Agno never
stops to ask. Switching to ``/plan`` changes the evaluator, not the
tool, so plan mode's ASK for ``git push`` reached the tool hook as if
someone had answered it — and the push ran.

Now an ASK for a tool that was built without a pause is put to the
person through the session's in-run approval channel, and refused when
nobody can answer.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ember_code.backend.command_handler import CommandHandler
from ember_code.core.config.permission_eval import PermissionEvaluator, PermissionMode
from ember_code.core.config.settings import Settings
from ember_code.core.config.tool_permissions import ToolPermissions
from ember_code.core.hooks.executor import HookExecutor
from ember_code.core.hooks.schemas import HookDefinition, HookResult
from ember_code.core.hooks.tool_hook import ToolEventHook
from ember_code.core.session.broadcast import BroadcastBus
from ember_code.core.session.core import Session
from ember_code.core.tools.registry import ToolRegistry


class _SilentExecutor(HookExecutor):
    def __init__(self) -> None:
        super().__init__({})

    def get_matching_hooks(self, event: str, target: str = "") -> list[HookDefinition]:
        return []

    async def execute(self, event: str, payload: dict[str, Any], target: str = "") -> HookResult:
        return HookResult(should_continue=True)


def _started_in(mode: str, tmp_path: Path):
    """A session started in ``mode``, with the agent's shell tool built
    the way the registry builds it for that mode."""
    settings = Settings()
    settings.permissions.mode = mode
    registry = ToolRegistry(
        base_dir=tmp_path,
        permissions=ToolPermissions(
            project_dir=tmp_path, settings_permissions=settings.permissions
        ),
    )
    shell = registry.resolve(["Bash"])[0]
    agent = SimpleNamespace(name="main", tools=[shell])

    session = Session.__new__(Session)
    session.settings = settings
    session.broadcast_bus = BroadcastBus()
    session.permission_evaluator = PermissionEvaluator.from_strings(mode=mode)
    return session, agent


def _hook(session: Session, ask_person=None) -> ToolEventHook:
    return ToolEventHook(
        executor=_SilentExecutor(),
        session_id="s",
        permission_evaluator=session.permission_evaluator,
        ask_person=ask_person,
    )


def _push_tool(ran: list[str]):
    def run_shell_command(**kwargs: Any) -> str:
        ran.append(kwargs["command"])
        return "pushed"

    return run_shell_command


@pytest.mark.asyncio
async def test_git_push_does_not_run_in_plan_mode_after_a_bypass_start(tmp_path):
    session, agent = _started_in("bypassPermissions", tmp_path)
    await CommandHandler(session).handle("/plan on")
    assert session.permission_evaluator.mode is PermissionMode.PLAN

    ran: list[str] = []
    result = await _hook(session)(
        name="run_shell_command",
        func=_push_tool(ran),
        args={"command": "git push origin main"},
        agent=agent,
    )

    assert ran == []
    assert result.startswith("Blocked")
    assert "exit_plan_mode" in result


@pytest.mark.asyncio
async def test_the_person_is_asked_when_someone_can_answer(tmp_path):
    session, agent = _started_in("bypassPermissions", tmp_path)
    await CommandHandler(session).handle("/plan on")

    asked: list[str] = []

    async def ask(tool_name, args, reason, agent_path):
        asked.append(args["command"])
        return answer

    ran: list[str] = []
    answer = False
    result = await _hook(session, ask)(
        name="run_shell_command", func=_push_tool(ran), args={"command": "git push"}, agent=agent
    )
    assert asked == ["git push"]
    assert ran == []
    assert "declined" in result

    answer = True
    await _hook(session, ask)(
        name="run_shell_command", func=_push_tool(ran), args={"command": "git push"}, agent=agent
    )
    assert ran == ["git push"]


@pytest.mark.asyncio
async def test_a_read_only_command_still_runs(tmp_path):
    session, agent = _started_in("bypassPermissions", tmp_path)
    await CommandHandler(session).handle("/plan on")
    ran: list[str] = []
    await _hook(session)(
        name="run_shell_command", func=_push_tool(ran), args={"command": "git status"}, agent=agent
    )
    assert ran == ["git status"]


@pytest.mark.asyncio
async def test_a_call_that_paused_is_taken_as_answered(tmp_path):
    """Built with a pause, the call only reaches the hook once someone
    confirmed it — so the ASK is not asked a second time."""
    session, agent = _started_in("default", tmp_path)
    await CommandHandler(session).handle("/plan on")

    async def ask(*_):  # pragma: no cover — must not be called
        raise AssertionError("asked twice")

    ran: list[str] = []
    await _hook(session, ask)(
        name="run_shell_command", func=_push_tool(ran), args={"command": "git push"}, agent=agent
    )
    assert ran == ["git push"]
