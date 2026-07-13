"""Permission dialog — modal prompt for tool-use approval.

Extracted from ``_dialogs.py`` (iter 34) per Pattern 8: small
modules, one responsibility. Final per-dialog extraction; after
this iter, ``_dialogs.py`` is a thin re-export shim.

The future in ``__init__`` (not ``wait_for_decision``) is
load-bearing: a click that lands between ``mount()`` and the
await used to silently drop the user's choice because the future
wasn't created yet. See ``__init__``'s comment.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from ember_code.frontend.tui.widgets._dialogs_common import _is_inside

logger = logging.getLogger(__name__)


class PermissionDialog(Widget):
    """Modal permission prompt with vertical option list.

    Navigate with Up/Down arrows, confirm with Enter.
    """

    _OPTIONS = [
        ("once", "Allow once"),
        ("always", "Always allow"),
        ("similar", "Allow similar"),
        ("deny", "Deny"),
    ]

    can_focus = True

    DEFAULT_CSS = """
    PermissionDialog {
        layer: dialog;
        dock: bottom;
        width: 100%;
        height: auto;
        max-height: 60%;
        background: $surface-darken-1;
        border-top: heavy $warning;
        padding: 0 2;
    }

    PermissionDialog .perm-header {
        height: auto;
        width: 100%;
    }

    PermissionDialog .title {
        text-style: bold;
        color: $warning;
    }

    PermissionDialog .description {
        color: $text;
    }

    PermissionDialog .option-list {
        height: auto;
        margin-top: 1;
    }

    PermissionDialog .option {
        padding: 0 1;
        height: 1;
    }

    PermissionDialog .option.-selected {
        background: $accent;
        color: $text;
        text-style: bold;
    }

    PermissionDialog .hint {
        color: $text-muted;
        margin-top: 1;
    }
    """

    class Approved(Message):
        def __init__(self, choice: str):
            self.choice = choice
            super().__init__()

    class Denied(Message):
        pass

    selected_index = reactive(0)

    def __init__(self, tool_name: str, details: str = "", description: str = ""):
        super().__init__()
        self._tool_name = tool_name
        self._full_details = details or description
        self._details_expanded = False
        # Create the future eagerly so a click that lands BEFORE
        # ``wait_for_decision`` runs still records its result. Previously
        # ``self._decision`` was None until ``wait_for_decision`` was
        # awaited, so a fast click between mount() and the await silently
        # dropped the user's choice — the future was created later, no
        # one set it, and the await hung forever.
        self._decision: asyncio.Future = asyncio.get_event_loop().create_future()
        self.last_choice: str = "deny"

    def _render_details(self) -> str:
        raw = self._full_details
        if not raw:
            return ""
        safe = raw.replace("[", "\\[")
        lines = safe.splitlines()
        if self._details_expanded or len(lines) <= 3:
            return safe
        preview = "\n".join(lines[:3])
        return (
            f"{preview}\n[dim italic]... ({len(lines) - 3} more lines — Tab to expand)[/dim italic]"
        )

    def compose(self) -> ComposeResult:
        yield Static("", id="perm-title", classes="title")
        with Vertical(classes="option-list"):
            for i, (_key, label) in enumerate(self._OPTIONS):
                cls = "option -selected" if i == 0 else "option"
                yield Static(f"  {label}", id=f"opt-{i}", classes=cls)
        yield Static(
            "[dim]↑/↓ select · Enter confirm · Tab expand/collapse · Esc deny[/dim]",
            classes="hint",
        )

    def on_mount(self) -> None:
        self._update_title()

    def _update_title(self) -> None:
        details = self._render_details()
        with contextlib.suppress(Exception):
            self.query_one("#perm-title", Static).update(
                f"[bold $warning]  {self._tool_name}[/bold $warning]\n[dim]{details}[/dim]"
            )

    def watch_selected_index(self, old: int, new: int) -> None:
        """Update visual selection when index changes."""
        try:
            old_widget = self.query_one(f"#opt-{old}", Static)
            old_widget.remove_class("-selected")
            new_widget = self.query_one(f"#opt-{new}", Static)
            new_widget.add_class("-selected")
        except Exception as exc:
            logger.debug("Failed to update permission dialog selection: %s", exc)

    def on_key(self, event) -> None:
        event.stop()
        event.prevent_default()
        if event.key == "up":
            self.selected_index = max(0, self.selected_index - 1)
        elif event.key == "down":
            self.selected_index = min(len(self._OPTIONS) - 1, self.selected_index + 1)
        elif event.key == "enter":
            self._confirm_selection()
        elif event.key == "tab":
            self._details_expanded = not self._details_expanded
            self._update_title()
            self.refresh(layout=True)
        elif event.key == "escape":
            self.post_message(self.Denied())
            if self._decision and not self._decision.done():
                self._decision.set_result(False)
            self.remove()

    def on_click(self, event) -> None:
        """Allow clicking an option to select and confirm."""
        # Check if the click target is one of the option widgets
        target = event.widget if hasattr(event, "widget") else None
        if target is None:
            return
        for i in range(len(self._OPTIONS)):
            try:
                widget = self.query_one(f"#opt-{i}", Static)
                # ``Widget`` doesn't expose ``is_descendant_of`` —
                # walk the target's ancestor chain manually instead.
                if target is widget or _is_inside(target, widget):
                    self.selected_index = i
                    self._confirm_selection()
                    return
            except Exception as exc:
                logger.debug("Failed to match click to permission option #opt-%d: %s", i, exc)

    def _confirm_selection(self) -> None:
        key, _label = self._OPTIONS[self.selected_index]
        self.last_choice = key
        if key == "deny":
            self.post_message(self.Denied())
            if self._decision and not self._decision.done():
                self._decision.set_result(False)
        else:
            self.post_message(self.Approved(key))
            if self._decision and not self._decision.done():
                self._decision.set_result(True)
        self.remove()

    async def wait_for_decision(self) -> bool:
        """Block until the user makes a choice. Returns True if approved.

        The future is created in ``__init__`` (not here) so a click that
        races ahead of ``wait_for_decision`` still resolves correctly.
        If the future has already been resolved by an early click, this
        returns immediately.
        """
        return await self._decision
