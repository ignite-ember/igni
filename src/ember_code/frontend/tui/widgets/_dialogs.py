"""Modal/overlay widgets: permission dialog, session picker, model picker, login."""

import asyncio
import contextlib
import logging
from datetime import datetime

from pydantic import BaseModel
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

logger = logging.getLogger(__name__)


def _is_inside(target: Widget, container: Widget) -> bool:
    """True if ``target`` is a descendant of ``container``.

    Textual's ``Widget`` doesn't expose ``is_descendant_of``, so we walk
    the parent chain ourselves. The previous code called the missing
    method, swallowed the AttributeError, and silently dropped clicks.
    """
    node = getattr(target, "parent", None)
    while node is not None:
        if node is container:
            return True
        node = getattr(node, "parent", None)
    return False


class SessionInfo(BaseModel):
    """Lightweight session metadata for the picker UI."""

    session_id: str
    name: str = ""
    created_at: int = 0
    updated_at: int = 0
    run_count: int = 0
    summary: str = ""
    agent_name: str = ""

    @property
    def display_name(self) -> str:
        """Session name, falling back to the session_id."""
        return self.name or self.session_id

    @property
    def display_time(self) -> str:
        """Human-readable timestamp."""
        ts = self.updated_at or self.created_at
        if not ts:
            return "unknown"
        dt = datetime.fromtimestamp(ts)
        now = datetime.now()
        delta = now - dt
        if delta.days == 0:
            return dt.strftime("%H:%M")
        if delta.days == 1:
            return "yesterday"
        if delta.days < 7:
            return f"{delta.days}d ago"
        return dt.strftime("%Y-%m-%d")

    @property
    def label(self) -> str:
        """Two-part label: name line + summary line."""
        parts = [f"[bold]{self.display_name}[/bold]"]
        parts.append(f"[dim]{self.display_time}[/dim]")
        if self.run_count:
            parts.append(f"[dim]{self.run_count} runs[/dim]")
        line1 = "  ".join(parts)

        if self.summary:
            short = self.summary[:80]
            if len(self.summary) > 80:
                short += "..."
            return f"{line1}\n    [dim italic]{short}[/dim italic]"
        return line1


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


class LoginWidget(Widget):
    """Bottom-docked login status display. Pure display — all logic lives on BE."""

    can_focus = True

    DEFAULT_CSS = """
    LoginWidget {
        layer: dialog;
        dock: bottom;
        width: 100%;
        height: auto;
        max-height: 10;
        background: $surface-darken-1;
        border-top: heavy $accent;
        padding: 0 2;
    }

    LoginWidget .login-title {
        text-style: bold;
        color: $accent;
    }

    LoginWidget .login-status {
        color: $text-muted;
    }

    LoginWidget .hint {
        color: $text-muted;
    }
    """

    class LoggedIn(Message):
        """Posted on successful login."""

        def __init__(self, email: str):
            self.email = email
            super().__init__()

    class Cancelled(Message):
        """Posted when the user cancels login."""

        pass

    def __init__(self, backend=None):
        super().__init__()
        self._backend = backend

    def compose(self) -> ComposeResult:
        yield Static("[bold $accent]Login to Ember Cloud[/bold $accent]", classes="login-title")
        yield Static("Starting...", classes="login-status", id="login-status")
        yield Static("", classes="login-status", id="login-url")
        yield Static("[dim]Esc to cancel[/dim]", classes="hint")

    def on_key(self, event) -> None:
        event.stop()
        event.prevent_default()
        if event.key == "escape":
            self.cancel()

    def cancel(self) -> None:
        """Send cancel to BE and remove widget."""
        if self._backend:
            self._backend.cancel_login()
        self.post_message(self.Cancelled())
        self.remove()

    def update_status(self, text: str) -> None:
        """Called by app when login_status push arrives."""
        import re

        with contextlib.suppress(Exception):
            status = self.query_one("#login-status", Static)
            url_widget = self.query_one("#login-url", Static)
            if "http" in text:
                urls = re.findall(r"https?://\S+", text)
                if urls:
                    url_widget.update(f"[bold]URL:[/bold] {urls[-1]}")
                    lines = [line for line in text.splitlines() if "http" not in line]
                    status.update(f"[dim]{chr(10).join(lines)}[/dim]")
                    return
            status.update(f"[dim]{text}[/dim]")

    def show_result(self, success: bool, result: str) -> None:
        """Called by app when login_result push arrives."""
        with contextlib.suppress(Exception):
            if success:
                self.post_message(self.LoggedIn(result))
                self.remove()
            else:
                self.query_one("#login-status", Static).update(f"[red]{result}[/red]")
