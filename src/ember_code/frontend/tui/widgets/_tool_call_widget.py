"""Collapsible tool-call display — finalized tool call + result.

Extracted from ``_messages.py`` (iter 40) per Pattern 8. The
sibling widget ``ToolCallLiveWidget`` (still in `_messages.py`
until its own extraction) renders in-flight streaming; this one
is used for the final resolved record.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Collapsible, Static

from ember_code.frontend.tui.widgets._messages_common import TOOL_FRIENDLY_NAMES


class ToolCallWidget(Widget):
    """Collapsible display for a tool call and its result."""

    DEFAULT_CSS = """
    ToolCallWidget {
        height: auto;
        margin: 0 2;

    }

    ToolCallWidget .tool-header {
        color: $warning;
    }

    ToolCallWidget .tool-result {
        color: $text-muted;
        padding: 0 0 0 2;
    }
    """

    def __init__(self, tool_name: str, args: dict | None = None, result: str = ""):
        super().__init__()
        self._tool_name = TOOL_FRIENDLY_NAMES.get(tool_name, tool_name)
        self._args = args or {}
        self._result = result

    def compose(self) -> ComposeResult:
        args_summary = ""
        if self._args:
            parts = []
            for k, v in self._args.items():
                val = str(v)
                if len(val) > 40:
                    val = val[:37] + "..."
                parts.append(f"{k}={val}")
            args_summary = f" ({', '.join(parts)})"

        title = f"{self._tool_name}{args_summary}"

        with Collapsible(title=title, collapsed=True):
            if self._result:
                yield Static(self._result, classes="tool-result")
            else:
                yield Static("[dim]No output[/dim]", classes="tool-result")
