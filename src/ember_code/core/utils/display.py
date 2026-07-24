"""Display utilities — Rich-based output for the terminal.

The public surface is exactly one class — :class:`DisplayManager`
— plus the two Pydantic DTOs it accepts
(:class:`RunStats` / :class:`ToolCallDisplay`), which are
re-exported here so external callers can pull everything from
this module.

There is deliberately no module-level singleton and no free-
function facade: every caller constructs (or is handed) a
:class:`DisplayManager` instance and calls methods on it. This
keeps Rule 1 (no raw dicts crossing module boundaries) and
Rule 3 (no emoji icons — text prefixes only) enforced at the
type-signature level rather than by convention.
"""

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from ember_code.core.utils.display_schemas import RunStats, ToolCallDisplay

__all__ = ["DisplayManager", "RunStats", "ToolCallDisplay"]


class DisplayManager:
    """Manages terminal output with configurable formatting.

    Every method takes either a plain string (info / warning /
    error / markdown / response / welcome banner) or one of the
    two Pydantic DTOs (:class:`RunStats`, :class:`ToolCallDisplay`).
    The DTOs own the domain formatting policy — this class is a
    thin Rich sink.
    """

    def __init__(self, console: Console | None = None):
        self.console = console or Console()

    def print_markdown(self, text: str) -> None:
        """Render markdown text in the terminal."""
        self.console.print(Markdown(text))

    def print_response(self, text: str, agent_name: str | None = None) -> None:
        """Print an agent response with optional agent label."""
        if agent_name:
            self.console.print(f"[dim]{agent_name}[/dim]")
        self.print_markdown(text)

    def print_tool_call(self, call: ToolCallDisplay) -> None:
        """Print a tool call notification.

        Delegates truncation to :meth:`ToolCallDisplay.format_args`
        so the "50-char clip + ellipsis" policy is a data
        concern, not a Rich concern. The prefix is the text
        marker ``>`` — no emoji icons (Rule 3).
        """
        args_str = call.format_args()
        self.console.print(f"[dim]  > {call.tool_name}{args_str}[/dim]")

    def print_error(self, message: str) -> None:
        """Print an error message."""
        self.console.print(f"[red]Error:[/red] {message}")

    def print_warning(self, message: str) -> None:
        """Print a warning message."""
        self.console.print(f"[yellow]Warning:[/yellow] {message}")

    def print_info(self, message: str) -> None:
        """Print an info message."""
        self.console.print(f"[dim]{message}[/dim]")

    def print_run_stats(self, stats: RunStats) -> None:
        """Print run statistics after a completed run.

        The full summary string is composed by
        :meth:`RunStats.format_summary`; this method just wraps
        it in the ``── … ──`` separator and hands it to Rich.
        """
        self.console.print(f"[dim]  ── {stats.format_summary()} ──[/dim]")

    def print_welcome(self, version: str) -> None:
        """Print the welcome banner."""
        self.console.print(
            Panel(
                f"[bold]igni[/bold] v{version}\n"
                f"[dim]AI coding assistant powered by Agno[/dim]\n\n"
                f"Type your message, or /help for commands.\n"
                f"Press Ctrl+C to exit.",
                border_style="blue",
            )
        )
