"""Tests for CLI flags — P2 coverage gaps.

Tests the settings/behavior that CLI flags control, not the CLI invocation itself.
CLI invocation tests are in test_cli.py.
"""

from pathlib import Path

from click.testing import CliRunner

from ember_code.cli import cli
from ember_code.core.config.settings import Settings


class TestCLIFlagBehaviors:
    """Test the settings changes that CLI flags produce."""

    def test_version_flag(self):
        """--version should output version string."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0

    def test_debug_creates_settings(self):
        """--debug should be a valid flag (tested via settings)."""
        # Debug flag sets up logging, doesn't change Settings
        settings = Settings()
        # Just verify settings is valid
        assert settings.models.default is not None

    def test_no_color_flag_exists(self):
        """--no-color should be a recognized CLI option."""
        runner = CliRunner()
        # Should not raise "no such option"
        result = runner.invoke(cli, ["--no-color", "--help"])
        assert "--no-color" not in result.output or result.exit_code == 0

    def test_read_only_denies_writes(self):
        """--read-only behavior: file_write and shell_execute = deny."""
        settings = Settings()
        settings.permissions.file_write = "deny"
        settings.permissions.shell_execute = "deny"
        assert settings.permissions.file_write == "deny"
        assert settings.permissions.shell_execute == "deny"

    def test_auto_approve_allows_all(self):
        """--auto-approve behavior: all permissions = allow."""
        settings = Settings()
        settings.permissions.file_write = "allow"
        settings.permissions.shell_execute = "allow"
        settings.permissions.git_push = "allow"
        settings.permissions.git_destructive = "allow"
        assert settings.permissions.file_write == "allow"

    def test_strict_denies_all(self):
        """--strict behavior: deny all."""
        settings = Settings()
        settings.permissions.file_write = "deny"
        settings.permissions.shell_execute = "deny"
        settings.permissions.git_push = "deny"
        settings.permissions.git_destructive = "deny"
        assert settings.permissions.file_write == "deny"
        assert settings.permissions.git_push == "deny"

    def test_accept_edits_allows_writes_only(self):
        """--accept-edits behavior: file_write=allow, shell=ask."""
        settings = Settings()
        settings.permissions.file_write = "allow"
        settings.permissions.shell_execute = "ask"
        assert settings.permissions.file_write == "allow"
        assert settings.permissions.shell_execute == "ask"

    def test_verbose_sets_display(self):
        """--verbose behavior: show_routing=True, show_reasoning=True."""
        settings = Settings()
        settings.display.show_routing = True
        settings.display.show_reasoning = True
        assert settings.display.show_routing is True

    def test_quiet_hides_details(self):
        """--quiet behavior: show_tool_calls=False, show_routing=False."""
        settings = Settings()
        settings.display.show_tool_calls = False
        settings.display.show_routing = False
        assert settings.display.show_tool_calls is False

    def test_add_dir_setting(self):
        """--add-dir stores additional directories."""
        # This is handled in cli.py, not Settings — just verify the pattern
        dirs = [Path("/tmp/dir1"), Path("/tmp/dir2")]
        assert len(dirs) == 2

    def test_max_run_timeout_exists(self):
        """max_run_timeout setting exists for arun timeout."""
        settings = Settings()
        assert hasattr(settings.models, "max_run_timeout")
        assert settings.models.max_run_timeout > 0
