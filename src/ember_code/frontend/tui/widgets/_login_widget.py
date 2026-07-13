"""Login dialog widget — displays Ember Cloud OAuth flow status.

Extracted from ``_dialogs.py`` (iter 31) per Pattern 8: small
modules, one responsibility. All logic lives on the backend; this
widget is pure display + escape-to-cancel plumbing.
"""

from __future__ import annotations

import contextlib
import re

from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static


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
