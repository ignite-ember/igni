"""The agent could grant itself bypassPermissions, in one tool call.

``slash_command`` refuses a short list of commands. The list was built
around commands that would be *inert* through this path anyway —
``/quit``, ``/clear``, ``/login`` — on the reasoning that silently
no-op'ing confuses the model, so refuse clearly instead.

``/plan``, ``/accept`` and ``/bypass`` are not inert. ``CommandHandler``
dispatches them to ``ModesCommand``, which calls
``Session.set_permission_mode`` on the live ``PermissionEvaluator``, and
the docstring on ``/bypass`` says what that means: "the permission
evaluator skips the HITL prompt for ANY tool". ``/accept`` does it for
every file edit. ``/plan off`` walks back out of the read-only sandbox a
user just put the agent into.

So the model could call ``slash_command("/bypass")``, get back a
cheerful "You are now in **bypassPermissions mode**", and have its next
``rm -rf`` or ``.env`` write auto-approved with no dialog raised. The
approval dialog is the whole mechanism; a tool that lets the approved
party grant its own approval is not a permission system.

``ModesCommand.accept`` already states the rule these tests enforce —
"the agent intentionally does NOT get a tool to flip itself in. Only the
user can opt in via this slash command" — it just was not true of this
tool.

The dispatch-never-happens assertions are the load-bearing ones: a
refusal string that arrives *after* ``CommandHandler.handle`` ran would
leave the mode flipped and read as safe.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ember_code.backend import command_handler as cmd_mod
from ember_code.backend.builtin_command_registry import BuiltinCommandRegistry
from ember_code.core.tools.slash import SlashCommandTool

MODE_COMMANDS = ["/plan", "/accept", "/bypass"]


def _make_session(project_dir: Path):
    session = MagicMock()
    session.project_dir = project_dir
    session.session_id = "abc12345"
    session.skill_pool.match_user_command.return_value = None
    session.skill_pool.list_skills.return_value = []
    return session


class _Tracking:
    """Stands in for ``CommandHandler`` and records that it ran.

    The block has to fire *before* dispatch; anything that records a
    call here means the mode was already flipped by the time the agent
    read its refusal.
    """

    calls: list[str] = []  # noqa: RUF012 — module-scoped recorder, reset per test

    def __init__(self, *_a, **_kw) -> None:
        pass

    async def handle(self, cmd: str):
        type(self).calls.append(cmd)
        raise AssertionError(f"slash_command dispatched {cmd!r} — the block did not fire")


class TestModeCommandsAreRefused:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("cmd", MODE_COMMANDS)
    async def test_refused(self, cmd, tmp_path):
        tool = SlashCommandTool(_make_session(tmp_path))

        result = await tool.slash_command(cmd)

        assert result.startswith("Error:")
        assert cmd in result
        assert "permission" in result.lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cmd", MODE_COMMANDS)
    async def test_never_reaches_the_command_handler(self, cmd, tmp_path, monkeypatch):
        """The one that matters. Before the fix these dispatched, and
        ``ModesCommand`` mutated ``session.permission_evaluator.mode``
        on the way through."""
        _Tracking.calls = []
        monkeypatch.setattr(cmd_mod, "CommandHandler", _Tracking)
        tool = SlashCommandTool(_make_session(tmp_path))

        result = await tool.slash_command(cmd)

        assert _Tracking.calls == []
        assert result.startswith("Error:")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "line",
        ["/bypass on", "/accept enable", "/plan off", "/BYPASS", "bypass on"],
    )
    async def test_the_escalating_arg_forms_are_refused_too(self, line, tmp_path, monkeypatch):
        """``/bypass`` bare only toggles; ``/bypass on`` enters
        unconditionally, and ``/plan off`` is the escape from a sandbox.
        Normalization (missing slash, case) happens before the lookup,
        so those spellings must not be a way around it."""
        _Tracking.calls = []
        monkeypatch.setattr(cmd_mod, "CommandHandler", _Tracking)
        tool = SlashCommandTool(_make_session(tmp_path))

        result = await tool.slash_command(line)

        assert _Tracking.calls == []
        assert result.startswith("Error:")


class TestTheBlockListStillMatchesTheRegistry:
    def test_every_permission_mode_builtin_is_blocked(self):
        """Pins the coupling rather than the list. A fourth mode
        command added to ``cmd_modes`` and wired into the registry
        would otherwise ship reachable, which is how this one got in.
        """
        from ember_code.core.tools.slash import _BLOCKED_MODE_COMMANDS

        registry = BuiltinCommandRegistry()
        mode_builtins = {
            entry.slash
            for entry in registry.entries()
            if entry.module == "ember_code.backend.cmd_modes"
        }

        assert mode_builtins == set(MODE_COMMANDS), mode_builtins
        assert mode_builtins <= _BLOCKED_MODE_COMMANDS

    def test_the_harmless_ones_still_go_through(self, tmp_path):
        """Not a blanket block. ``/compact`` is deliberately reachable
        (a long-running agent deciding to compact mid-turn is the
        feature), so a fix that just refused everything would pass the
        tests above and break the tool."""
        from ember_code.core.tools.slash import _BLOCKED_COMMANDS, _BLOCKED_MODE_COMMANDS

        assert "/compact" not in _BLOCKED_COMMANDS | _BLOCKED_MODE_COMMANDS
        assert "/codeindex" not in _BLOCKED_COMMANDS | _BLOCKED_MODE_COMMANDS
