"""Conversation content widgets: messages, tool calls, MCP calls, agent tree."""

import logging

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Collapsible, Markdown, Static, Tree

# Shared friendly display names for internal tool names.
# Used by ToolCallWidget, ToolCallLiveWidget, and StreamHandler.
logger = logging.getLogger(__name__)

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


class MessageWidget(Widget):
    """Displays a conversation message (user or assistant).

    Long messages are truncated by default. Click the 'Show more' label
    or use Ctrl+O (expand all) to reveal the full content.
    """

    DEFAULT_CSS = """
    MessageWidget {
        height: auto;
        margin: 0 0 1 0;
        padding: 0;
    }

    MessageWidget .message-row {
        height: auto;
        width: 100%;
    }

    MessageWidget .role-label {
        width: 2;
        height: auto;
        text-style: bold;
    }

    MessageWidget .role-user {
        color: ansi_bright_blue;
    }

    MessageWidget .role-assistant {
        color: ansi_yellow;
    }

    MessageWidget .message-body {
        width: 1fr;
        height: auto;
    }

    MessageWidget .message-content {
        padding: 0;
    }

    MessageWidget .message-content-full {
        padding: 0;
        display: none;
    }

    MessageWidget .show-more {
        color: $accent;
        text-style: italic;
    }

    MessageWidget.-expanded .message-content {
        display: none;
    }

    MessageWidget.-expanded .message-content-full {
        display: block;
    }

    MessageWidget.-expanded .show-more {
        display: none;
    }
    """

    expanded = reactive(False)

    def __init__(
        self, content: str, role: str = "user", truncate_lines: int = 10, expanded: bool = False
    ):
        super().__init__()
        self._content = content
        self._role = role
        self._truncate_lines = truncate_lines
        self._is_long = len(content.splitlines()) > self._truncate_lines
        if expanded and self._is_long:
            self.expanded = True
            self.add_class("-expanded")

    @property
    def is_long(self) -> bool:
        """Whether this message exceeds the truncation threshold."""
        return self._is_long

    def compose(self) -> ComposeResult:
        content = self._content
        if self._role == "user":
            if content.startswith("$ "):
                role_display = "$ "
                content = content[2:]
            elif content.startswith("/"):
                role_display = "/ "
                content = content[1:]
            else:
                role_display = "> "
        else:
            role_display = "● "
            content = self._content
        role_class = f"role-{self._role}"

        with Horizontal(classes="message-row"):
            yield Static(f"[bold]{role_display}[/bold]", classes=f"role-label {role_class}")
            with Vertical(classes="message-body"):
                if not self._is_long:
                    if self._role == "assistant":
                        yield Markdown(content, classes="message-content")
                    else:
                        # ``markup=False`` for user content: it's raw
                        # input (could contain ``[/loop ...]``, code
                        # snippets, BBCode-shaped strings, etc.) that
                        # Textual would otherwise parse as markup and
                        # crash with ``MarkupError`` on the first
                        # unbalanced bracket. Plain-text rendering is
                        # what we want for human input anyway.
                        yield Static(content, classes="message-content", markup=False)
                else:
                    truncated = "\n".join(content.splitlines()[: self._truncate_lines])

                    if self._role == "assistant":
                        yield Markdown(truncated, classes="message-content")
                        yield Markdown(content, classes="message-content-full")
                    else:
                        yield Static(truncated, classes="message-content", markup=False)
                        yield Static(content, classes="message-content-full", markup=False)

                    lines_hidden = len(self._content.splitlines()) - self._truncate_lines
                    yield Static(
                        f"[dim italic]... {lines_hidden} more lines — click to expand[/dim italic]",
                        classes="show-more",
                    )

    def on_click(self) -> None:
        if self._is_long:
            self.toggle_expanded()

    def toggle_expanded(self) -> None:
        self.expanded = not self.expanded
        self.toggle_class("-expanded")

    def set_expanded(self, value: bool) -> None:
        if self._is_long and value != self.expanded:
            self.toggle_expanded()


