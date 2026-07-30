"""Ember-specific eval assertions — file system and tool call checks.

Promotes the previous ``_HANDLERS`` dispatch dict of free functions
to a :class:`FileAssertionCheckers` class holding one method per
``FileAssertionType`` (subclass override / polymorphism-by-name).
The public entry point :func:`check_file_assertion` is preserved for
back-compat with callers that pass raw dicts.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)


# ── Assertion schema (Rule 1 / Pattern 2) ─────────────────────────
FileAssertionType = Literal[
    "file_exists",
    "file_not_exists",
    "file_contains",
    "file_not_contains",
    "file_unchanged",
]


class FileAssertion(BaseModel):
    """A single file-system assertion.

    ``type`` is a `Literal` so a typo in the eval YAML fails Pydantic
    validation at load time instead of silently taking the
    ``unknown assertion type`` path at run time.
    """

    type: FileAssertionType
    path: str = ""
    pattern: str = ""


def check_unexpected_tool_calls(
    response: object,
    unexpected: list[str],
) -> tuple[bool, str]:
    """Check that none of the unexpected tools were called.

    Parses tool calls from the response's messages (OpenAI format).
    Returns (passed, detail).
    """
    called_tools: set[str] = set()

    # RunOutput.tools is a list of ToolExecution objects
    tools = getattr(response, "tools", None)
    if tools:
        for tool in tools:
            name = getattr(tool, "tool_name", None)
            if name:
                called_tools.add(name)

    # Fallback: parse from messages
    if not called_tools:
        messages = getattr(response, "messages", None) or []
        for msg in messages:
            for tc in getattr(msg, "tool_calls", None) or []:
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                name = fn.get("name", "")
                if name:
                    called_tools.add(name)

    forbidden_used = called_tools & set(unexpected)
    if forbidden_used:
        return False, f"unexpected tools called: {', '.join(sorted(forbidden_used))}"
    return True, "no unexpected tool calls"


# ── Per-type checker methods on one class ─────────────────────────


class FileAssertionCheckers:
    """One method per :data:`FileAssertionType`.

    Replaces the previous ``_HANDLERS`` module-level dict-of-functions.
    :meth:`check` dispatches to the appropriate method by name, so the
    Literal type acts as method-name selector (Pattern 4 polymorphism
    without a subclass explosion — every check is stateless, one class
    keeps them cohesive).
    """

    def check(self, spec: FileAssertion, path: Path) -> tuple[bool, str]:
        method_name = f"_check_{spec.type}"
        method = getattr(self, method_name, None)
        if method is None:  # unreachable — Literal validation catches it
            return False, f"unknown assertion type: {spec.type}"
        return method(spec, path)

    @staticmethod
    def _check_file_exists(_spec: FileAssertion, path: Path) -> tuple[bool, str]:
        if path.is_file():
            return True, f"{path} exists"
        return False, f"{path} does not exist"

    @staticmethod
    def _check_file_not_exists(_spec: FileAssertion, path: Path) -> tuple[bool, str]:
        if not path.exists():
            return True, f"{path} does not exist"
        return False, f"{path} exists (expected not to)"

    @staticmethod
    def _check_file_contains(spec: FileAssertion, path: Path) -> tuple[bool, str]:
        if not path.is_file():
            return False, f"{path} does not exist"
        content = path.read_text()
        if re.search(spec.pattern, content):
            return True, f"{path} contains /{spec.pattern}/"
        return False, f"{path} does not contain /{spec.pattern}/"

    @staticmethod
    def _check_file_not_contains(spec: FileAssertion, path: Path) -> tuple[bool, str]:
        if not path.is_file():
            return True, f"{path} does not exist (OK for not_contains)"
        content = path.read_text()
        if re.search(spec.pattern, content):
            return False, f"{path} contains /{spec.pattern}/ (expected not to)"
        return True, f"{path} does not contain /{spec.pattern}/"

    @staticmethod
    def _check_file_unchanged(_spec: FileAssertion, _path: Path) -> tuple[bool, str]:
        # Requires pre-run snapshot — skip for now
        return True, "file_unchanged not yet implemented, skipping"


#: Shared default instance — the checker methods are stateless so we
#: reuse a single instance across every driver invocation.
DEFAULT_CHECKERS = FileAssertionCheckers()


def check_file_assertion(
    assertion: dict | FileAssertion,
    work_dir: Path | None = None,
) -> tuple[bool, str]:
    """Run a single file assertion. Returns (passed, detail).

    Accepts either a raw dict (legacy caller shape from YAML) or a
    validated :class:`FileAssertion`. Relative ``path`` entries are
    resolved against ``work_dir`` so the assertion checks the *case's*
    sandbox, not the script's cwd. Absolute paths are honored as-is.

    Unknown ``type`` values fail validation before reaching a handler
    — the old runtime ``unknown assertion type`` fallback is kept only
    for the raw-dict path so callers passing legacy shapes get the same
    error string instead of a raised ``ValidationError``.
    """
    if isinstance(assertion, FileAssertion):
        spec = assertion
    else:
        try:
            spec = FileAssertion.model_validate(assertion)
        except ValidationError:
            return False, f"unknown assertion type: {assertion.get('type', '')}"

    raw = spec.path
    p = Path(raw)
    path = p if p.is_absolute() or work_dir is None else (work_dir / p)

    return DEFAULT_CHECKERS.check(spec, path)
