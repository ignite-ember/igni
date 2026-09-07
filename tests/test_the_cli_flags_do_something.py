"""What each ``igni`` flag actually does, asserted through the real CLI.

``tests/test_cli_flags.py`` had a test per flag and none of them ran
one. The shape was::

    def test_read_only_denies_writes(self):
        settings = Settings()
        settings.permissions.file_write = "deny"
        settings.permissions.shell_execute = "deny"
        assert settings.permissions.file_write == "deny"

That sets two fields and asserts one of them back. It would pass with
``--read-only`` deleted from the CLI, misspelled, or wired to the
opposite value. ``test_add_dir_setting`` asserted ``len([a, b]) == 2``.
``test_cli_permission_wiring.py`` is honest about doing the same —
"we replicate the flag-handling block from cli.py directly" — which
means the mapping from argv to settings, the part that can actually
be wrong, had nothing on it.

Two layers here, both going through the product's own code:

1. **argv → CliOptions.** Click parses; the callback does
   ``CliOptions.model_validate(ctx.params)``. Captured by wrapping
   ``load_settings_from_options``, which the callback calls with the
   bundle it built.
2. **CliOptions → Settings.** ``load_settings_from_options`` is the
   real mapping function. Called directly, asserting the values a
   permission evaluator will later read.

Four flags had no mention in any test at all — ``--continue``,
``--message``, ``--session-id``, ``--worktree`` — and the last of
those creates a git worktree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import ember_code.cli as cli_module
from ember_code.cli.options import CliOptions


@pytest.fixture
def parsed(monkeypatch: pytest.MonkeyPatch):
    """Run the real CLI and hand back the ``CliOptions`` it built.

    Wrapping ``load_settings_from_options`` rather than reading
    ``ctx.obj`` afterwards: the callback tears the context down on its
    way out, and this way the object under test is the one the
    product's own code passed on.
    """
    seen: dict[str, CliOptions] = {}
    original = cli_module.load_settings_from_options

    def _spy(options: CliOptions):
        seen["options"] = options
        return original(options)

    monkeypatch.setattr(cli_module, "load_settings_from_options", _spy)

    def run(*argv: str) -> CliOptions:
        """Parse ``argv`` and return the bundle, exit code aside.

        The exit code is deliberately not asserted. Parsing happens on
        the callback's first line and several flags legitimately fail
        *after* it — ``--worktree`` outside a git repository, ``--pipe``
        with nothing on stdin. Both are correct refusals with their own
        tests below; requiring a clean exit here would turn this file
        into a test of what happens next instead of a test of the
        parse.
        """
        seen.pop("options", None)
        CliRunner().invoke(cli_module.cli, list(argv))
        assert "options" in seen, (
            f"the CLI never built a CliOptions for {argv} — it failed before "
            "the callback ran, which means the option was not accepted at all"
        )
        return seen["options"]

    return run


class TestClickParsesEachFlag:
    """Every option reaches the field the rest of the CLI reads."""

    @pytest.mark.parametrize(
        "argv,field",
        [
            (["--verbose"], "verbose"),
            (["--quiet"], "quiet"),
            (["--read-only"], "read_only"),
            (["--accept-edits"], "accept_edits"),
            (["--auto-approve"], "auto_approve"),
            (["--no-web"], "no_web"),
            (["--strict"], "strict"),
            (["--no-color"], "no_color"),
            (["--debug"], "debug"),
            (["--worktree"], "worktree"),
            (["--continue"], "continue_session"),
        ],
    )
    def test_the_flag_sets_its_field(self, parsed, argv: list[str], field: str):
        assert getattr(parsed(*argv), field) is True

    @pytest.mark.parametrize(
        "field",
        [
            "verbose",
            "quiet",
            "read_only",
            "accept_edits",
            "auto_approve",
            "no_web",
            "strict",
            "no_color",
            "debug",
            "worktree",
            "continue_session",
        ],
    )
    def test_it_is_off_without_the_flag(self, parsed, field: str):
        """The other half. A field hardcoded to ``True`` passes every
        test above."""
        assert getattr(parsed(), field) is False

    def test_model_takes_its_value(self, parsed):
        assert parsed("--model", "MiniMax-M2.7").model == "MiniMax-M2.7"

    def test_session_id_takes_its_value(self, parsed):
        assert parsed("--session-id", "abc12345").session_id == "abc12345"

    def test_add_dir_accumulates(self, parsed, tmp_path):
        """``multiple=True``. The old test for this asserted
        ``len([Path("/tmp/dir1"), Path("/tmp/dir2")]) == 2``."""
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()

        options = parsed("--add-dir", str(a), "--add-dir", str(b))

        assert set(options.add_dir) == {str(a), str(b)}

    def test_add_dir_rejects_a_directory_that_is_not_there(self, tmp_path):
        """``type=click.Path(exists=True, file_okay=False)`` — a typo in
        a path must fail at the boundary, not halfway through a session."""
        result = CliRunner().invoke(cli_module.cli, ["--add-dir", str(tmp_path / "nope")])

        assert result.exit_code != 0
        assert "does not exist" in result.output.lower()

    @pytest.mark.parametrize(
        "short,long,field",
        [
            ("-c", "--continue", "continue_session"),
            ("-p", "--pipe", "pipe"),
            ("-m", "--message", "message"),
        ],
    )
    def test_the_short_form_means_the_same_thing(self, parsed, short: str, long: str, field: str):
        """Documented aliases. ``-c``, ``-p`` and ``-m`` appear in the
        help text; a mismatch between one and its long form is
        invisible until somebody types the short one."""
        args = ["hi"] if field == "message" else []

        assert getattr(parsed(short, *args), field) == getattr(parsed(long, *args), field)


class TestFlagsThatRefuse:
    """Two flags fail on purpose, and the message is the feature."""

    def test_worktree_outside_a_git_repository_says_so(self, tmp_path):
        """``--worktree`` runs the session in an isolated git worktree.
        Outside a repository there is nothing to branch from, and a
        silent fallback to the current directory would be the worst
        possible answer — the flag exists to keep the agent away from
        your working tree."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(cli_module.cli, ["--worktree"])

        assert result.exit_code != 0
        assert "not a git repository" in str(result.exception).lower()

    def test_pipe_with_no_input_says_where_input_should_come_from(self):
        """Pipe mode reads stdin. Told nothing, it must name both ways
        of giving it something rather than hanging or exiting mute."""
        result = CliRunner().invoke(cli_module.cli, ["--pipe"], input="")

        assert result.exit_code != 0
        assert "stdin" in result.output
        assert "-m" in result.output


