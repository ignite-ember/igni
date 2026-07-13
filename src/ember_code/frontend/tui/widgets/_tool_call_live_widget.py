"""Live tool-call widget — running + done rendering with expand.

Extracted from ``_messages.py`` (iter 42) per Pattern 8. Sibling
to ``ToolCallWidget`` (which renders finalized calls only). This
one handles the in-flight lifecycle: streaming progress lines
while the tool runs, then a collapsed preview + click-to-expand
full result once it finishes.
"""

from __future__ import annotations

from rich.console import Group
from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual.widgets import Static

from ember_code.frontend.tui.widgets._messages_common import TOOL_FRIENDLY_NAMES


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
        is_error: bool = False,
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
        # ``True`` when the tool returned an error payload (raised, or
        # returned a string starting with ``"Error:"``). Header renders
        # ``✗`` in red instead of the dim ``✓`` so a failed
        # call in a batch is visually distinct — used to be all green
        # checkmarks even when the agent saw "Error: ..." in its tool
        # result and treated it as a failure.
        self._is_error = is_error
        display = self._format()
        super().__init__(display)

    def is_running(self) -> bool:
        """Return True if this tool call is still running."""
        return self._status == "running"

    def mark_error(self, summary: str = "") -> None:
        """Switch this completed call to the error display.

        Called from the run controller when a ``ToolCompleted`` event
        arrives with ``is_error=True``. If ``summary`` is provided it
        overrides any existing result summary so the inline footer
        shows the error message.
        """
        self._is_error = True
        if summary:
            self._result_summary = summary

    def render_text(self) -> str:
        """Render for tests and direct inspection. Uses raw tool name and style hints."""
        args = f"({self._args_summary})" if self._args_summary else ""
        if self._status == "running":
            return f"⏳ {self._raw_tool_name}{args}"
        if self._is_error:
            return f"[red]✗ {self._raw_tool_name}{args}[/red]"
        return f"[dim]✓ {self._raw_tool_name}{args}[/dim]"

    def _format_header(self) -> str:
        """Format just the header line (checkmark/cross + tool name + args)."""
        safe_args = self._args_summary.replace("[", "\\[") if self._args_summary else ""
        args = f"({safe_args})" if safe_args else ""
        if self._is_error:
            return f"[red]✗ {self._tool_name}{args}[/red]"
        return f"[dim]✓ {self._tool_name}{args}[/dim]"

    def _format(self) -> str:
        # Escape Rich markup in args to avoid bracket conflicts
        safe_args = self._args_summary.replace("[", "\\[") if self._args_summary else ""
        args = f"({safe_args})" if safe_args else ""
        if self._status == "running":
            header = f"[bold $accent]⏳ {self._tool_name}{args}[/bold $accent]"
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
        # Done — glyph + colour reflect the tool's actual outcome so
        # a failed call in an 8-tool batch is visually distinct from
        # the successful ones.
        if self._is_error:
            line1 = f"[red]✗ {self._tool_name}{args}[/red]"
        else:
            line1 = f"[dim]✓ {self._tool_name}{args}[/dim]"
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
            header = Text.from_markup(self._format_header())
            collapsed_table, expanded_table = self._diff_table
            if self._expanded:
                return Group(header, expanded_table)
            return Group(header, collapsed_table)

        # Expanded non-diff results — use Markdown for proper Unicode handling
        if self._expanded and self._full_result and not self._result_has_markup:
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