class StreamingMessageWidget(Widget):
    """Displays a streaming assistant message, updated chunk by chunk."""

    DEFAULT_CSS = """
    StreamingMessageWidget {
        height: auto;
        margin: 0;
        padding: 0;
    }

    StreamingMessageWidget .message-row {
        height: auto;
        width: 100%;
    }

    StreamingMessageWidget .role-label {
        width: 2;
        height: auto;
        text-style: bold;
        color: ansi_yellow;
    }

    StreamingMessageWidget .stream-content {
        width: 1fr;
        height: auto;
        padding: 0;
    }

    StreamingMessageWidget.-thinking .role-label {
        color: ansi_bright_black;
    }

    StreamingMessageWidget.-thinking .stream-content {
        color: ansi_bright_black;
        text-style: italic;
    }

    StreamingMessageWidget.-thinking Markdown {
        color: ansi_bright_black;
    }
    """

    # Throttle markdown re-renders to keep the UI responsive during streaming.
    # Chunks are buffered and flushed at most every RENDER_INTERVAL seconds.
    RENDER_INTERVAL = 0.10  # seconds

    def __init__(self, css_class: str = ""):
        super().__init__()
        if css_class:
            self.add_class(f"-{css_class}")
        self._chunks: list[str] = []
        self._dirty = False
        self._render_timer: Timer | None = None
        self._timer_running = False

    def compose(self) -> ComposeResult:
        with Horizontal(classes="message-row"):
            yield Static("[bold]● [/bold]", classes="role-label")
            yield Markdown("", classes="stream-content")

    def on_mount(self) -> None:
        self._render_timer = self.set_interval(self.RENDER_INTERVAL, self._flush_render, pause=True)

    @property
    def text(self) -> str:
        return "".join(self._chunks)

    def append_chunk(self, chunk: str) -> None:
        """Append a text chunk. The actual render is throttled."""
        current = self.text
        if current and chunk.startswith(current) and len(chunk) > len(current):
            chunk = chunk[len(current) :]
        self._chunks.append(chunk)
        self._dirty = True
        if self._render_timer and not self._timer_running:
            self._render_timer.resume()
            self._timer_running = True

    def _flush_render(self) -> None:
        """Render accumulated chunks to the Markdown widget."""
        if not self._dirty:
            if self._render_timer:
                self._render_timer.pause()
                self._timer_running = False
            return
        self._dirty = False
        try:
            md = self.query_one(".stream-content", Markdown)
            md.update(self.text)
        except Exception as exc:
            logger.debug("Failed to update streaming content: %s", exc)

    def finalize(self) -> str:
        """Flush any pending content and return the full text."""
        if self._render_timer:
            self._render_timer.pause()
            self._timer_running = False
        if self._dirty:
            self._flush_render()
        return self.text


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


