"""The first thing a new installation prints must be runnable.

Bare `igni` prints a short pointer, because there is no terminal chat
UI any more. For three releases that pointer told people to type
`ember`:

    • ember -m "<message>"        one-shot message
    • ember -p                       pipe mode (stdin → stdout)
    • ember -c -m "<message>"     resume last session, one-shot

`ember` has never been a console script. `pyproject.toml` installs
`igni` and `ignite-ember` and nothing else, so every one of those lines
was a command-not-found — and it was the *first* thing anybody saw
after installing. Found by installing the published wheel from PyPI
into a clean environment and running it, which is the only way to see
what a new user sees.

So the rule is that every command the message names has to be a command
that exists: either a console script `pyproject.toml` declares, or a
`python -m` module invocation. Checked against `pyproject.toml` rather
than a hardcoded list, so adding or renaming an entry point cannot
leave this behind.
"""

from __future__ import annotations

import pathlib
import re
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - the project pins 3.12
    import tomli as tomllib

import pytest

from ember_code.cli.invocation import _HELP_TEXT, COMMAND

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_PYPROJECT = _ROOT / "pyproject.toml"

#: A bullet line's first word — the command a reader would type.
_BULLET = re.compile(r"^\s*•\s+(\S+)", re.MULTILINE)


def _console_scripts() -> set[str]:
    data = tomllib.loads(_PYPROJECT.read_text())
    return set(data.get("project", {}).get("scripts", {}))


class TestTheCheckMeasuresSomething:
    """A parser returning nothing would make the rule below vacuous."""

    def test_the_help_text_has_bullets(self) -> None:
        assert _BULLET.findall(_HELP_TEXT), _HELP_TEXT

    def test_pyproject_declares_scripts(self) -> None:
        scripts = _console_scripts()

        assert scripts, "no [project.scripts] found — the rule has nothing to check against"
        assert "igni" in scripts, scripts


class TestEveryCommandItNamesExists:
    @pytest.mark.parametrize("word", sorted(set(_BULLET.findall(_HELP_TEXT))))
    def test_the_bullet_names_something_runnable(self, word: str) -> None:
        """Either a declared console script, or `python -m <module>`."""
        runnable = _console_scripts() | {"python", "python3"}

        assert word in runnable, (
            f"the first-run message tells people to type {word!r}, which is not a console "
            f"script in pyproject.toml ({sorted(_console_scripts())}) and is not a `python -m` "
            f"invocation. That is a command-not-found as somebody's first experience of igni."
        )

    def test_it_recommends_the_canonical_script(self) -> None:
        """`ignite-ember` is kept for backwards compatibility. New
        installations should be pointed at `igni`."""
        assert COMMAND == "igni"
        assert COMMAND in _console_scripts()

    def test_it_does_not_say_ember(self) -> None:
        """The exact regression. `ember` reads like a command because it
        used to be the product's name; it has never been a binary."""
        named = set(_BULLET.findall(_HELP_TEXT))

        assert "ember" not in named, named
