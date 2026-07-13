"""Command / shell mode indicators + inline shell execution.

Extracted from ``tui/app.py``. Same pattern as
``codeindex_handlers.py`` etc.

Free functions taking ``app: EmberApp`` as first arg:

* :func:`update_command_mode_indicator` — flip the prompt
  indicator + placeholder for command mode.
* :func:`exit_command_mode` — leave command mode and clear
  the input.
* :func:`update_shell_mode_indicator` — flip the prompt
  indicator + placeholder for shell mode.
* :func:`exit_shell_mode` — leave shell mode and clear the
  input.
* :func:`run_shell_inline` — run a shell command inline (no
  AI turn), stream its output into a live widget, and stash
  the transcript in ``app._shell_context`` so the next AI
  message sees it.

Rule 2 clean — all imports at module top.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from typing import TYPE_CHECKING

from rich.markup import escape
from textual.css.query import NoMatches
from textual.widgets import Static

from ember_code.frontend.tui.widgets import PromptInput

if TYPE_CHECKING:
    from ember_code.frontend.tui.app import EmberApp

logger = logging.getLogger(__name__)


# ── Command mode ──────────────────────────────────────────────


def update_command_mode_indicator(app: "EmberApp") -> None:
    """Update the prompt indicator and placeholder for command mode."""
    try:
        indicator = app.query_one("#prompt-indicator", Static)
        input_widget = app.query_one("#user-input", PromptInput)
        if app._command_mode:
            indicator.update("[bold cyan]/ [/bold cyan]")
            input_widget.placeholder = "Command name (Esc to return to chat)"
        else:
            indicator.update("> ")
            input_widget.placeholder = "Type a message or /help"
    except NoMatches:
        pass


def exit_command_mode(app: "EmberApp") -> None:
    """Exit command mode and return to chat."""
    app._command_mode = False
    update_command_mode_indicator(app)
    with contextlib.suppress(NoMatches):
        app.query_one("#user-input", PromptInput).clear()


# ── Shell mode ────────────────────────────────────────────────


def update_shell_mode_indicator(app: "EmberApp") -> None:
    """Update the prompt indicator and placeholder for shell mode."""
    try:
        indicator = app.query_one("#prompt-indicator", Static)
        input_widget = app.query_one("#user-input", PromptInput)
        if app._shell_mode:
            indicator.update("[bold $warning]$ [/bold $warning]")
            input_widget.placeholder = "Shell command (Esc to return to chat)"
        else:
            indicator.update("> ")
            input_widget.placeholder = "Type a message or /help"
    except NoMatches:
        pass


def exit_shell_mode(app: "EmberApp") -> None:
    """Exit shell mode and return to chat."""
    app._shell_mode = False
    update_shell_mode_indicator(app)
    with contextlib.suppress(NoMatches):
        app.query_one("#user-input", PromptInput).clear()


# ── Inline shell execution ────────────────────────────────────


async def run_shell_inline(app: "EmberApp", cmd: str) -> None:
    """Run a shell command inline, show output, and add to
    conversation context.

    The command and output are stored so the AI sees them as
    context in the next message, but no AI response is
    triggered.
    """
    if not cmd:
        return

    app._conversation.append_user(f"$ {cmd}")

    # Mount a live output widget that updates as lines arrive.
    output_widget = Static("[dim]...[/dim]", classes="info-message")
    app._conversation.append(output_widget)
    lines: list[str] = []

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(app._project_dir) if app._project_dir else None,
            start_new_session=True,
        )
        app._shell_proc = proc

        try:
            assert proc.stdout is not None
            while True:
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=0.5)
                except asyncio.TimeoutError:
                    if proc.returncode is not None:
                        break
                    continue
                if not raw:
                    break
                line = raw.decode(errors="replace").rstrip()
                lines.append(line)
                # Show last 50 lines in the live widget (escape
                # Rich markup so a stray ``[bold]`` in program
                # output doesn't corrupt the render).
                visible = escape("\n".join(lines[-50:]))
                output_widget.update(f"[dim]{visible}[/dim]")
                # Auto-scroll.
                try:
                    container = app.query_one("#conversation")
                    container.scroll_end(animate=False)
                except NoMatches:
                    pass
            await proc.wait()
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            lines.append("(cancelled)")

        exit_code = proc.returncode or 0
    except Exception as e:
        lines.append(f"(error: {e})")
        exit_code = -1
    finally:
        app._shell_proc = None

    # Final update with all output.
    output = "\n".join(lines)
    if output:
        output_widget.update(f"[dim]{escape(output)}[/dim]")
    else:
        output_widget.update("[dim](no output)[/dim]")
    if exit_code != 0 and exit_code != -1:
        app._conversation.append_info(f"Exit code: {exit_code}")

    # Store for AI context.
    app._shell_context.append(f"$ {cmd}\n{output}")