class TestFlagsBecomeSettings:
    """The mapping the old tests hand-wrote instead of calling."""

    @staticmethod
    def _settings(**kwargs: Any):
        return cli_module.load_settings_from_options(CliOptions(**kwargs))

    def test_read_only_denies_writes_and_shell(self):
        settings = self._settings(read_only=True)

        assert settings.permissions.file_write == "deny"
        assert settings.permissions.shell_execute == "deny"

    def test_auto_approve_allows_them(self):
        settings = self._settings(auto_approve=True)

        assert settings.permissions.file_write == "allow"
        assert settings.permissions.shell_execute == "allow"

    def test_accept_edits_allows_writes_and_still_asks_about_shell(self):
        """The whole point of the mode: edits stop interrupting you,
        shell does not. If both went to ``allow`` the flag would be
        ``--auto-approve`` under another name."""
        settings = self._settings(accept_edits=True)

        assert settings.permissions.file_write == "allow"
        assert settings.permissions.shell_execute != "allow"

    def test_the_default_asks(self):
        """The baseline every assertion above is relative to. Without
        it, a build that hardcoded ``deny`` everywhere would satisfy
        the read-only test."""
        settings = self._settings()

        assert settings.permissions.file_write == "ask"
        assert settings.permissions.shell_execute == "ask"

    @pytest.mark.parametrize(
        "flag,mode",
        [
            ("read_only", "plan"),
            ("accept_edits", "acceptEdits"),
            ("auto_approve", "bypassPermissions"),
        ],
    )
    def test_the_permission_mode_matches_the_flag(self, flag: str, mode: str):
        assert self._settings(**{flag: True}).permissions.mode == mode

    def test_verbose_turns_the_detail_on(self):
        settings = self._settings(verbose=True)

        assert settings.display.show_routing is True
        assert settings.display.show_reasoning is True

    def test_quiet_turns_it_off(self):
        settings = self._settings(quiet=True)

        assert settings.display.show_tool_calls is False
        assert settings.display.show_routing is False

    def test_no_web_denies_both_web_permissions(self):
        settings = self._settings(no_web=True)

        assert settings.permissions.web_search == "deny"
        assert settings.permissions.web_fetch == "deny"

    def test_model_overrides_the_configured_default(self):
        assert self._settings(model="some-other-model").models.default == ("some-other-model")


class TestEveryOptionIsCovered:
    """A rule, so the next flag added does not arrive untested.

    Four of them had no mention in any test file when this was
    written — ``--continue``, ``--message``, ``--session-id``,
    ``--worktree``. The last creates a git worktree and moves the
    session into it.
    """

    @staticmethod
    def _declared() -> set[str]:
        """Every long option on the ``igni`` command, read from source.

        Click exposes the parsed options at runtime too, but reading
        the decorators keeps this honest about aliases: ``-c`` and
        ``--continue`` are one option, and the long form is the name
        anyone greps for.
        """
        import re
        from pathlib import Path

        source = (
            Path(__file__).resolve().parent.parent / "src/ember_code/cli/__init__.py"
        ).read_text()
        return set(re.findall(r'@click\.option\([^)]*?"(--[a-z0-9-]+)"', source, re.S))

    @staticmethod
    def _named_in_tests() -> str:
        from pathlib import Path

        tests = Path(__file__).resolve().parent
        return "\n".join(
            p.read_text() for p in tests.rglob("test_*.py") if p.name != Path(__file__).name
        )

    def test_the_scan_found_options(self):
        """A regex that matches nothing passes the rule below for every
        flag at once."""
        assert len(self._declared()) >= 14

    def test_every_option_is_named_by_some_test(self):
        declared = self._declared()
        # This file names them all; the check is about the suite as a
        # whole, so it reads every other test file plus this one.
        corpus = self._named_in_tests() + Path(__file__).read_text()
        missing = sorted(flag for flag in declared if flag not in corpus)

        assert missing == [], (
            f"{missing} are options on the `igni` command that no test mentions. "
            "A flag nobody runs is a flag nobody knows is wired."
        )
