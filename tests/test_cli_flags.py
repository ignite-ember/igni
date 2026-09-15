"""Tests for CLI flags — P2 coverage gaps.

Tests the settings/behavior that CLI flags control, not the CLI invocation itself.
CLI invocation tests are in test_cli.py.
"""

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

    # Everything that used to sit between here and
    # ``test_max_run_timeout_exists`` has moved to
    # ``tests/test_the_cli_flags_do_something.py``, because none of it
    # ran the CLI.
    #
    # The shape, verbatim from the version this replaces::
    #
    #     def test_read_only_denies_writes(self):
    #         settings = Settings()
    #         settings.permissions.file_write = "deny"
    #         settings.permissions.shell_execute = "deny"
    #         assert settings.permissions.file_write == "deny"
    #
    # It sets two fields and asserts one of them back, and would pass
    # with ``--read-only`` deleted from the CLI. ``test_add_dir_setting``
    # asserted ``len([Path("/tmp/dir1"), Path("/tmp/dir2")]) == 2``.
    # ``test_no_color_flag_exists`` asserted
    # ``"--no-color" not in result.output or result.exit_code == 0``,
    # which is satisfied by either half.
    #
    # Nine tests, one flag name each, and no coverage of any of them.
    # That is worse than a gap: a gap gets filled, and a row of green
    # ticks does not.

    def test_max_run_timeout_exists(self):
        """max_run_timeout setting exists for arun timeout."""
        settings = Settings()
        assert hasattr(settings.models, "max_run_timeout")
        assert settings.models.max_run_timeout > 0
