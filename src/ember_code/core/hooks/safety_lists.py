"""Safety-list checks — protected paths + blocked shell commands.

Extracted from :mod:`ember_code.core.hooks.tool_hook` so the
defense-in-depth safety checks that fire BEFORE the permission
evaluator are auditable in isolation. Both functions here are pure:
they take the tool name + args + the pre-configured list, and
return either ``None`` (allowed) or a user-facing block message
(denied).

Threat model — CC's "bypass-resistant scoped deny": a
``PreToolUse`` hook's ``allow`` decision, or a
``PermissionMode.BYPASS_PERMISSIONS`` mode setting, MUST NOT be
able to unlock a write to ``.env`` / ``*.key`` / etc. or a shell
call matching ``rm -rf /``. These checks run at hook steps 2 and 3,
before the evaluator's mode-auto-allow step 4, so hooks/modes
never touch them.
"""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Function names that mutate the filesystem via the file-toolkit
# family. The protected-paths check gates writes to any of these.
_WRITE_TOOL_FUNCTIONS = frozenset(
    {
        "save_file",
        "edit_file",
        "edit_file_replace_all",
        "create_file",
    }
)

# Function names that spawn shell commands. The blocked-commands
# check gates every call to these against the configured deny list.
_SHELL_TOOL_FUNCTIONS = frozenset({"run_shell_command"})


def _is_protected_path(path: str, protected_patterns: list[str]) -> bool:
    """Return True when ``path`` matches any pattern in ``protected_patterns``.

    Matches either the basename or the full path against each
    pattern — an ``.env`` in a subdirectory still trips the
    basename match, and a full-path pattern like
    ``**/credentials.json`` still trips the full-path match.
    """
    filename = Path(path).name
    for pattern in protected_patterns:
        if fnmatch.fnmatch(filename, pattern):
            return True
        if fnmatch.fnmatch(path, pattern):
            return True
    return False


def check_protected_paths(
    tool_name: str,
    args: dict[str, Any],
    protected_paths: list[str],
) -> str | None:
    """Return a block message if this tool call would write to a
    protected path; ``None`` when it's clean.

    Only trips for tools in :data:`_WRITE_TOOL_FUNCTIONS`. Reads
    ``args["file_path"]`` — matches every file-write entrypoint in
    the toolkit.
    """
    if not protected_paths:
        return None
    if tool_name not in _WRITE_TOOL_FUNCTIONS:
        return None
    file_path = args.get("file_path", "")
    if not file_path:
        return None
    if _is_protected_path(file_path, protected_paths):
        logger.warning("Protected path blocked: %s via %s", file_path, tool_name)
        return f"Blocked: '{file_path}' is a protected path and cannot be written to."
    return None


def check_blocked_commands(
    tool_name: str,
    args: dict[str, Any],
    blocked_commands: list[str],
) -> str | None:
    """Return a block message if this tool call runs a blocked shell
    command; ``None`` when it's clean.

    Only trips for tools in :data:`_SHELL_TOOL_FUNCTIONS`. Matches
    the joined ``args["args"]`` (list or string) against every
    substring in ``blocked_commands``.
    """
    if not blocked_commands:
        return None
    if tool_name not in _SHELL_TOOL_FUNCTIONS:
        return None
    cmd_args = args.get("args", [])
    cmd_str = (
        " ".join(str(a) for a in cmd_args) if isinstance(cmd_args, list) else str(cmd_args)
    )
    for blocked in blocked_commands:
        if blocked in cmd_str:
            logger.warning("Blocked command: %s", cmd_str)
            return f"Blocked: command matches blocked pattern '{blocked}'."
    return None
