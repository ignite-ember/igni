"""CONTRIBUTING.md exists, sends security reports to SECURITY.md, and lists what CI runs.

A contribution guide that names checks CI does not run, or misses ones it
does, sends a contributor to a red pull request they could have caught
locally.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_CONTRIBUTING = _ROOT / "CONTRIBUTING.md"
_CI = _ROOT / ".github" / "workflows" / "ci.yml"


def test_security_reports_go_to_security_md_not_issues():
    text = _CONTRIBUTING.read_text()
    assert "[SECURITY.md](SECURITY.md)" in text
    assert "not in a public issue" in text


@pytest.mark.parametrize(
    "check",
    [
        "ruff check src/ tests/",
        "ruff format --check src/ tests/",
        "mypy src/ember_code/",
        "pytest tests/",
        "make packages-check",
        "npx tsc -b --noEmit",
        "npx vitest run",
        "npx playwright test",
    ],
)
def test_each_listed_check_is_one_ci_runs(check: str):
    assert check in _CONTRIBUTING.read_text()
    assert check in _CI.read_text(), f"CONTRIBUTING.md lists {check!r}, which CI does not run"


def test_it_asks_for_tests_and_a_sign_off():
    text = _CONTRIBUTING.read_text()
    assert "fails without the fix" in text
    assert "Signed-off-by" in text
    assert "[CODE_STANDARDS.md](CODE_STANDARDS.md)" in text
