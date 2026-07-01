"""Tests for the markdown-authored custom commands subsystem.

Covers discovery (multi-root precedence, ``read_claude`` toggle),
frontmatter parsing (description / allowed-tools / argument-hint /
model — including malformed YAML), and the three template-token
substitutions (``$ARGUMENTS``, ``!`cmd```, ``@path``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ember_code.core.utils.markdown_commands import (
    MarkdownCommand,
    _parse_frontmatter,
    discover_markdown_commands,
)


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


# ── Frontmatter parsing ────────────────────────────────────────


class TestParseFrontmatter:
    def test_no_frontmatter_returns_empty_meta(self):
        meta, body = _parse_frontmatter("Just a body, no header.\n")
        assert meta == {}
        assert body == "Just a body, no header.\n"

    def test_simple_frontmatter(self):
        meta, body = _parse_frontmatter(
            "---\ndescription: Quick status\nmodel: sonnet\n---\nBody.\n"
        )
        assert meta == {"description": "Quick status", "model": "sonnet"}
        assert body == "Body.\n"

    def test_malformed_yaml_is_no_op(self):
        """A broken frontmatter block shouldn't sink the whole
        command — fail open and treat the file as bodyless meta."""
        meta, body = _parse_frontmatter("---\n: : not valid:\n  :\n---\nBody survives.\n")
        assert meta == {}
        assert "Body survives" in body

    def test_non_dict_yaml_treated_as_empty(self):
        """A scalar / list at the top level isn't a meta dict."""
        meta, body = _parse_frontmatter("---\njust a string\n---\nBody.\n")
        assert meta == {}
        assert body == "Body.\n"


# ── Discovery ─────────────────────────────────────────────────


