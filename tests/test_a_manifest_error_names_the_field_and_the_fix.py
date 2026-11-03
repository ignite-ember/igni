"""A manifest that does not validate says which field, and what to write.

It used to print pydantic's report: the model's class name, the
input's repr, an error type in brackets and an errors.pydantic.dev URL
— accurate, and of no use to somebody who misspelt ``permissions``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ember_code.extensions.manifest import ExtensionManifest, ManifestError

BASE = {
    "id": "acme.tickets",
    "version": "0.1.0",
    "contract": ">=1.0 <2",
    "trust": "unsigned",
    "dev": {"command": ["python", "server.py"]},
}

HINTS = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}


def _error(tmp_path: Path, manifest: dict) -> str:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ManifestError) as caught:
        ExtensionManifest.load(path)
    message = str(caught.value)
    assert "errors.pydantic.dev" not in message
    assert "validation error for" not in message
    assert "type=" not in message
    return message


def test_a_misspelt_field_is_named_with_what_was_meant(tmp_path):
    message = _error(tmp_path, {**BASE, "permisions": {}})
    assert "permisions" in message
    assert "did you mean 'permissions'" in message


def test_a_misspelt_nested_field_is_named_by_its_path(tmp_path):
    message = _error(tmp_path, {**BASE, "permissions": {"rcp": []}})
    assert "permissions.rcp" in message
    assert "did you mean 'rpc'" in message


def test_a_missing_field_says_it_is_required(tmp_path):
    manifest = dict(BASE)
    del manifest["id"]
    message = _error(tmp_path, manifest)
    assert "id: required" in message
    assert "reverse-DNS" in message


def test_a_tool_given_as_a_string_shows_the_shape(tmp_path):
    message = _error(tmp_path, {**BASE, "permissions": {"tools": ["acme.tickets.lookup"]}})
    assert "permissions.tools[0]" in message
    assert '"annotations"' in message


def test_a_tool_missing_a_hint_says_all_four_are_needed(tmp_path):
    tool = {"name": "acme.tickets.lookup", "annotations": HINTS}
    message = _error(tmp_path, {**BASE, "permissions": {"tools": [tool]}})
    assert "permissions.tools[0].annotations.openWorldHint: required" in message
    assert "all four" in message


def test_a_rule_the_model_checks_reads_as_a_sentence(tmp_path):
    manifest = {**BASE, "python": {"package": "p", "entry": "p", "mcp": {"transport": "stdio"}}}
    message = _error(tmp_path, manifest)
    assert "exactly one of" in message
    assert "Value error" not in message


def test_the_error_is_still_a_value_error(tmp_path):
    """The store and the installer catch ValueError, and keep working."""
    assert issubclass(ManifestError, ValueError)


def test_a_misspelt_value_is_matched_to_what_was_meant(tmp_path):
    message = _error(tmp_path, {**BASE, "permissions": {"hooks": ["PostToolUsed"]}})
    assert "permissions.hooks[0]" in message
    assert "did you mean 'PostToolUse'" in message
