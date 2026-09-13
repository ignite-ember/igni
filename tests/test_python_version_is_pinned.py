"""One Python version, declared in one story across four files.

The desktop app installs 3.12 and only 3.12
(``clients/tauri/src-tauri/src/runtime.rs``), so that is the only
interpreter the product ever runs on. Everything else that names a
version is describing the same fact and can drift away from it
silently: ``requires-python`` let a contributor's ``uv sync`` pick 3.14,
mypy type-checked against 3.11, and CI asserted support for 3.11 and
3.13 by running the suite there.

The drift is worth a test because the failure it produced was silent.
On 3.14 the Neo4j sidecar exits without writing stderr, without a Neo4j
log, and without raising through the attach — the knowledge base simply
never comes up, and the panel reports "disabled" as though someone had
configured it that way.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from ember_code.backend.__main__ import SUPPORTED_PYTHON, warn_if_unsupported_python

_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = tomllib.loads((_ROOT / "pyproject.toml").read_text())
_VERSION = f"{SUPPORTED_PYTHON[0]}.{SUPPORTED_PYTHON[1]}"


def test_requires_python_pins_one_version_not_a_floor():
    """``>=3.12`` would resolve to whatever is newest on the machine,
    which is how the backend ended up on 3.14."""
    spec = _PYPROJECT["project"]["requires-python"]

    assert f">={_VERSION}" in spec
    assert "<3.13" in spec, f"requires-python is a floor, not a pin: {spec}"


def test_mypy_checks_the_version_that_ships():
    assert _PYPROJECT["tool"]["mypy"]["python_version"] == _VERSION


def test_the_tauri_runtime_installs_the_same_version():
    """The one that actually reaches a user."""
    runtime = (_ROOT / "clients/tauri/src-tauri/src/runtime.rs").read_text()
    match = re.search(r'const PYTHON_VERSION: &str = "([^"]+)"', runtime)

    assert match, "PYTHON_VERSION is gone from runtime.rs"
    assert match.group(1) == _VERSION


def test_the_version_file_is_the_pin():
    """``.python-version`` is what ``uv venv`` and ``uv run`` read. Its
    absence is how a development checkout ended up on 3.14 while every
    other file said 3.12."""
    assert (_ROOT / ".python-version").read_text().strip() == _VERSION


def test_the_lock_is_resolved_for_that_version_only():
    lock = (_ROOT / "uv.lock").read_text()
    match = re.search(r'requires-python = "([^"]+)"', lock)

    assert match, "uv.lock has no requires-python"
    assert match.group(1) == f"=={_VERSION}.*", f"lock resolved for {match.group(1)}"


def test_the_package_advertises_one_version():
    """Classifiers are a public support claim; PyPI shows them."""
    claimed = [
        c.rsplit(" :: ", 1)[1]
        for c in _PYPROJECT["project"]["classifiers"]
        if c.startswith("Programming Language :: Python :: ") and "." in c
    ]
    assert claimed == [_VERSION], f"the package page claims {claimed}"


def test_ci_reads_the_version_rather_than_repeating_it():
    """Every written-out copy of the version is a chance to drift, and
    this file held two. A matrix also put the version in the *check
    name* (``test (3.11)``), which the branch ruleset requires by name —
    so changing versions silently demanded a ruleset edit, and
    forgetting it blocked every PR on a check that could never report.
    """
    ci = (_ROOT / ".github/workflows/ci.yml").read_text()

    assert "python-version-file: .python-version" in ci
    assert not re.search(r'python-version: "3\.\d+"', ci), (
        "ci.yml writes a Python version out instead of reading the pin"
    )
    assert not re.search(r"python-version: \[", ci), (
        "a version matrix puts the version back into the check name"
    )


class TestTheRuntimeWarning:
    def test_it_is_quiet_on_the_supported_version(self):
        emitted: list[tuple] = []
        ok = warn_if_unsupported_python(
            emit=lambda *a: emitted.append(a),
        )
        # The suite may itself be running off-version; assert the
        # relationship rather than the absolute.
        import sys

        if sys.version_info[:2] == SUPPORTED_PYTHON:
            assert ok and not emitted
        else:
            assert not ok and emitted

    def test_it_names_both_versions_when_it_complains(self):
        """A warning that says "unsupported" without saying what is
        supported makes the reader go and find out."""
        emitted: list[tuple] = []
        warn_if_unsupported_python(emit=lambda *a: emitted.append(a))

        import sys

        if sys.version_info[:2] == SUPPORTED_PYTHON:
            pytest.skip("running on the supported version; nothing to warn about")
        template, *args = emitted[0]
        rendered = template % tuple(args)
        assert _VERSION in rendered
        assert f"{sys.version_info[0]}.{sys.version_info[1]}" in rendered
