"""Static conversation-message renderer — user + assistant bubbles.

Extracted from ``_messages.py`` (iter 41) per Pattern 8: small
modules, one responsibility. Long messages truncate by default;
click to expand or use Ctrl+O for expand-all.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Markdown, Static


class MessageWidget(Widget):
    """Displays a conversation message (user or assistant).

    Long messages are truncated by default. Click the 'Show more' label
    or use Ctrl+O (expand all) to reveal the full content.
    """

    DEFAULT_CSS = """
    MessageWidget {
        height: auto;
        margin: 0 0 1 0;
        padding: 0;
    }

    MessageWidget .message-row {
        height: auto;
        width: 100%;
    }

    MessageWidget .role-label {
        width: 2;
        height: auto;
        text-style: bold;
    }

    MessageWidget .role-user {
        color: ansi_bright_blue;
    }

    MessageWidget .role-assistant {
        color: ansi_yellow;
    }

    MessageWidget .message-body {
        width: 1fr;
        height: auto;
    }

    MessageWidget .message-content {
        padding: 0;
    }

    MessageWidget .message-content-full {
        padding: 0;
        display: none;
    }

    MessageWidget .show-more {
        color: $accent;
        text-style: italic;
    }

    MessageWidget.-expanded .message-content {
        display: none;
    }

    MessageWidget.-expanded .message-content-full {
        display: block;
    }

    MessageWidget.-expanded .show-more {
        display: none;
    }
    """

    expanded = reactive(False)

    def __init__(
        self, content: str, role: str = "user", truncate_lines: int = 10, expanded: bool = False
    ):
        super().__init__()
        self._content = content
        self._role = role
        self._truncate_lines = truncate_lines
        self._is_long = len(content.splitlines()) > self._truncate_lines
        if expanded and self._is_long:
            self.expanded = True
            self.add_class("-expanded")

    @property
    def is_long(self) -> bool:
        """Whether this message exceeds the truncation threshold."""
        return self._is_long

    def compose(self) -> ComposeResult:
        content = self._content
        if self._role == "user":
            if content.startswith("$ "):
                role_display = "$ "
                content = content[2:]
            elif content.startswith("/"):
                role_display = "/ "
                content = content[1:]
            else:
                role_display = "> "
        else:
            role_display = "● "
            content = self._content
        role_class = f"role-{self._role}"

        with Horizontal(classes="message-row"):
            yield Static(f"[bold]{role_display}[/bold]", classes=f"role-label {role_class}")
            with Vertical(classes="message-body"):
                if not self._is_long:
                    if self._role == "assistant":
                        yield Markdown(content, classes="message-content")
                    else:
                        # ``markup=False`` for user content: it's raw
                        # input (could contain ``[/loop ...]``, code
                        # snippets, BBCode-shaped strings, etc.) that
                        # Textual would otherwise parse as markup and
                        # crash with ``MarkupError`` on the first
                        # unbalanced bracket. Plain-text rendering is
                        # what we want for human input anyway.
                        yield Static(content, classes="message-content", markup=False)
                else:
                    truncated = "\n".join(content.splitlines()[: self._truncate_lines])

                    if self._role == "assistant":
                        yield Markdown(truncated, classes="message-content")
                        yield Markdown(content, classes="message-content-full")
                    else:
                        yield Static(truncated, classes="message-content", markup=False)
                        yield Static(content, classes="message-content-full", markup=False)

                    lines_hidden = len(self._content.splitlines()) - self._truncate_lines
                    yield Static(
                        f"[dim italic]... {lines_hidden} more lines — click to expand[/dim italic]",
                        classes="show-more",
                    )

    def on_click(self) -> None:
        if self._is_long:
            self.toggle_expanded()

    def toggle_expanded(self) -> None:
        self.expanded = not self.expanded
        self.toggle_class("-expanded")

    def set_expanded(self, value: bool) -> None:
        if self._is_long and value != self.expanded:
            self.toggle_expanded()
