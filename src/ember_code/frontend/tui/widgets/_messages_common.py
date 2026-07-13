"""Shared constants for message-rendering widgets.

Extracted during the per-widget split of ``_messages.py``
(iter 40) so `ToolCallWidget` and `ToolCallLiveWidget` (each in
their own module) can share ``TOOL_FRIENDLY_NAMES`` without
importing back from the shrinking parent. Same shape as
``_dialogs_common.py``.
"""

from __future__ import annotations

# Shared friendly display names for internal tool names.
# Used by ToolCallWidget, ToolCallLiveWidget, and StreamHandler.
TOOL_FRIENDLY_NAMES: dict[str, str] = {
    "run_shell_command": "Shell",
    "read_file": "Read",
    "write_file": "Write",
    "edit_file": "Edit",
    "search_files": "Search",
    "grep_search": "Grep",
    "glob_files": "Glob",
    "list_directory": "List",
    "web_fetch": "Fetch",
    "web_search": "WebSearch",
    "spawn_agent": "Agent",
    "spawn_team": "Team",
}
