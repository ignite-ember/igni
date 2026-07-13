"""Streaming assistant-message renderer — chunk-by-chunk markdown.

Extracted from ``_messages.py`` (iter 41) per Pattern 8. Buffers
incoming chunks and re-renders the Markdown widget at most once
per ``RENDER_INTERVAL`` so the UI stays responsive under high
LLM chunk rates.
"""

from __future__ import annotations

import logging

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Markdown, Static

logger = logging.getLogger(__name__)


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
