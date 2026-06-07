"""Ember-specific eval assertions — file system and tool call checks."""

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


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


def check_file_assertion(assertion: dict, work_dir: Path | None = None) -> tuple[bool, str]:
    """Run a single file assertion. Returns (passed, detail).

    Relative ``path`` entries are resolved against ``work_dir`` so the
    assertion checks the *case's* sandbox, not the script's cwd. Absolute
    paths are honored as-is.
    """
    atype = assertion.get("type", "")
    raw = assertion.get("path", "")
    p = Path(raw)
    path = p if p.is_absolute() or work_dir is None else (work_dir / p)
    pattern = assertion.get("pattern", "")

    if atype == "file_exists":
        if path.is_file():
            return True, f"{path} exists"
        return False, f"{path} does not exist"

    if atype == "file_not_exists":
        if not path.exists():
            return True, f"{path} does not exist"
        return False, f"{path} exists (expected not to)"

    if atype == "file_contains":
        if not path.is_file():
            return False, f"{path} does not exist"
        content = path.read_text()
        if re.search(pattern, content):
            return True, f"{path} contains /{pattern}/"
        return False, f"{path} does not contain /{pattern}/"

    if atype == "file_not_contains":
        if not path.is_file():
            return True, f"{path} does not exist (OK for not_contains)"
        content = path.read_text()
        if re.search(pattern, content):
            return False, f"{path} contains /{pattern}/ (expected not to)"
        return True, f"{path} does not contain /{pattern}/"

    if atype == "file_unchanged":
        # Requires pre-run snapshot — skip for now
        return True, "file_unchanged not yet implemented, skipping"

    return False, f"unknown assertion type: {atype}"