class TestDiscoverMarkdownCommands:
    def test_finds_project_ember_command(self, tmp_path, monkeypatch):
        """Most basic case: a single command at
        ``<project>/.ember/commands/review.md``."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()
        _write(
            tmp_path / ".ember" / "commands" / "review.md",
            "---\ndescription: Review the diff\n---\nReview changes.\n",
        )
        cmds = discover_markdown_commands(tmp_path)
        assert "review" in cmds
        assert cmds["review"].description == "Review the diff"
        assert "Review changes." in cmds["review"].body

    def test_finds_user_global_command(self, tmp_path, monkeypatch):
        """A command at ``~/.ember/commands/`` is available in any
        project — covers the global-tier discovery path."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write(home / ".ember" / "commands" / "global.md", "Globally available.\n")
        cmds = discover_markdown_commands(tmp_path)
        assert "global" in cmds

    def test_project_overrides_user_global_on_name_collision(self, tmp_path, monkeypatch):
        """Project commands win — drop-in user globals shouldn't
        silently shadow a project-defined behavior."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write(home / ".ember" / "commands" / "review.md", "GLOBAL\n")
        _write(tmp_path / ".ember" / "commands" / "review.md", "PROJECT\n")
        cmds = discover_markdown_commands(tmp_path)
        assert "PROJECT" in cmds["review"].body
        assert "GLOBAL" not in cmds["review"].body

    def test_ember_dir_beats_claude_dir_at_same_tier(self, tmp_path, monkeypatch):
        """Within the same tier (e.g. both project), ember wins —
        keeps our own namespace authoritative when both exist."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write(tmp_path / ".claude" / "commands" / "review.md", "FROM-CLAUDE\n")
        _write(tmp_path / ".ember" / "commands" / "review.md", "FROM-EMBER\n")
        cmds = discover_markdown_commands(tmp_path)
        assert "FROM-EMBER" in cmds["review"].body

    def test_read_claude_false_skips_claude_dirs(self, tmp_path, monkeypatch):
        """``cross_tool_support=False`` → only ember dirs scanned.
        A user with this toggle off shouldn't get drive-by command
        injection from ``~/.claude/commands/``."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write(home / ".claude" / "commands" / "claude.md", "Should-not-load")
        _write(tmp_path / ".claude" / "commands" / "proj.md", "Should-not-load")
        _write(tmp_path / ".ember" / "commands" / "ember.md", "Loads")
        cmds = discover_markdown_commands(tmp_path, read_claude=False)
        assert "claude" not in cmds
        assert "proj" not in cmds
        assert "ember" in cmds

    def test_dotfiles_ignored(self, tmp_path, monkeypatch):
        """Editor backup files like ``.review.md.swp`` shouldn't
        accidentally register as a command."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write(tmp_path / ".ember" / "commands" / ".backup.md", "Should not load")
        cmds = discover_markdown_commands(tmp_path)
        assert cmds == {}

    def test_allowed_tools_parsed_as_list(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write(
            tmp_path / ".ember" / "commands" / "rev.md",
            "---\nallowed-tools: Bash, Read,Write \n---\nBody\n",
        )
        cmds = discover_markdown_commands(tmp_path)
        assert cmds["rev"].allowed_tools == ("Bash", "Read", "Write")

    def test_allowed_tools_from_yaml_list(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write(
            tmp_path / ".ember" / "commands" / "rev.md",
            "---\nallowed-tools:\n  - Bash\n  - Read\n---\nBody\n",
        )
        cmds = discover_markdown_commands(tmp_path)
        assert cmds["rev"].allowed_tools == ("Bash", "Read")

    def test_argument_hint_and_model_parsed(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write(
            tmp_path / ".ember" / "commands" / "rev.md",
            "---\nargument-hint: <path>\nmodel: opus\n---\nBody\n",
        )
        cmds = discover_markdown_commands(tmp_path)
        assert cmds["rev"].argument_hint == "<path>"
        assert cmds["rev"].model == "opus"

    def test_missing_commands_dir_is_no_op(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        assert discover_markdown_commands(tmp_path) == {}


# ── Token substitution ────────────────────────────────────────


class TestRenderArguments:
    @pytest.mark.asyncio
    async def test_arguments_substituted(self, tmp_path):
        cmd = MarkdownCommand(name="x", path=tmp_path / "x.md", body="Args: $ARGUMENTS done")
        assert await cmd.render("foo bar") == "Args: foo bar done"

    @pytest.mark.asyncio
    async def test_empty_arguments_substituted_empty(self, tmp_path):
        cmd = MarkdownCommand(name="x", path=tmp_path / "x.md", body="X[$ARGUMENTS]Y")
        assert await cmd.render("") == "X[]Y"

    @pytest.mark.asyncio
    async def test_no_arguments_token_is_no_op(self, tmp_path):
        cmd = MarkdownCommand(name="x", path=tmp_path / "x.md", body="Static body.")
        assert await cmd.render("ignored") == "Static body."


class TestRenderShell:
    @pytest.mark.asyncio
    async def test_shell_inline_captures_stdout(self, tmp_path):
        cmd = MarkdownCommand(name="x", path=tmp_path / "x.md", body="Echo: !`echo hello` done")
        out = await cmd.render("", project_dir=tmp_path)
        assert "Echo: hello done" in out

    @pytest.mark.asyncio
    async def test_shell_error_inlines_marker(self, tmp_path):
        cmd = MarkdownCommand(name="x", path=tmp_path / "x.md", body="!`false`")
        out = await cmd.render("", project_dir=tmp_path)
        assert "[error:" in out
        assert "exit 1" in out

    @pytest.mark.asyncio
    async def test_shell_command_not_found(self, tmp_path):
        cmd = MarkdownCommand(
            name="x", path=tmp_path / "x.md", body="!`thiscommandshouldnotexist_xyz`"
        )
        out = await cmd.render("", project_dir=tmp_path)
        assert "[error:" in out

    @pytest.mark.asyncio
    async def test_shell_concurrent_substitutions(self, tmp_path):
        """Multiple !`cmd` tokens fan out concurrently. The output
        is interleaved correctly because we map each match back to
        its captured index."""
        cmd = MarkdownCommand(
            name="x",
            path=tmp_path / "x.md",
            body="A=!`echo first` B=!`echo second`",
        )
        out = await cmd.render("", project_dir=tmp_path)
        assert "A=first" in out
        assert "B=second" in out


class TestRenderFiles:
    @pytest.mark.asyncio
    async def test_at_path_inlines_file_contents(self, tmp_path):
        target = tmp_path / "data.txt"
        target.write_text("DATA-CONTENT")
        cmd = MarkdownCommand(name="x", path=tmp_path / "x.md", body="Read this: @./data.txt")
        out = await cmd.render("", project_dir=tmp_path)
        assert "DATA-CONTENT" in out

    @pytest.mark.asyncio
    async def test_at_path_trailing_punctuation_preserved(self, tmp_path):
        """A user writing ``See @README.md.`` shouldn't break the
        path lookup. We strip trailing ``.,;:`` for the file
        lookup and put it back in the output."""
        target = tmp_path / "README.md"
        target.write_text("README")
        cmd = MarkdownCommand(name="x", path=tmp_path / "x.md", body="See @./README.md.")
        out = await cmd.render("", project_dir=tmp_path)
        assert "README." in out

    @pytest.mark.asyncio
    async def test_missing_path_left_as_literal(self, tmp_path):
        cmd = MarkdownCommand(name="x", path=tmp_path / "x.md", body="See @./does-not-exist.md")
        out = await cmd.render("", project_dir=tmp_path)
        assert "@./does-not-exist.md" in out

    @pytest.mark.asyncio
    async def test_project_command_cannot_escape_project_dir(self, tmp_path):
        """SAFETY: a command file LIVING inside a project must not
        be able to read paths outside it. Defense in depth — the
        same property we enforce for rules-file @ imports."""
        # Create a project + an outside file
        project = tmp_path / "project"
        project.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("OUTSIDE-SECRET")
        cmd_file = project / ".ember" / "commands" / "leak.md"
        cmd_file.parent.mkdir(parents=True)
        cmd_file.write_text(f"Leak: @{outside.resolve()}")
        cmd = MarkdownCommand(name="leak", path=cmd_file, body=cmd_file.read_text())
        out = await cmd.render("", project_dir=project)
        # The literal token is preserved; the file contents are NOT inlined.
        assert "OUTSIDE-SECRET" not in out

    @pytest.mark.asyncio
    async def test_user_command_can_reference_anywhere(self, tmp_path):
        """A user-tier command (living in ``~/.ember/commands``)
        is explicitly authored by the user and CAN reference any
        path — they wrote it themselves. Only PROJECT commands are
        scoped."""
        home = tmp_path / "home"
        user_cmd_dir = home / ".ember" / "commands"
        user_cmd_dir.mkdir(parents=True)
        project = tmp_path / "project"
        project.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("OUTSIDE-CONTENT")
        cmd_file = user_cmd_dir / "ref.md"
        cmd_file.write_text(f"@{outside.resolve()}")
        cmd = MarkdownCommand(name="ref", path=cmd_file, body=cmd_file.read_text())
        out = await cmd.render("", project_dir=project)
        assert "OUTSIDE-CONTENT" in out


class TestCommandHandlerDispatch:
    """End-to-end: ``CommandHandler.handle`` should route an
    unknown built-in command to the markdown-command tier before
    falling through to the skill matcher."""

    @pytest.mark.asyncio
    async def test_command_handler_dispatches_to_markdown_command(self, tmp_path, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        from ember_code.backend.command_handler import CommandHandler
        from ember_code.protocol.messages import CommandAction

        # Wire HOME so user-tier dirs land inside tmp_path.
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()

        _write(
            tmp_path / ".ember" / "commands" / "review.md",
            "---\ndescription: Review\n---\nReview args: $ARGUMENTS\n",
        )

        session = MagicMock()
        session.project_dir = tmp_path
        session.settings.rules.cross_tool_support = True
        # The skill matcher would otherwise short-circuit and
        # return Unknown; force it to never match so we exercise
        # the markdown-command tier in isolation.
        session.skill_pool.match_user_command.return_value = None
        # ``_handle_skill`` calls ``stripped.split()[0]`` for the
        # error message — no need to mock anything else.

        handler = CommandHandler(session)
        result = await handler.handle("/review some thing")
        assert result.action == CommandAction.RUN_PROMPT
        assert "Review args: some thing" in result.content
        # No async-mock junk leaked through.
        assert isinstance(result.content, str)
        # Sanity: the mock wasn't accidentally awaited downstream.
        assert not isinstance(result.content, AsyncMock)

    @pytest.mark.asyncio
    async def test_builtin_command_still_beats_markdown(self, tmp_path, monkeypatch):
        """A markdown file at ``commands/help.md`` must NOT shadow
        the built-in ``/help`` — built-ins ship with the binary
        and a user shouldn't accidentally hijack them with a
        drop-in markdown file."""
        from unittest.mock import MagicMock

        from ember_code.backend.command_handler import CommandHandler

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()
        _write(
            tmp_path / ".ember" / "commands" / "help.md",
            "HIJACK\n",
        )

        session = MagicMock()
        session.project_dir = tmp_path
        session.settings.rules.cross_tool_support = True
        session.skill_pool.match_user_command.return_value = None

        handler = CommandHandler(session)
        result = await handler.handle("/help")
        # Built-in /help renders its own help text — the hijack
        # string must not appear.
        assert "HIJACK" not in (result.content or "")


class TestEndToEndRender:
    @pytest.mark.asyncio
    async def test_all_substitutions_compose(self, tmp_path):
        """All three token types in one body. Confirms the
        evaluation order: ``$ARGUMENTS`` → ``!`cmd``` → ``@path``
        (each substitution happens once, in that order)."""
        target = tmp_path / "ref.txt"
        target.write_text("REFERENCED")
        cmd = MarkdownCommand(
            name="x",
            path=tmp_path / "x.md",
            body="Args: $ARGUMENTS\nShell: !`echo SHELLED`\nFile: @./ref.txt",
        )
        out = await cmd.render("ARGSY", project_dir=tmp_path)
        assert "Args: ARGSY" in out
        assert "Shell: SHELLED" in out
        assert "File: REFERENCED" in out
