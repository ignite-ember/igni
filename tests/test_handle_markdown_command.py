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

# The dispatcher reads ``discover_markdown_commands`` via the
# ``command_handler`` reference injected at construction time, so
# the primary patch path is
# ``ember_code.backend.command_handler.discover_markdown_commands``.
# The legacy
# ``ember_code.backend.markdown_command_dispatcher.discover_markdown_commands``
# patch is stacked as defensive belt-and-braces in case a future
# refactor re-introduces a module-level alias here. With the
# constructor-injected design, that path is currently dead — but
# the helper costs nothing and protects against regressions.
_DISCOVERY_PATCH_TARGETS = (
    "ember_code.backend.command_handler.discover_markdown_commands",
    "ember_code.backend.markdown_command_dispatcher.discover_markdown_commands",
)


def _patch_discovery(**kwargs):
    """Stack patch() contexts for every module that re-exports
    ``discover_markdown_commands`` and return a context manager.
    The first mock (the ``command_handler`` path, which is what
    gets injected into the dispatcher) is recorded as
    ``ctx.first_mock`` so callers can assert call args.

    IMPORTANT: callers must build ``CommandHandler`` INSIDE the
    ``with`` block so the patched symbol is what gets captured at
    construction (see :func:`_make_handler`).
    """
    from contextlib import ExitStack

    stack = ExitStack()
    mocks = []
    for target in _DISCOVERY_PATCH_TARGETS:
        mocks.append(stack.enter_context(patch(target, **kwargs)))
    stack.first_mock = mocks[0]
    return stack


def _make_handler(tmp_path: Path) -> CommandHandler:
    """Build a minimal CommandHandler with just enough Session
    state for ``_handle_markdown_command`` to exercise. The
    injected ``discover`` callable captures whatever
    ``command_handler.discover_markdown_commands`` resolves to at
    THIS moment — call this INSIDE a ``_patch_discovery`` block so
    the patched symbol lands on the dispatcher.
    """
    session = MagicMock()
    session.project_dir = tmp_path
    session.settings.rules.cross_tool_support = True
    return CommandHandler(session)


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
        with _patch_discovery(side_effect=RuntimeError("oops")):
            handler = _make_handler(tmp_path)
            result = await handler._handle_markdown_command("/foo", "")
        assert result is None


class TestUnknownName:
    @pytest.mark.asyncio
    async def test_unknown_command_returns_none(self, tmp_path):
        # No commands defined → lookup misses → fall through.
        with _patch_discovery(return_value={}):
            handler = _make_handler(tmp_path)
            result = await handler._handle_markdown_command("/unknown", "")
        assert result is None


class TestSuccessfulRender:
    @pytest.mark.asyncio
    async def test_renders_markdown_command_with_args(self, tmp_path):
        # Happy path — command found, render returns the
        # filled-in prompt, wrapper returns it as a RUN_PROMPT
        # action so the dispatcher feeds it to the agent.
        with _patch_discovery(return_value={"deploy": _mock_md("rendered prompt body")}):
            handler = _make_handler(tmp_path)
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
        md = _mock_md("ok")
        with _patch_discovery(return_value={"deploy": md}):
            handler = _make_handler(tmp_path)
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
        md = _mock_md(side_effect=ValueError("template bad"))
        with _patch_discovery(return_value={"deploy": md}):
            handler = _make_handler(tmp_path)
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
        with _patch_discovery(return_value={}) as ctx:
            handler = _make_handler(tmp_path)
            handler._session.settings.rules.cross_tool_support = True
            await handler._handle_markdown_command("/foo", "")
        ctx.first_mock.assert_called_once_with(tmp_path, read_claude=True)

    @pytest.mark.asyncio
    async def test_skips_claude_when_cross_tool_support_off(self, tmp_path):
        with _patch_discovery(return_value={}) as ctx:
            handler = _make_handler(tmp_path)
            handler._session.settings.rules.cross_tool_support = False
            await handler._handle_markdown_command("/foo", "")
        ctx.first_mock.assert_called_once_with(tmp_path, read_claude=False)


def _mock_md(return_value="rendered prompt body", *, side_effect=None):
    """Build a MarkdownCommand-shaped mock whose ``.render`` is an
    AsyncMock returning ``return_value`` (or raising ``side_effect``)."""
    md = MagicMock()
    if side_effect is not None:
        md.render = AsyncMock(side_effect=side_effect)
    else:
        md.render = AsyncMock(return_value=return_value)
    return md
