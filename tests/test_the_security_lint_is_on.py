"""ruff's flake8-bandit rules (``S``) run on igni and its SDKs, with nothing blanket-ignored.

They found an untrusted repository's ``api_key_cmd`` being run when it was
switched on (see ``test_an_untrusted_folder_runs_no_key_command.py``). A
rule set somebody quietly drops, or an ``ignore = ["S"]``, would let the next
one through, so the configuration is pinned here.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "pyproject",
    [
        "pyproject.toml",
        "packages/igni-service/pyproject.toml",
        "packages/igni-extension/pyproject.toml",
    ],
)
def test_bandit_rules_are_selected_and_only_assert_is_off(pyproject: str):
    lint = tomllib.loads((_ROOT / pyproject).read_text())["tool"]["ruff"]["lint"]
    assert "S" in lint["select"]
    assert [rule for rule in lint["ignore"] if rule.startswith("S")] == ["S101"]
    for path, rules in lint.get("per-file-ignores", {}).items():
        assert "tests" in path, f"{path}: bandit rules may be relaxed for tests only"
        assert "S" not in rules
