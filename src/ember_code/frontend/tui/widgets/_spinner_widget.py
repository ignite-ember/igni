"""Spinner widget — activity indicator during agent runs.

Extracted from ``_chrome.py`` (iter 36) per Pattern 8: small
modules, one responsibility. Token counts + elapsed time live on
the ``StatusBar``; this widget is just the animated label.
"""

from __future__ import annotations

from textual.timer import Timer
from textual.widgets import Static

from ember_code.frontend.tui.widgets._constants import SPINNER_FRAMES
from ember_code.frontend.tui.widgets._formatting import format_token_count


class SpinnerWidget(Static):
    """Claude Code-style activity indicator.

    Keeps it simple — just a label with animated dots.
    All token/time stats live in the footer StatusBar.
    """

    DEFAULT_CSS = """
    SpinnerWidget {
        height: 1;
        margin: 0 0 0 2;
    }
    """

    def __init__(self, label: str = "Thinking"):
        self._label = label
        self._frame = 0
        self._tokens: int = 0
        self._timer: Timer | None = None
        super().__init__(self._format())

    def on_mount(self) -> None:
        self._timer = self.set_interval(1 / 12, self._tick)

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(SPINNER_FRAMES)
        self.update(self._format())

    def render_text(self) -> str:
        """Plain-text render used by tests and direct inspection."""
        frame = SPINNER_FRAMES[self._frame]
        text = f"{frame} {self._label}..."
        if self._tokens > 0:
            text += f"  {format_token_count(self._tokens)} tokens"
        return text

    def _format(self) -> str:
        frame = SPINNER_FRAMES[self._frame]
        if self._label == "Thinking":
            return f"[dim]{frame} Thinking...[/dim]"
        return f"[bold $accent]{frame} {self._label}...[/bold $accent]"

    def set_label(self, label: str) -> None:
        self._label = label
        self.update(self._format())

    def set_tokens(self, tokens: int) -> None:
        self._tokens = tokens

    def stop(self) -> None:
        if self._timer:
            self._timer.stop()
            self._timer = None
