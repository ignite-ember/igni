"""Argument text must never become a template token.

``MarkdownCommand.render`` used to substitute ``$ARGUMENTS`` *before*
scanning for ``!`cmd``` and ``@path``, so the argument string was
re-read as template syntax. Via the always-attached ``slash_command``
tool that argument is model-supplied and reaches ``/bin/sh -c`` with no
permission gate in front of it (SWP-35 / SWP-36 / SWP-37).

Every assertion below fails against the old ordering: the sentinel file
exists / the shell metacharacter is executed rather than carried.
"""

import pytest

from ember_code.core.utils.markdown_commands import MarkdownCommand


class TestArgumentsAreNotRescanned:
    @pytest.mark.asyncio
    async def test_shell_token_in_arguments_is_not_executed(self, tmp_path):
        marker = tmp_path / "pwned.txt"
        cmd = MarkdownCommand(name="x", path=tmp_path / "x.md", body="Review: $ARGUMENTS")
        out = await cmd.render(f"auth.py !`touch {marker}`", project_dir=tmp_path)

        assert not marker.exists(), "argument text was executed as a shell token"
        # The token survives verbatim into the prompt — the agent sees
        # what it asked for, the shell never does.
        assert f"!`touch {marker}`" in out

    @pytest.mark.asyncio
    async def test_at_path_in_arguments_is_not_inlined(self, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("SECRET-MATERIAL")
        cmd = MarkdownCommand(name="x", path=tmp_path / "x.md", body="Review: $ARGUMENTS")
        out = await cmd.render(f"@{secret}", project_dir=tmp_path)

        assert "SECRET-MATERIAL" not in out
        assert str(secret) in out

    @pytest.mark.asyncio
    async def test_arguments_inside_a_shell_token_are_quoted(self, tmp_path):
        """The body's own ``!`cmd``` may still interpolate ``$ARGUMENTS``,
        but as exactly one shell word."""
        marker = tmp_path / "pwned2.txt"
        cmd = MarkdownCommand(name="x", path=tmp_path / "x.md", body="Out: !`echo $ARGUMENTS`")
        out = await cmd.render(f"hi; touch {marker}", project_dir=tmp_path)

        assert not marker.exists(), "argument text escaped its shell word"
        assert f"Out: hi; touch {marker}" in out

    @pytest.mark.asyncio
    async def test_body_tokens_still_expand(self, tmp_path):
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
