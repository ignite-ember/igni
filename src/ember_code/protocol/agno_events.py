"""Format helpers for translating Agno events to TUI-friendly data.

Agno event type tuples, friendly tool names, and formatting functions
used by RunController to render Agno streaming events in the TUI.
"""

import logging
from typing import Any

from agno.run import agent as agent_events
from agno.run import team as team_events

logger = logging.getLogger(__name__)

# ── Agno bug workarounds ─────────────────────────────────────────────

# Workaround 1: Agno's team HITL streaming code calls `run_response.agent_id`
# on a TeamRunOutput (which only has `team_id`). Monkeypatch the missing
# attribute so the event creation doesn't crash.
try:
    from agno.run.team import TeamRunOutput as _TRO

    if not hasattr(_TRO, "agent_id"):
        _TRO.agent_id = None  # type: ignore[attr-defined]
    if not hasattr(_TRO, "agent_name"):
        _TRO.agent_name = None  # type: ignore[attr-defined]
except ImportError:
    pass

# ── Agno event type sets ──────────────────────────────────────────────

CONTENT_EVENTS = (agent_events.RunContentEvent, team_events.RunContentEvent)
TOOL_STARTED_EVENTS = (agent_events.ToolCallStartedEvent, team_events.ToolCallStartedEvent)
TOOL_COMPLETED_EVENTS = (agent_events.ToolCallCompletedEvent, team_events.ToolCallCompletedEvent)
TOOL_ERROR_EVENTS = (agent_events.ToolCallErrorEvent, team_events.ToolCallErrorEvent)
MODEL_COMPLETED_EVENTS = (
    agent_events.ModelRequestCompletedEvent,
    team_events.ModelRequestCompletedEvent,
)
RUN_CONTENT_COMPLETED_EVENTS = (
    agent_events.RunContentCompletedEvent,
    team_events.RunContentCompletedEvent,
)
RUN_COMPLETED_EVENTS = (
    agent_events.RunCompletedEvent,
    team_events.RunCompletedEvent,
    agent_events.RunOutput,
    team_events.RunOutput,
)
RUN_STARTED_EVENTS = (agent_events.RunStartedEvent, team_events.RunStartedEvent)
RUN_ERROR_EVENTS = (agent_events.RunErrorEvent, team_events.RunErrorEvent)
REASONING_EVENTS = (agent_events.ReasoningStartedEvent, team_events.ReasoningStartedEvent)
REASONING_CONTENT_EVENTS = (
    agent_events.ReasoningContentDeltaEvent,
    team_events.ReasoningContentDeltaEvent,
)
TASK_CREATED_EVENTS = (team_events.TaskCreatedEvent,)
TASK_UPDATED_EVENTS = (team_events.TaskUpdatedEvent,)
TASK_ITERATION_EVENTS = (team_events.TaskIterationStartedEvent,)
TASK_STATE_UPDATED_EVENTS = (team_events.TaskStateUpdatedEvent,)
RUN_PAUSED_EVENTS = (agent_events.RunPausedEvent, team_events.RunPausedEvent)

# ── Friendly tool names ──────────────────────────────────────────────

TOOL_NAMES = {
    "read_file": "Read",
    "save_file": "Write",
    "edit_file": "Edit",
    "edit_file_replace_all": "Edit",
    "create_file": "Write",
    "run_shell_command": "Bash",
    "grep": "Grep",
    "grep_files": "Grep",
    "grep_count": "Grep",
    "glob_files": "Glob",
    "list_files": "LS",
    "duckduckgo_search": "WebSearch",
    "duckduckgo_news": "WebSearch",
    "fetch_url": "WebFetch",
    "fetch_json": "WebFetch",
    "run_python_code": "Python",
    "spawn_agent": "Agent",
    "spawn_team": "Team",
    "delegate_task_to_member": "Delegate",
    "delegate_task_to_members": "Delegate",
    "search_knowledge_base": "Knowledge",
    "update_user_memory": "Memory",
    "schedule_task": "Schedule",
    "list_scheduled_tasks": "Schedule",
    "cancel_scheduled_task": "Schedule",
}


# ── Formatting helpers ────────────────────────────────────────────────


