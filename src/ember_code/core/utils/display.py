"""Display utilities — Rich-based output for the terminal."""

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel


class DisplayManager:
    """Manages terminal output with configurable formatting."""

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

    def print_tool_call(self, tool_name: str, args: dict | None = None) -> None:
        """Print a tool call notification."""
        args_str = ""
        if args:
            parts = []
            for k, v in args.items():
                val = str(v)
                if len(val) > 50:
                    val = val[:47] + "..."
                parts.append(f"{k}={val}")
            args_str = f" ({', '.join(parts)})"
        self.console.print(f"[dim]  ⚡ {tool_name}{args_str}[/dim]")

    def print_error(self, message: str) -> None:
        """Print an error message."""
        self.console.print(f"[red]Error:[/red] {message}")

    def print_warning(self, message: str) -> None:
        """Print a warning message."""
        self.console.print(f"[yellow]Warning:[/yellow] {message}")

    def print_info(self, message: str) -> None:
        """Print an info message."""
        self.console.print(f"[dim]{message}[/dim]")

    def print_run_stats(
        self,
        elapsed_seconds: float,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model: str = "",
    ) -> None:
        """Print run statistics after a completed run."""
        parts = []
        if elapsed_seconds < 60:
            parts.append(f"{elapsed_seconds:.1f}s")
        else:
            m = int(elapsed_seconds // 60)
            s = int(elapsed_seconds % 60)
            parts.append(f"{m}m {s}s")
        if input_tokens or output_tokens:
            parts.append(
                f"{input_tokens + output_tokens} tokens ({input_tokens}↑ {output_tokens}↓)"
            )
        if model:
            parts.append(model)
        self.console.print(f"[dim]  ── {' · '.join(parts)} ──[/dim]")

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


# Default instance for module-level convenience functions
_default = DisplayManager()

console = _default.console


def print_markdown(text: str) -> None:
    _default.print_markdown(text)


def print_response(text: str, agent_name: str | None = None) -> None:
    _default.print_response(text, agent_name)


def print_tool_call(tool_name: str, args: dict | None = None) -> None:
    _default.print_tool_call(tool_name, args)


def print_error(message: str) -> None:
    _default.print_error(message)


def print_warning(message: str) -> None:
    _default.print_warning(message)


def print_info(message: str) -> None:
    _default.print_info(message)


def print_run_stats(
    elapsed_seconds: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    model: str = "",
) -> None:
    _default.print_run_stats(elapsed_seconds, input_tokens, output_tokens, model)


def print_welcome(version: str) -> None:
    _default.print_welcome(version)
