"""Session picker dialog — bottom-docked session selector.

Extracted from ``_dialogs.py`` (iter 33) per Pattern 8: small
modules, one responsibility. Navigate with Up/Down, confirm with
Enter, cancel with Escape. Click an entry to select it directly.
"""

from __future__ import annotations

import contextlib

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from ember_code.frontend.tui.widgets._dialogs_common import _is_inside
from ember_code.frontend.tui.widgets._session_info import SessionInfo


class SessionPickerWidget(Widget):
    """Bottom-docked session picker.

    Navigate with Up/Down arrows, confirm with Enter, cancel with Escape.
    Click an entry to select it.
    """

    can_focus = True

    DEFAULT_CSS = """
    SessionPickerWidget {
        layer: dialog;
        dock: bottom;
        width: 100%;
        height: auto;
        max-height: 20;
        background: $surface-darken-1;
        border-top: heavy $accent;
        padding: 0 2;
    }

    SessionPickerWidget .picker-title {
        text-style: bold;
        color: $accent;
    }

    SessionPickerWidget .session-list {
        height: auto;
        max-height: 14;
        overflow-y: auto;
    }

    SessionPickerWidget .session-entry {
        padding: 0 1;
        height: auto;
    }

    SessionPickerWidget .session-entry.-selected {
        background: $accent;
        color: $text;
        text-style: bold;
    }

    SessionPickerWidget .session-entry.-current {
        color: $success;
    }

    SessionPickerWidget .empty-msg {
        color: $text-muted;
        padding: 1 0;
    }

    SessionPickerWidget .hint {
        color: $text-muted;
        margin-top: 1;
    }
    """

    class Selected(Message):
        """Posted when the user picks a session."""

        def __init__(self, session_id: str):
            self.session_id = session_id
            super().__init__()

    class Cancelled(Message):
        """Posted when the user cancels the picker."""

        pass

    selected_index = reactive(0)

    def __init__(self, sessions: list[SessionInfo], current_session_id: str = ""):
        super().__init__()
        self._sessions = sessions
        self._current_session_id = current_session_id

    def compose(self) -> ComposeResult:
        yield Static("[bold $accent]Select Session[/bold $accent]", classes="picker-title")
        with Vertical(classes="session-list"):
            if not self._sessions:
                yield Static("No previous sessions found.", classes="empty-msg")
            else:
                for i, info in enumerate(self._sessions):
                    classes = ["session-entry"]
                    if i == 0:
                        classes.append("-selected")
                    if info.session_id == self._current_session_id:
                        classes.append("-current")
                    yield Static(info.label, id=f"sess-{i}", classes=" ".join(classes))
        yield Static("[dim]↑/↓ to select · Enter to confirm · Esc to cancel[/dim]", classes="hint")

    def watch_selected_index(self, old: int, new: int) -> None:
        try:
            old_widget = self.query_one(f"#sess-{old}", Static)
            old_widget.remove_class("-selected")
            new_widget = self.query_one(f"#sess-{new}", Static)
            new_widget.add_class("-selected")
            # Keep the highlighted row visible — arrow nav past the
            # viewport would otherwise hide the selection.
            with contextlib.suppress(Exception):
                new_widget.scroll_visible(animate=False)
        except Exception:
            pass

    def on_key(self, event) -> None:
        event.stop()
        event.prevent_default()
        if not self._sessions:
            if event.key in ("escape", "enter"):
                self.post_message(self.Cancelled())
                self.remove()
            return

        if event.key == "up":
            self.selected_index = max(0, self.selected_index - 1)
        elif event.key == "down":
            self.selected_index = min(len(self._sessions) - 1, self.selected_index + 1)
        elif event.key == "enter":
            session = self._sessions[self.selected_index]
            self.post_message(self.Selected(session.session_id))
            self.remove()
        elif event.key == "escape":
            self.post_message(self.Cancelled())
            self.remove()

    def on_click(self, event) -> None:
        """Click an entry to select and confirm."""
        target = event.widget if hasattr(event, "widget") else None
        if target is None:
            return
        for i in range(len(self._sessions)):
            try:
                widget = self.query_one(f"#sess-{i}", Static)
                if target is widget or _is_inside(target, widget):
                    self.selected_index = i
                    session = self._sessions[i]
                    self.post_message(self.Selected(session.session_id))
                    self.remove()
                    return
            except Exception:
                pass