class ToolCallLiveWidget(Static):
    """Claude Code-style tool call display with click-to-expand result.

    Running:  ``● Shell(git status)``
    Done:     ``● Shell(git status)``
              ``└ completed in 0.03s — click to expand``
    """

    DEFAULT_CSS = """
    ToolCallLiveWidget {
        height: auto;
        margin: 0 0 0 2;
        overflow: hidden;
    }
    """

    def __init__(
        self,
        tool_name: str,
        args_summary: str = "",
        status: str = "running",
        preview_lines: int = 4,
    ):
        self._raw_tool_name = tool_name
        self._tool_name = TOOL_FRIENDLY_NAMES.get(tool_name, tool_name)
        self._args_summary = args_summary
        self._status = status
        self._result_summary = ""
        self._full_result = ""
        self._result_has_markup = False  # True if result contains Rich markup
        self._diff_table: object = None  # Rich Table for edit diffs
        self._expanded = False
        self._preview_lines = preview_lines
        display = self._format()
        super().__init__(display)

    def is_running(self) -> bool:
        """Return True if this tool call is still running."""
        return self._status == "running"

    def render_text(self) -> str:
        """Render for tests and direct inspection. Uses raw tool name and style hints."""
        args = f"({self._args_summary})" if self._args_summary else ""
        if self._status == "running":
            return f"\u23f3 {self._raw_tool_name}{args}"
        return f"[dim]\u2713 {self._raw_tool_name}{args}[/dim]"

    def _format_header(self) -> str:
        """Format just the header line (checkmark + tool name + args)."""
        safe_args = self._args_summary.replace("[", "\\[") if self._args_summary else ""
        args = f"({safe_args})" if safe_args else ""
        return f"[dim]\u2713 {self._tool_name}{args}[/dim]"

    def _format(self) -> str:
        # Escape Rich markup in args to avoid bracket conflicts
        safe_args = self._args_summary.replace("[", "\\[") if self._args_summary else ""
        args = f"({safe_args})" if safe_args else ""
        if self._status == "running":
            header = f"[bold $accent]\u23f3 {self._tool_name}{args}[/bold $accent]"
            agents = getattr(self, "_progress_agents", {})
            order = getattr(self, "_progress_order", [])
            if agents:
                sections = []
                for agent_key in order:
                    lines = agents.get(agent_key, [])
                    if agent_key == "_tasks":
                        # Task-level lines (not agent-specific)
                        for line in lines[-4:]:
                            sections.append(f"[dim]{line}[/dim]")
                    else:
                        # Agent header + recent activity. Show up to 8
                        # trailing lines so the rolling 5-line ``✎``
                        # streaming preview is visible alongside the
                        # last few tool-call entries (``├─`` / ``└─``).
                        sections.append(f"[bold]  ├─ \\[{agent_key}\\][/bold]")
                        for line in lines[-8:]:
                            sections.append(f"[dim]{line}[/dim]")
                header += "\n" + "\n".join(sections)
            return header
        # Done
        line1 = f"[dim]\u2713 {self._tool_name}{args}[/dim]"
        if not self._full_result:
            if self._result_summary:
                return line1 + f"\n  [dim]└ {self._result_summary}[/dim]"
            return line1

        # If result contains Rich markup (e.g. colored diff), render as-is
        if self._result_has_markup:
            display = self._full_result
        else:
            display = self._full_result.replace("[", "\\[")

        # Sanitize: strip non-printable and wide Unicode that breaks Textual layout
        def _safe_line(line: str) -> str:
            return "".join(c for c in line if c.isprintable() or c in ("\t",))

        lines = [_safe_line(line) for line in display.splitlines()]

        if self._expanded:
            return line1 + "\n" + "\n".join(lines)

        # Collapsed: show up to PREVIEW_LINES
        preview = "\n".join(lines[: self._preview_lines])
        remaining = len(lines) - self._preview_lines
        result = line1 + f"\n{preview}"
        if remaining > 0:
            result += f"\n  [dim]└ {remaining} more lines — click to expand[/dim]"
        return result

    def render(self):
        """Override render for rich content — diff tables or markdown."""
        if self._status != "done":
            return super().render()

        # Diff tables — use Rich Table for full-width backgrounds
        if self._result_has_markup and getattr(self, "_diff_table", None):
            from rich.console import Group
            from rich.text import Text

            header = Text.from_markup(self._format_header())
            collapsed_table, expanded_table = self._diff_table
            if self._expanded:
                return Group(header, expanded_table)
            return Group(header, collapsed_table)

        # Expanded non-diff results — use Markdown for proper Unicode handling
        if self._expanded and self._full_result and not self._result_has_markup:
            from rich.console import Group
            from rich.markdown import Markdown as RichMarkdown
            from rich.text import Text

            header = Text.from_markup(self._format_header())
            safe = (
                self._full_result.replace("[", "\\[")
                if not self._result_has_markup
                else self._full_result
            )
            return Group(header, RichMarkdown(safe))

        return super().render()

    def on_click(self) -> None:
        if self._status != "done":
            return
        if not self._full_result and not self._diff_table:
            return
        self._expanded = not self._expanded
        if self._diff_table:
            # Force full re-render with layout recalculation
            self.refresh(layout=True)
        else:
            self.update(self._format())

    def update_progress(self, line: str) -> None:
        """Append a progress line while the tool is running.

        Lines starting with ``├─ [name]`` are treated as agent headers.
        Tool calls underneath are grouped under the current agent.
        """
        if self._status != "running":
            return
        if not hasattr(self, "_progress_agents"):
            self._progress_agents: dict[str, list[str]] = {}
            self._progress_current_agent: str = ""
            self._progress_order: list[str] = []

        escaped = line.replace("[", "\\[").replace("]", "\\]")

        # Detect agent header lines: ├─ [name]
        # TODO: rewrite this mess
        if "├─" in line and line.strip().startswith("├─") and "├─ " not in line.split("├─")[1][:3]:
            # Extract agent name from ├─ [agentname]
            agent = line.split("├─")[1].strip().strip("[]\\")
            if agent and agent not in self._progress_agents:
                self._progress_agents[agent] = []
                self._progress_order.append(agent)
            if agent:
                self._progress_current_agent = agent
        elif self._progress_current_agent:
            agent_lines = self._progress_agents.setdefault(self._progress_current_agent, [])
            # Streaming content previews (✎): keep a rolling window of
            # the last few so the user gets ~paragraph context as the
            # agent thinks, not just one line that flickers and
            # disappears. Tool-call lifecycle lines (├─, └─) append
            # unconditionally — those are the structural skeleton of
            # what the agent did and shouldn't be evicted.
            if "✎" in line:
                agent_lines.append(escaped)
                _MAX_PREVIEW = 5
                preview_indices = [i for i, ln in enumerate(agent_lines) if "✎" in ln]
                while len(preview_indices) > _MAX_PREVIEW:
                    agent_lines.pop(preview_indices[0])
                    preview_indices = [i for i, ln in enumerate(agent_lines) if "✎" in ln]
            else:
                agent_lines.append(escaped)
        # Task-level lines (not under an agent)
        elif "TASK:" in line or "╞═" in line or "│" in line:
            self._progress_agents.setdefault("_tasks", []).append(escaped)
            if "_tasks" not in self._progress_order:
                self._progress_order.insert(0, "_tasks")

        self.update(self._format())

    def mark_done(
        self,
        result_summary: str = "",
        full_result: str = "",
        has_markup: bool = False,
        diff_table: object = None,
    ) -> None:
        self._status = "done"
        self._result_summary = result_summary
        self._full_result = full_result
        self._result_has_markup = has_markup
        self._diff_table = diff_table
        self.update(self._format())


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


