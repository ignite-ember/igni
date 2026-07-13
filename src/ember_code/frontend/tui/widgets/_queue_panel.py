"""Queue panel — visible list of messages the user has queued.

Extracted from ``_chrome.py`` (iter 37) per Pattern 8. Shown at
the bottom of the composer when items are queued; hidden
otherwise. Arrow-keys navigate, Delete removes, Enter edits,
Escape closes.
"""

from __future__ import annotations

import logging

from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

logger = logging.getLogger(__name__)


class QueuePanel(Widget):
    """Interactive panel showing queued messages."""

    can_focus = True

    DEFAULT_CSS = """
    QueuePanel {
        dock: bottom;
        height: auto;
        max-height: 10;
        border-top: solid $accent;
        padding: 0 1;
    }

    QueuePanel.-hidden {
        display: none;
    }

    QueuePanel .queue-header {
        color: $accent;
        text-style: bold;
        height: 1;
    }

    QueuePanel .queue-item {
        height: 1;
        padding: 0 1;
    }

    QueuePanel .queue-item.-selected {
        background: $accent 30%;
        text-style: bold;
    }

    QueuePanel .queue-hint {
        color: $text-muted;
        height: 1;
    }
    """

    class ItemDeleted(Message):
        """Posted when a queue item is deleted."""

        def __init__(self, index: int):
            self.index = index
            super().__init__()

    class ItemEditRequested(Message):
        """Posted when the user wants to edit a queue item."""

        def __init__(self, index: int, text: str):
            self.index = index
            self.text = text
            super().__init__()

    class PanelClosed(Message):
        """Posted when the user closes the panel with Escape."""

        pass

    selected_index = reactive(0)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._items: list[str] = []
        self.add_class("-hidden")

    def refresh_items(self, items: list[str]) -> None:
        """Update the displayed queue items."""
        self._items = list(items)
        if not self._items:
            self.add_class("-hidden")
            self.remove_children()
            return
        self.remove_class("-hidden")
        # Rebuild children FIRST so they exist when the watcher queries
        # them, then clamp selected_index. Reverse order would fire
        # watch_selected_index against a tree that hasn't rebuilt yet.
        self._rebuild()
        clamped = min(self.selected_index, max(0, len(self._items) - 1))
        if clamped != self.selected_index:
            self.selected_index = clamped

    def _rebuild(self) -> None:
        """Rebuild child widgets from current items."""
        self.remove_children()
        if not self._items:
            return
        self.mount(
            Static(
                f"[bold $accent]Queue ({len(self._items)})[/bold $accent]"
                "  [dim]↑↓ navigate  Del remove  Enter edit  Esc close[/dim]",
                classes="queue-header",
            )
        )
        for i, text in enumerate(self._items):
            first_line = text.split("\n", 1)[0].strip()
            preview = first_line if len(first_line) <= 50 else first_line[:47] + "..."
            cls = "queue-item -selected" if i == self.selected_index else "queue-item"
            self.mount(Static(f"  {i + 1}. {preview}", id=f"q-{i}", classes=cls))

    def watch_selected_index(self, old: int, new: int) -> None:
        # No children to update when the panel is empty/hidden. The reactive
        # still fires (e.g. clamping in refresh_items), so bail early instead
        # of spamming "no nodes match" debug logs.
        if not self._items or not self.is_mounted:
            return
        try:
            old_w = self.query_one(f"#q-{old}", Static)
            old_w.remove_class("-selected")
        except Exception as exc:
            logger.debug("Failed to deselect queue item #q-%d: %s", old, exc)
        try:
            new_w = self.query_one(f"#q-{new}", Static)
            new_w.add_class("-selected")
        except Exception as exc:
            logger.debug("Failed to select queue item #q-%d: %s", new, exc)

    def on_key(self, event) -> None:
        if not self._items:
            return
        event.stop()
        event.prevent_default()

        if event.key == "up":
            self.selected_index = max(0, self.selected_index - 1)
        elif event.key == "down":
            self.selected_index = min(len(self._items) - 1, self.selected_index + 1)
        elif event.key in ("delete", "backspace"):
            if 0 <= self.selected_index < len(self._items):
                self.post_message(self.ItemDeleted(self.selected_index))
        elif event.key == "enter":
            if 0 <= self.selected_index < len(self._items):
                self.post_message(
                    self.ItemEditRequested(self.selected_index, self._items[self.selected_index])
                )
        elif event.key == "escape":
            self.post_message(self.PanelClosed())

    def on_click(self, event) -> None:
        """Click a queue item to select it."""
        target = event.widget if hasattr(event, "widget") else None
        if target is None:
            return
        for i in range(len(self._items)):
            try:
                widget = self.query_one(f"#q-{i}", Static)
                if target is widget or target.is_descendant_of(widget):
                    self.selected_index = i
                    return
            except Exception as exc:
                logger.debug("Failed to match click to queue item #q-%d: %s", i, exc)