def format_tool_args(tool_args: dict | None, tool_name: str = "") -> str:
    """Format tool arguments into a short summary string."""
    if not tool_args or not isinstance(tool_args, dict):
        return ""

    # Orchestration tools — the model often passes a multi-paragraph
    # markdown brief as the task. Showing it verbatim in the tool-call
    # header drowns the activity log and leaks raw markdown into the
    # rendered terminal. Trim to the first non-empty line, capped at
    # 80 chars; the full task is still available in the agent's
    # session — the header is just a glanceable label.
    if tool_name in ("spawn_agent", "spawn_team"):
        # spawn_agent passes ``agent_name`` (scalar). spawn_team
        # passes ``agent_names`` as a LIST — joining ``parts`` with
        # a list element would TypeError. Coerce both shapes to a
        # single display string before joining.
        agent_raw = tool_args.get("agent_name") or tool_args.get("agent_names") or ""
        if isinstance(agent_raw, (list, tuple)):
            agent = ", ".join(str(n) for n in agent_raw)
        else:
            agent = str(agent_raw)
        task = str(tool_args.get("task", ""))
        mode = tool_args.get("mode", "")
        first_line = next((ln.strip() for ln in task.splitlines() if ln.strip()), "")
        if len(first_line) > 80:
            first_line = first_line[:77] + "..."
        parts: list[str] = []
        if agent:
            parts.append(agent)
        if mode:
            parts.append(f"mode={mode}")
        if first_line:
            parts.append(f'"{first_line}"')
        return ", ".join(parts)

    parts = []
    for k, v in list(tool_args.items())[:3]:
        val = str(v)
        if len(val) > 30:
            val = val[:27] + "..."
        parts.append(f"{k}={val}")
    return ", ".join(parts)


def _get_terminal_width() -> int:
    """Get the current terminal width for diff line padding."""
    import shutil

    return shutil.get_terminal_size((120, 40)).columns


_DIFF_PAD = 300  # fallback; prefer _get_terminal_width() at render time


def _styled_line(prefix: str, content: str, pad_width: int, style: str) -> str:
    """Build a styled diff line padded to fill the full width."""
    padded = f"{prefix}{content}".ljust(pad_width)
    return f"[{style}]{padded}[/{style}]"


def _build_diff_table(rows: list[tuple[str, str]], max_rows: int | None = None) -> Any:
    """Build a Rich Table for the diff with full-width colored backgrounds.

    Args:
        rows: list of (display_text, style_string) tuples.
        max_rows: if set, limit to this many rows and add a hint.
    """
    from rich.table import Table
    from rich.text import Text

    table = Table(
        show_header=False,
        show_edge=False,
        show_lines=False,
        box=None,
        expand=True,
        padding=0,
        pad_edge=False,
    )
    table.add_column(ratio=1, no_wrap=False, overflow="fold")

    display_rows = rows[:max_rows] if max_rows else rows
    for text_content, style in display_rows:
        table.add_row(Text(text_content, style=style) if style else Text(text_content))

    if max_rows and len(rows) > max_rows:
        remaining = len(rows) - max_rows
        table.add_row(Text(f"  └ {remaining} more lines — click to expand", style="dim"))

    return table


def _format_edit_diff(tool: Any) -> tuple[Any, Any, list[tuple[str, str]]] | None:
    """Format an Edit tool's diff as (markup_preview, table_renderable).

    Returns a tuple of (preview_markup_str, Rich Table) or None.
    The preview is used for the collapsed view, the Table for the expanded view
    so that backgrounds fill edge-to-edge even on wrapped lines.
    """
    import difflib

    args = getattr(tool, "tool_args", None)
    if not args or not isinstance(args, dict):
        return None
    old = args.get("old_string", "")
    new = args.get("new_string", "")
    if not old and not new:
        return None

    old_lines = old.splitlines()
    new_lines = new.splitlines()

    # Find the real starting line number in the file. Try
    # ``new`` first (post-edit / history re-render — the file
    # already has the new content), then fall back to ``old``
    # (live in-flight edit — file still has the pre-edit
    # content). Without the ``old`` fallback, live edit cards
    # always show "line 1" regardless of where the edit
    # actually lands.
    start_line = 1
    file_path = args.get("file_path", "")
    if file_path:
        try:
            with open(file_path) as f:
                file_content = f.read()
            idx = file_content.find(new) if new else -1
            if idx < 0 and old:
                idx = file_content.find(old)
            if idx >= 0:
                start_line = file_content[:idx].count("\n") + 1
        except Exception:
            pass

    sm = difflib.SequenceMatcher(None, old_lines, new_lines)
    # Collect lines as (display_text, style_string)
    rows: list[tuple[str, str]] = []
    old_num = start_line
    new_num = start_line

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(j2 - j1):
                rows.append((f"  {new_num + k:>4}   {new_lines[j1 + k]}", ""))
            old_num += i2 - i1
            new_num += j2 - j1
        elif tag == "delete":
            for k in range(i2 - i1):
                rows.append((f"- {old_num + k:>4}   {old_lines[i1 + k]}", "#ff6b6b on #3d0000"))
            old_num += i2 - i1
        elif tag == "insert":
            for k in range(j2 - j1):
                rows.append((f"+ {new_num + k:>4}   {new_lines[j1 + k]}", "#69db7c on #003d00"))
            new_num += j2 - j1
        elif tag == "replace":
            for k in range(i2 - i1):
                rows.append((f"- {old_num + k:>4}   {old_lines[i1 + k]}", "#ff6b6b on #3d0000"))
            old_num += i2 - i1
            for k in range(j2 - j1):
                rows.append((f"+ {new_num + k:>4}   {new_lines[j1 + k]}", "#69db7c on #003d00"))
            new_num += j2 - j1

    if not rows:
        return None

    # Build both collapsed and expanded tables
    collapsed_table = _build_diff_table(rows, max_rows=4)
    expanded_table = _build_diff_table(rows)

    return collapsed_table, expanded_table, rows


