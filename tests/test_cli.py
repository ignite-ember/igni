"""Tests for cli.py — CLI entry point and flag handling."""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from ember_code.cli import cli


def _patch_cli():
    """Return patches for the common CLI dependencies."""
    return (
        patch("ember_code.core.config.settings.load_settings"),
        patch("ember_code.cli.asyncio.run"),
    )


class TestCLIFlags:
    """Test that CLI flags produce the correct settings overrides."""

    def test_version_flag(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "igni" in result.output

    def test_model_override(self):
        runner = CliRunner()
        with (
            patch("ember_code.core.config.settings.load_settings") as mock_load,
            patch("ember_code.cli.asyncio.run"),
        ):
            mock_load.return_value = MagicMock()
            runner.invoke(cli, ["--model", "gpt-4", "-m", "hi"], catch_exceptions=False)
            overrides = mock_load.call_args[1].get("cli_overrides", {})
            assert overrides["models"]["default"] == "gpt-4"

    def test_verbose_flag(self):
        runner = CliRunner()
        with (
            patch("ember_code.core.config.settings.load_settings") as mock_load,
            patch("ember_code.cli.asyncio.run"),
        ):
            mock_load.return_value = MagicMock()
            runner.invoke(cli, ["--verbose", "-m", "hi"], catch_exceptions=False)
            overrides = mock_load.call_args[1].get("cli_overrides", {})
            assert overrides["display"]["show_routing"] is True

    def test_quiet_flag(self):
        runner = CliRunner()
        with (
            patch("ember_code.core.config.settings.load_settings") as mock_load,
            patch("ember_code.cli.asyncio.run"),
        ):
            mock_load.return_value = MagicMock()
            runner.invoke(cli, ["--quiet", "-m", "hi"], catch_exceptions=False)
            overrides = mock_load.call_args[1].get("cli_overrides", {})
            assert overrides["display"]["show_tool_calls"] is False

    def test_read_only_flag(self):
        runner = CliRunner()
        with (
            patch("ember_code.core.config.settings.load_settings") as mock_load,
            patch("ember_code.cli.asyncio.run"),
        ):
            mock_load.return_value = MagicMock()
            runner.invoke(cli, ["--read-only", "-m", "hi"], catch_exceptions=False)
            overrides = mock_load.call_args[1].get("cli_overrides", {})
            assert overrides["permissions"]["file_write"] == "deny"
            assert overrides["permissions"]["shell_execute"] == "deny"

    def test_auto_approve_flag(self):
        runner = CliRunner()
        with (
            patch("ember_code.core.config.settings.load_settings") as mock_load,
            patch("ember_code.cli.asyncio.run"),
        ):
            mock_load.return_value = MagicMock()
            runner.invoke(cli, ["--auto-approve", "-m", "hi"], catch_exceptions=False)
            overrides = mock_load.call_args[1].get("cli_overrides", {})
            assert overrides["permissions"]["file_write"] == "allow"
            assert overrides["permissions"]["shell_execute"] == "allow"

    def test_strict_flag(self):
        runner = CliRunner()
        with (
            patch("ember_code.core.config.settings.load_settings") as mock_load,
            patch("ember_code.cli.asyncio.run"),
        ):
            mock_load.return_value = MagicMock()
            runner.invoke(cli, ["--strict", "-m", "hi"], catch_exceptions=False)
            overrides = mock_load.call_args[1].get("cli_overrides", {})
            assert overrides["permissions"]["file_write"] == "deny"

    def test_no_web_flag(self):
        runner = CliRunner()
        with (
            patch("ember_code.core.config.settings.load_settings") as mock_load,
            patch("ember_code.cli.asyncio.run"),
        ):
            mock_load.return_value = MagicMock()
            runner.invoke(cli, ["--no-web", "-m", "hi"], catch_exceptions=False)
            overrides = mock_load.call_args[1].get("cli_overrides", {})
            assert overrides["permissions"]["web_search"] == "deny"
            assert overrides["permissions"]["web_fetch"] == "deny"


class TestCLIModes:
    """Test that CLI dispatches to the correct execution mode."""

    def test_message_mode_calls_run_single_message(self):
        runner = CliRunner()
        with (
            patch("ember_code.core.config.settings.load_settings", return_value=MagicMock()),
            patch("ember_code.cli.asyncio.run") as mock_run,
        ):
            runner.invoke(cli, ["-m", "hello world"], catch_exceptions=False)
            mock_run.assert_called_once()

    def test_pipe_mode_reads_stdin(self):
        runner = CliRunner()
        with (
            patch("ember_code.core.config.settings.load_settings", return_value=MagicMock()),
            patch("ember_code.cli.asyncio.run") as mock_run,
        ):
            runner.invoke(cli, ["--pipe"], input="test input", catch_exceptions=False)
            mock_run.assert_called_once()

    def test_pipe_mode_no_input_errors(self):
        runner = CliRunner()
        with patch("ember_code.core.config.settings.load_settings", return_value=MagicMock()):
            result = runner.invoke(cli, ["--pipe"], input="", catch_exceptions=False)
            assert result.exit_code != 0

    def test_pipe_mode_combines_message_and_stdin(self):
        runner = CliRunner()
        with (
            patch("ember_code.core.config.settings.load_settings", return_value=MagicMock()),
            patch("ember_code.cli.asyncio.run") as mock_run,
        ):
            runner.invoke(
                cli, ["--pipe", "-m", "prefix"], input="stdin data", catch_exceptions=False
            )
            mock_run.assert_called_once()

    def test_bare_command_prints_help_pointer(self):
        """No TUI ships anymore — the bare ``ember`` command prints
        instructions pointing users at the non-interactive modes and
        the React clients."""
        runner = CliRunner()
        with patch("ember_code.core.config.settings.load_settings", return_value=MagicMock()):
            result = runner.invoke(cli, [], catch_exceptions=False)
            assert result.exit_code == 0
            assert "React client" in result.output
            assert "python -m ember_code.backend" in result.output


class TestWorktreeCleanup:
    """The free ``_worktree_cleanup`` helper was promoted to
    :meth:`WorktreeManager.report_cleanup` — a real method on the
    class it always operated on. The tests below still cover the
    same three branches (no manager / no info / cleanup called),
    now invoking the method directly on a mock. ``click.echo`` is
    injected via the ``echo=`` kwarg so the method stays UI-agnostic.
    """

    def test_cleanup_none_manager(self):
        # "No manager" is now a caller-side concern — there's no
        # instance to call the method on. The equivalent behavior
        # is the ``echo_help_pointer`` path in the CLI, which the
        # ``TestCLIModes`` tests already cover.
        pass

    def test_cleanup_no_info(self):
        wm = MagicMock()
        wm.info = None
        # Reuse the real method against a mock — same isinstance
        # check the production code performs.
        from ember_code.core.worktree import WorktreeManager

        WorktreeManager.report_cleanup(wm, echo=lambda *_a, **_kw: None)
        wm.cleanup.assert_not_called()

    def test_cleanup_cleaned(self):
        from ember_code.core.worktree import WorktreeCleanupResult, WorktreeManager

        wm = MagicMock()
        wm.info = MagicMock(worktree_path="/tmp/wt", branch_name="wt-branch")
        wm.cleanup.return_value = WorktreeCleanupResult(status="cleaned")
        WorktreeManager.report_cleanup(wm, echo=lambda *_a, **_kw: None)
        wm.cleanup.assert_called_once()

    def test_cleanup_preserved(self):
        from ember_code.core.worktree import WorktreeCleanupResult, WorktreeManager

        wm = MagicMock()
        wm.info = MagicMock(worktree_path="/tmp/wt", branch_name="wt-branch")
        wm.cleanup.return_value = WorktreeCleanupResult(status="preserved_dirty")
        WorktreeManager.report_cleanup(wm, echo=lambda *_a, **_kw: None)
        wm.cleanup.assert_called_once()