class AgentTreeWidget(Widget):
    """Displays the orchestrator's team plan as a tree."""

    DEFAULT_CSS = """
    AgentTreeWidget {
        height: auto;
        max-height: 12;
        margin: 0 2 1 2;
        padding: 0;

    }

    AgentTreeWidget .tree-header {
        color: $accent;
        text-style: bold;
    }

    AgentTreeWidget Tree {
        height: auto;
        max-height: 10;
    }
    """

    def __init__(
        self,
        team_name: str,
        team_mode: str,
        agent_names: list[str],
        reasoning: str = "",
    ):
        super().__init__()
        self._team_name = team_name
        self._team_mode = team_mode
        self._agent_names = agent_names
        self._reasoning = reasoning

    def compose(self) -> ComposeResult:
        yield Static(
            f"[bold $accent]Team:[/bold $accent] {self._team_name} [dim]({self._team_mode})[/dim]",
            classes="tree-header",
        )
        tree: Tree[str] = Tree(self._team_name)
        tree.root.expand()

        tree.root.add(f"[dim]mode:[/dim] {self._team_mode}")

        agents_node = tree.root.add("[bold]agents[/bold]", expand=True)
        for name in self._agent_names:
            agents_node.add_leaf(f"[green]{name}[/green]")

        if self._reasoning:
            short = self._reasoning[:120]
            if len(self._reasoning) > 120:
                short += "..."
            tree.root.add(f"[dim]reason:[/dim] {short}")

        yield tree
