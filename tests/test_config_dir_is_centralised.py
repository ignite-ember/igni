"""The configuration directory is named in one place.

It used to be the bare string ``".ember"`` in 71 sites across 110 files,
which made "the configuration directory" impossible to grep for as a
concept — the same characters appear in prose, in ``ember.md``, and
inside longer paths. Renaming it meant finding every one and deciding,
per site, whether it was the directory or a sentence about it.

These tests keep it that way. Without them the literals come back one
convenient edit at a time, and the next person to attempt the rename
starts over.

Prose is deliberately exempt: a docstring reading
``~/{CONFIG_DIR}/config.yaml`` helps nobody, so comments and docstrings
still spell the name out and are rewritten when the value changes.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from ember_code.core.paths import (
    CONFIG_DIR,
    DEFAULT_DATA_DIR,
    LEGACY_CONFIG_DIR,
    home_config_dir,
    project_config_dir,
)

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "ember_code"

#: The spellings that mean "the configuration directory" and that a
#: constant should have replaced.
#:
#: Built from the constants rather than written out, so this survives the
#: next rename — a guard against hardcoding that hardcodes the name it is
#: guarding goes quietly vacuous the moment the value changes, which is
#: exactly what happened to it during the move to ``.igni``.
#:
#: Both names are checked. The new one is the live sin; the old one still
#: matters because a literal ``".ember"`` left in code is now not just
#: uncentralised but *wrong*, and reads as a path nothing writes to.
_NAMES = (CONFIG_DIR, LEGACY_CONFIG_DIR)
_BARE = re.compile(r"(?<![\w.])\"(?:" + "|".join(re.escape(n) for n in _NAMES) + r")\"(?![\w])")
_HOME = re.compile(r"\"~/(?:" + "|".join(re.escape(n) for n in _NAMES) + r")\"")

#: Where the name is allowed to appear as a literal.
_ALLOWED = {
    # Defines it.
    "core/paths.py",
    # The body of a shell script written into a project, not Python —
    # substituting a constant into it would corrupt the script.
    "core/init_templates.py",
}


def _python_files() -> list[pathlib.Path]:
    return sorted(SRC.rglob("*.py"))


def _string_literals(path: pathlib.Path) -> list[str]:
    """Every string constant in the file, docstrings excluded.

    Uses ``ast`` rather than a line regex so a mention inside a comment
    or a docstring — which is prose, and exempt — cannot fail the test.
    """
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:  # pragma: no cover — a broken file fails elsewhere
        return []

    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))

    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


class TestNothingSpellsItOutInCode:
    def test_no_bare_literal_survives(self):
        offenders = []
        for path in _python_files():
            rel = path.relative_to(SRC).as_posix()
            if rel in _ALLOWED:
                continue
            for literal in _string_literals(path):
                if literal in _NAMES or literal in {f"~/{n}" for n in _NAMES}:
                    offenders.append(rel)
                    break
        assert not offenders, (
            "these spell the config directory out instead of importing CONFIG_DIR "
            f"or DEFAULT_DATA_DIR from ember_code.core.paths: {sorted(set(offenders))}"
        )

    @pytest.mark.parametrize("pattern", [_BARE, _HOME])
    def test_the_source_text_agrees(self, pattern):
        """A second opinion that does not rely on the ast walk above."""
        offenders = [
            path.relative_to(SRC).as_posix()
            for path in _python_files()
            if path.relative_to(SRC).as_posix() not in _ALLOWED and pattern.search(path.read_text())
        ]
        # Prose can match, so this only reports files where the literal
        # is not inside a comment.
        real = []
        for rel in offenders:
            text = (SRC / rel).read_text()
            for line in text.split("\n"):
                stripped = line.strip()
                if pattern.search(line) and not stripped.startswith(("#", "*", '"""', "'''")):
                    real.append(rel)
                    break
        assert not real, f"literal outside a comment in: {sorted(set(real))}"


class TestTheConstantsBehave:
    def test_the_home_default_is_not_a_path(self):
        """``Path("~/...")`` does not expand itself, and 37 signatures
        take this as a ``str`` — typing it as a Path would silently
        create a directory literally named ``~``."""
        assert isinstance(DEFAULT_DATA_DIR, str)
        assert DEFAULT_DATA_DIR.startswith("~/")

    def test_the_two_constants_agree(self):
        assert f"~/{CONFIG_DIR}" == DEFAULT_DATA_DIR

    def test_the_helpers_use_the_constant(self, tmp_path: pathlib.Path, monkeypatch):
        assert project_config_dir(tmp_path).name == CONFIG_DIR
        # Against a fresh home, because ``home_config_dir`` deliberately
        # returns the *legacy* name when only that exists — and the
        # developer running this very likely has one, which made this
        # assertion depend on whose machine it ran on.
        monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: tmp_path))
        assert home_config_dir().name == CONFIG_DIR

    def test_project_config_dir_does_not_expand(self, tmp_path: pathlib.Path):
        """Callers join onto it; expanding here would surprise them."""
        assert project_config_dir(tmp_path).parent == tmp_path

    def test_paths_imports_nothing_from_ember(self):
        """It is the shallowest module in ``core`` on purpose — anything
        may import it, so it must not import back and make a cycle."""
        source = (SRC / "core" / "paths.py").read_text()
        tree = ast.parse(source)
        imported = [
            node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        ] + [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ]
        assert not [m for m in imported if m.startswith("ember_code")], imported
