"""Model picker dialog — bottom-docked model selector.

Extracted from ``_dialogs.py`` (iter 32) per Pattern 8: small
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


class ModelPickerWidget(Widget):
    """Bottom-docked model picker.

    Navigate with Up/Down arrows, confirm with Enter, cancel with Escape.
    Click an entry to select it.
    """

    can_focus = True

    DEFAULT_CSS = """
    ModelPickerWidget {
        layer: dialog;
        dock: bottom;
        width: 100%;
        height: auto;
        max-height: 20;
        background: $surface-darken-1;
        border-top: heavy $accent;
        padding: 0 2;
    }

    ModelPickerWidget .picker-title {
        text-style: bold;
        color: $accent;
    }

    ModelPickerWidget .model-list {
        height: auto;
        max-height: 14;
        overflow-y: auto;
    }

    ModelPickerWidget .model-entry {
        padding: 0 1;
        height: 1;
    }

    ModelPickerWidget .model-entry.-selected {
        background: $accent;
        color: $text;
        text-style: bold;
    }

    ModelPickerWidget .model-entry.-current {
        color: $success;
    }

    ModelPickerWidget .hint {
        color: $text-muted;
        margin-top: 1;
    }
    """

    class Selected(Message):
        """Posted when the user picks a model."""

        def __init__(self, model_name: str):
            self.model_name = model_name
            super().__init__()

    class Cancelled(Message):
        pass

    selected_index = reactive(0)

    def __init__(self, models: list[str], current_model: str = ""):
        super().__init__()
        self._models = models
        self._current_model = current_model
        # Pre-select the current model
        if current_model in models:
            self.selected_index = models.index(current_model)

    def compose(self) -> ComposeResult:
        yield Static("[bold $accent]Select Model[/bold $accent]", classes="picker-title")
        with Vertical(classes="model-list"):
            for i, name in enumerate(self._models):
                classes = ["model-entry"]
                if i == self.selected_index:
                    classes.append("-selected")
                if name == self._current_model:
                    classes.append("-current")
                    label = f"  {name} [dim](current)[/dim]"
                else:
                    label = f"  {name}"
                yield Static(label, id=f"model-{i}", classes=" ".join(classes))
        yield Static("[dim]↑/↓ to select · Enter to confirm · Esc to cancel[/dim]", classes="hint")

    def watch_selected_index(self, old: int, new: int) -> None:
        try:
            old_widget = self.query_one(f"#model-{old}", Static)
            old_widget.remove_class("-selected")
            new_widget = self.query_one(f"#model-{new}", Static)
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
        if event.key == "up":
            self.selected_index = max(0, self.selected_index - 1)
        elif event.key == "down":
            self.selected_index = min(len(self._models) - 1, self.selected_index + 1)
        elif event.key == "enter":
            if self._models:
                self.post_message(self.Selected(self._models[self.selected_index]))
            self.remove()
        elif event.key == "escape":
            self.post_message(self.Cancelled())
            self.remove()

    def on_click(self, event) -> None:
        """Click an entry to select and confirm."""
        target = event.widget if hasattr(event, "widget") else None
        if target is None:
            return
        for i in range(len(self._models)):
            try:
                widget = self.query_one(f"#model-{i}", Static)
                if target is widget or _is_inside(target, widget):
                    self.selected_index = i
                    self.post_message(self.Selected(self._models[i]))
                    self.remove()
                    return
            except Exception:
                pass
