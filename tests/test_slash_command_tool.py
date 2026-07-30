"""Tests for the agent-facing ``slash_command`` tool — CC's
``SlashCommand`` parity. Exercises:

- Built-in slash dispatch through the tool returns the command's
  text output to the agent.
- Markdown-authored commands and skills surface their rendered
  prompt body as the tool result (RUN_PROMPT).
- The hard-block list refuses execution with an explanatory
  error (no SystemExit, no session wipe).
- Edge cases: empty input, missing leading slash, command
  handler raising.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.backend import command_handler as cmd_mod
from ember_code.core.tools.slash import SlashCommandTool


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _make_session(project_dir: Path | None = None):
    """Minimal session double for the tool. ``CommandHandler``
    dereferences a handful of fields when running built-ins; the
    rest aren't touched by the commands we exercise here."""
    session = MagicMock()
    session.project_dir = project_dir or Path("/tmp")
    session.settings.rules.cross_tool_support = True
    # ``/help`` needs the keyboard shortcut section + no topic
    # match — those paths are pure attribute reads on
    # CommandHandler itself, not on the session.
    session.skill_pool.match_user_command.return_value = None
    session.skill_pool.list_skills.return_value = []
    # ``/ctx`` reads the agent / model — make sure these exist.
    session.session_id = "abc12345"
    return session


class TestBlockedCommands:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("cmd", ["/quit", "/exit", "/clear", "/login", "/logout", "/model"])
    async def test_blocked_commands_refused(self, cmd, tmp_path):
        """Each entry in the block list returns an explanatory
        error rather than dispatching. The session must NOT have
        been mutated by the tool call."""
        tool = SlashCommandTool(_make_session(tmp_path))
        result = await tool.slash_command(cmd)
        assert "Error" in result
        assert cmd in result
        assert "not invocable" in result.lower() or "require user" in result.lower()

    @pytest.mark.asyncio
    async def test_blocked_case_insensitive(self, tmp_path):
        """``/QUIT`` is the same blocked command as ``/quit`` —
        normalization happens before the lookup."""
        tool = SlashCommandTool(_make_session(tmp_path))
        result = await tool.slash_command("/QUIT")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_blocked_with_args(self, tmp_path):
        """``/model gpt-5`` matches the ``/model`` block prefix —
        we look at the first whitespace-delimited token."""
        tool = SlashCommandTool(_make_session(tmp_path))
        result = await tool.slash_command("/model gpt-5")
        assert "Error" in result
        assert "/model" in result


class TestInputNormalization:
    @pytest.mark.asyncio
    async def test_empty_string_returns_error(self, tmp_path):
        tool = SlashCommandTool(_make_session(tmp_path))
        result = await tool.slash_command("")
        assert "Error" in result and "empty" in result.lower()

    @pytest.mark.asyncio
    async def test_whitespace_only_returns_error(self, tmp_path):
        tool = SlashCommandTool(_make_session(tmp_path))
        result = await tool.slash_command("   ")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_missing_leading_slash_is_inferred(self, tmp_path):
        """The agent might type ``help`` instead of ``/help`` —
        we prepend the slash so the dispatch table still hits."""
        tool = SlashCommandTool(_make_session(tmp_path))
        result = await tool.slash_command("help")
        # /help returns the help markdown — should not be an error.
        assert "Error" not in result


class TestBuiltinDispatch:
    @pytest.mark.asyncio
    async def test_help_with_topic_returns_markdown(self, tmp_path):
        """``/help <topic>`` returns markdown for a known topic.
        Bare ``/help`` triggers the interactive panel and returns
        empty content (handled by the UI, not the tool result),
        so we exercise the topic path which always has text."""
        tool = SlashCommandTool(_make_session(tmp_path))
        result = await tool.slash_command("/help schedule")
        assert "Error" not in result
        assert "Schedule" in result
        # The markdown body should be meaningfully long.
        assert len(result) > 50

    @pytest.mark.asyncio
    async def test_help_with_unknown_topic_returns_error(self, tmp_path):
        """``/help bogus`` is a CommandResult.error — the tool
        surfaces it with the ``Error:`` prefix so the agent
        recognises the failure."""
        tool = SlashCommandTool(_make_session(tmp_path))
        result = await tool.slash_command("/help bogus")
        assert "Error" in result
        assert "Unknown help topic" in result


class TestMarkdownCommandDispatch:
    @pytest.mark.asyncio
    async def test_markdown_command_returns_rendered_prompt(self, tmp_path, monkeypatch):
        """A markdown command with ``$ARGUMENTS`` substitution
        should come back to the agent as the EXPANDED prompt —
        same content the user would have seen if they'd typed
        the command. Lets the agent see and act on the
        expansion."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write(
            tmp_path / ".ember" / "commands" / "ask.md",
            "Q: $ARGUMENTS — proceed?",
        )

        session = _make_session(tmp_path)
        tool = SlashCommandTool(session)
        result = await tool.slash_command("/ask what next")
        assert "Q: what next — proceed?" in result


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_handler_exception_caught(self, tmp_path, monkeypatch):
        """If the underlying CommandHandler raises, the tool
        returns an error string instead of propagating — keeps
        the agent loop alive."""

        class _Boom:
            def __init__(self, *_a, **_kw):
                pass

            async def handle(self, _cmd):
                raise RuntimeError("boom")

        monkeypatch.setattr(cmd_mod, "CommandHandler", _Boom)
        session = _make_session(tmp_path)
        tool = SlashCommandTool(session)
        result = await tool.slash_command("/help")
        assert "Error invoking /help" in result
        assert "boom" in result

    @pytest.mark.asyncio
    async def test_command_result_error_surfaced(self, tmp_path):
        """An unknown slash command produces a CommandResult with
        kind=ERROR. The tool surfaces it as an error string the
        agent can recognise."""
        session = _make_session(tmp_path)
        tool = SlashCommandTool(session)
        result = await tool.slash_command("/doesnotexist")
        # The CommandHandler falls through to skill matching,
        # which returns Unknown command. Our tool re-surfaces it.
        assert "Error" in result
        assert "Unknown" in result or "doesnotexist" in result


class TestNotDestructive:
    """Headline safety property: a blocked command must NOT
    actually fire its side effect. ``/clear`` would otherwise
    wipe the conversation; the agent calling ``slash_command(
    "/clear")`` must not crash or reset the session."""

    @pytest.mark.asyncio
    async def test_clear_does_not_invoke_handler(self, tmp_path, monkeypatch):
        handler_called = False

        class _Tracking:
            def __init__(self, *_a, **_kw):
                pass

            async def handle(self, _cmd):
                nonlocal handler_called
                handler_called = True
                return AsyncMock()

        monkeypatch.setattr(cmd_mod, "CommandHandler", _Tracking)
        session = _make_session(tmp_path)
        tool = SlashCommandTool(session)
        await tool.slash_command("/clear")
        assert handler_called is False  # block fires before dispatch
