"""MCP-call display widget — collapsible summary of one MCP tool call.

Extracted from ``_messages.py`` (iter 39) per Pattern 8: small
modules, one responsibility.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Collapsible, Static


class MCPCallWidget(Widget):
    """Displays an MCP server tool call."""

    DEFAULT_CSS = """
    MCPCallWidget {
        height: auto;
        margin: 0 2;

    }

    MCPCallWidget .mcp-header {
        color: $primary;
        text-style: bold;
    }

    MCPCallWidget .mcp-result {
        color: $text-muted;
        padding: 0 0 0 2;
    }
    """

    def __init__(
        self,
        server_name: str,
        tool_name: str,
        args: dict | None = None,
        result: str = "",
    ):
        super().__init__()
        self._server_name = server_name
        self._tool_name = tool_name
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

        title = f"MCP [{self._server_name}]: {self._tool_name}{args_summary}"

        with Collapsible(title=title, collapsed=True):
            if self._result:
                yield Static(self._result, classes="mcp-result")
            else:
                yield Static("[dim]No output[/dim]", classes="mcp-result")