class ToolResultData:
    """Extracted tool result data — replaces positional tuples."""

    __slots__ = ("summary", "full_result", "has_markup", "diff_table", "diff_rows")

    def __init__(
        self,
        summary: str = "",
        full_result: str = "",
        has_markup: bool = False,
        diff_table: Any = None,
        diff_rows: list | None = None,
    ):
        self.summary = summary
        self.full_result = full_result
        self.has_markup = has_markup
        self.diff_table = diff_table  # (collapsed, expanded) Rich Table pair
        self.diff_rows = diff_rows  # raw (text, style) list for serialization


def extract_result(event: Any) -> ToolResultData:
    """Extract tool result data from an Agno tool completion event."""
    tool = getattr(event, "tool", None)

    timing = ""
    if tool:
        tool_metrics = getattr(tool, "metrics", None)
        if tool_metrics:
            duration = getattr(tool_metrics, "duration", None)
            if duration is not None:
                timing = f"{duration:.2f}s"

    result = getattr(tool, "result", None) if tool else None
    tool_name = getattr(tool, "tool_name", "?") if tool else "?"

    # Debug: log raw tool result
    logger.debug(
        "extract_result [%s]: result type=%s, is_none=%s, len=%d",
        tool_name,
        type(result).__name__,
        result is None,
        len(str(result)) if result is not None else 0,
    )

    # For Edit tools, show a colored diff instead of "Successfully edited".
    # We must skip this on failure: a failed edit_file still has
    # ``old_string``/``new_string`` in ``tool_args`` (the LLM's proposed
    # change), so ``_format_edit_diff`` happily renders a *fake* diff
    # from a change that never happened. Worse, this branch returns
    # ``full_result=""`` which hides the ``Error:`` prefix from the
    # serializer's ``_result_is_error`` check — that's the v0.5.11 green
    # ✓ bug surviving even after the prefix detection was added.
    result_str = str(result).strip() if result else ""
    if tool_name == "edit_file" and tool and not result_str.startswith("Error:"):
        diff = _format_edit_diff(tool)
        if diff:
            collapsed_table, expanded_table, raw_rows = diff
            summary_msg = result_str or "Edited"
            if timing:
                summary_msg = f"{summary_msg}, {timing}"
            return ToolResultData(
                summary=summary_msg,
                full_result="",
                has_markup=True,
                diff_table=(collapsed_table, expanded_table),
                diff_rows=raw_rows,
            )

    full_text = str(result).strip() if result else ""
    # MCP tools may return literal "None"/"null" for empty responses
    if full_text in ("None", "null", "undefined"):
        full_text = ""

    summary = ""
    if full_text:
        lines = full_text.splitlines()
        if len(lines) <= 1:
            short = full_text[:80]
            summary = short + ("..." if len(full_text) > 80 else "")
        else:
            summary = f"{len(lines)} lines of output"

    if summary and timing:
        summary = f"{summary}, {timing}"
    elif not summary and timing:
        summary = f"completed in {timing}"

    return ToolResultData(summary=summary, full_result=full_text)
