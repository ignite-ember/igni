"""Tests for cli.py — CLI entry point and flag handling."""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from ember_code.cli import _worktree_cleanup, cli


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

    def test_no_tui_disabled(self):
        runner = CliRunner()
        with patch("ember_code.core.config.settings.load_settings", return_value=MagicMock()):
            result = runner.invoke(cli, ["--no-tui"])
            assert result.exit_code == 1
            assert "temporarily disabled" in result.output

    def test_default_launches_tui(self):
        runner = CliRunner()
        with (
            patch("ember_code.core.config.settings.load_settings", return_value=MagicMock()),
            patch("ember_code.cli._run_app") as mock_app,
        ):
            runner.invoke(cli, [], catch_exceptions=False)
            mock_app.assert_called_once()


class TestWorktreeCleanup:
    def test_cleanup_none_manager(self):
        _worktree_cleanup(None)  # should not raise

    def test_cleanup_no_info(self):
        wm = MagicMock()
        wm.info = None
        _worktree_cleanup(wm)
        wm.cleanup.assert_not_called()

    def test_cleanup_cleaned(self):
        wm = MagicMock()
        wm.info = MagicMock(worktree_path="/tmp/wt", branch_name="wt-branch")
        wm.cleanup.return_value = True
        _worktree_cleanup(wm)
        wm.cleanup.assert_called_once()

    def test_cleanup_preserved(self):
        wm = MagicMock()
        wm.info = MagicMock(worktree_path="/tmp/wt", branch_name="wt-branch")
        wm.cleanup.return_value = False
        _worktree_cleanup(wm)
        wm.cleanup.assert_called_once()
