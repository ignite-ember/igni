"""Tests for ``CommandHandler._handle_markdown_command`` — the
wrapper that surfaces markdown-authored slash commands to the
dispatcher.

The discovery side (``discover_markdown_commands``) is exhaustively
covered in ``test_markdown_commands.py``. This file pins the
WRAPPER's contract:

  * empty name → None (don't try to look up "")
  * discovery raises → None (swallow + fall through to next tier)
  * unknown name → None (fall through)
  * render raises → CommandResult.error (surface to user)
  * success → CommandResult with action=RUN_PROMPT

The behaviour is small but each branch is the kind of place where
a "neat refactor" can quietly change "fall through" to "surface
error" or vice versa.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ember_code.backend.command_handler import CommandHandler
from ember_code.protocol.messages import CommandAction, CommandResultKind


def _make_handler(tmp_path: Path) -> CommandHandler:
    """Build a minimal CommandHandler with just enough Session
    state for ``_handle_markdown_command`` to exercise."""
    session = MagicMock()
    session.project_dir = tmp_path
    session.settings.rules.cross_tool_support = True
    handler = CommandHandler.__new__(CommandHandler)
    handler._session = session
    return handler


class TestEmptyName:
    @pytest.mark.asyncio
    async def test_empty_name_returns_none(self, tmp_path):
        # ``/`` with no command name — the lstrip leaves "".
        # Don't even try to look it up.
        handler = _make_handler(tmp_path)
        result = await handler._handle_markdown_command("/", "")
        assert result is None

    @pytest.mark.asyncio
    async def test_bare_slash_returns_none(self, tmp_path):
        handler = _make_handler(tmp_path)
        result = await handler._handle_markdown_command("/", "args here")
        assert result is None


class TestDiscoveryFailure:
    @pytest.mark.asyncio
    async def test_discovery_exception_swallowed_and_fall_through(self, tmp_path):
        # If ``discover_markdown_commands`` throws (e.g. a YAML
        # parse error in some other unrelated file), we must
        # fall through to the next dispatcher tier rather than
        # surfacing the failure as a user-facing error. The
        # user typed a slash command; if it ISN'T a markdown
        # command they want the dispatcher to try the next
        # registry, not bail.
        handler = _make_handler(tmp_path)
        with patch(
            "ember_code.core.utils.markdown_commands.discover_markdown_commands",
            side_effect=RuntimeError("oops"),
        ):
            result = await handler._handle_markdown_command("/foo", "")
        assert result is None


class TestUnknownName:
    @pytest.mark.asyncio
    async def test_unknown_command_returns_none(self, tmp_path):
        # No commands defined → lookup misses → fall through.
        handler = _make_handler(tmp_path)
        with patch(
            "ember_code.core.utils.markdown_commands.discover_markdown_commands",
            return_value={},
        ):
            result = await handler._handle_markdown_command("/unknown", "")
        assert result is None


class TestSuccessfulRender:
    @pytest.mark.asyncio
    async def test_renders_markdown_command_with_args(self, tmp_path):
        # Happy path — command found, render returns the
        # filled-in prompt, wrapper returns it as a RUN_PROMPT
        # action so the dispatcher feeds it to the agent.
        handler = _make_handler(tmp_path)
        md = MagicMock()
        md.render = AsyncMock(return_value="rendered prompt body")
        with patch(
            "ember_code.core.utils.markdown_commands.discover_markdown_commands",
            return_value={"deploy": md},
        ):
            result = await handler._handle_markdown_command("/deploy", "to staging")

        assert result is not None
        assert result.kind == CommandResultKind.INFO
        assert result.content == "rendered prompt body"
        assert result.action == CommandAction.RUN_PROMPT

    @pytest.mark.asyncio
    async def test_render_receives_args_and_project_dir(self, tmp_path):
        # Pin the args we pass to render(). The user's text
        # after ``/cmd `` is forwarded as the ``args`` so the
        # markdown body's ``$ARGUMENTS`` substitution works.
        handler = _make_handler(tmp_path)
        md = MagicMock()
        md.render = AsyncMock(return_value="ok")
        with patch(
            "ember_code.core.utils.markdown_commands.discover_markdown_commands",
            return_value={"deploy": md},
        ):
            await handler._handle_markdown_command("/deploy", "to staging")
        md.render.assert_awaited_once_with("to staging", project_dir=tmp_path)


class TestRenderFailure:
    @pytest.mark.asyncio
    async def test_render_exception_surfaces_as_error(self, tmp_path):
        # Render failure is DIFFERENT from discovery failure —
        # the user explicitly invoked this markdown command, so
        # surfacing the error is right (vs. silently falling
        # through, which would make them think the command
        # doesn't exist).
        handler = _make_handler(tmp_path)
        md = MagicMock()
        md.render = AsyncMock(side_effect=ValueError("template bad"))
        with patch(
            "ember_code.core.utils.markdown_commands.discover_markdown_commands",
            return_value={"deploy": md},
        ):
            result = await handler._handle_markdown_command("/deploy", "")

        assert result is not None
        assert result.kind == CommandResultKind.ERROR
        assert "deploy" in result.content
        assert "render failed" in result.content
        assert "template bad" in result.content


class TestCrossToolSupport:
    @pytest.mark.asyncio
    async def test_reads_claude_when_cross_tool_support_on(self, tmp_path):
        # ``cross_tool_support`` flag is forwarded as
        # ``read_claude`` to discovery. Pin the wiring so a
        # rename or default-change is visible.
        handler = _make_handler(tmp_path)
        handler._session.settings.rules.cross_tool_support = True
        with patch(
            "ember_code.core.utils.markdown_commands.discover_markdown_commands",
            return_value={},
        ) as discover:
            await handler._handle_markdown_command("/foo", "")
        discover.assert_called_once_with(tmp_path, read_claude=True)

    @pytest.mark.asyncio
    async def test_skips_claude_when_cross_tool_support_off(self, tmp_path):
        handler = _make_handler(tmp_path)
        handler._session.settings.rules.cross_tool_support = False
        with patch(
            "ember_code.core.utils.markdown_commands.discover_markdown_commands",
            return_value={},
        ) as discover:
            await handler._handle_markdown_command("/foo", "")
        discover.assert_called_once_with(tmp_path, read_claude=False)
