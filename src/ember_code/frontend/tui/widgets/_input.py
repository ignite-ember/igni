"""Input history and prompt widget for igni TUI."""

from textual.message import Message
from textual.widgets import TextArea


class PromptInput(TextArea):
    """Multiline input: Enter submits, \\+Enter inserts a newline.

    Multiline text can also be pasted directly.

    Grows up to ``MAX_VISIBLE_ROWS`` rendered rows as the user adds
    lines; beyond that TextArea's internal ScrollView keeps the
    cursor in view and the user navigates with arrow keys. Growth
    happens upward — the ``#footer`` parent is ``dock: bottom; height:
    auto`` so its bottom edge stays anchored to the screen bottom
    and the conversation area shrinks instead of the footer
    overflowing past the viewport. (An earlier attempt with
    ``max-height: 8`` predated the footer collapsing tip-bar +
    status-bar into a single docked container, so growth back then
    pushed the status-bar past the screen edge. That's no longer
    possible — the whole chrome rides up together.)
    """

    MAX_VISIBLE_ROWS = 10

    suppress_submit: bool = False

    DEFAULT_CSS = """
    PromptInput {
        height: auto;
        min-height: 1;
        max-height: 10;
        border: none;
        padding: 0;
        /* Thin vertical scrollbar appears only when content
           exceeds ``max-height``; horizontal stays hidden since we
           soft-wrap. */
        scrollbar-size: 1 0;
    }
    PromptInput:focus {
        border: none;
    }
    PromptInput .text-area--placeholder {
        color: $text-muted;
    }
    """

    class Submitted(Message):
        """Posted when the user presses Enter to submit."""

        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    async def _on_key(self, event) -> None:
        if event.key == "enter":
            # When the file picker is open, don't submit — let the event
            # bubble up to the app's on_key where picker selection happens
            if self.suppress_submit:
                event.prevent_default()
                return
            row, col = self.cursor_location
            line = self.document.get_line(row)
            if col > 0 and line[col - 1] == "\\":
                # Backslash + Enter = newline
                event.prevent_default()
                event.stop()
                self.action_delete_left()
                self.insert("\n")
                return
            # Plain Enter = submit
            event.prevent_default()
            event.stop()
            text = self.text.strip()
            if text:
                self.post_message(self.Submitted(text))
            return
        await super()._on_key(event)


class InputHistory:
    """Tracks input history for Up/Down arrow navigation."""

    def __init__(self, max_size: int = 100):
        self._history: list[str] = []
        self._index: int = -1
        self._draft: str = ""
        self._max_size = max_size

    @property
    def history(self) -> list[str]:
        return list(self._history)

    def push(self, text: str) -> None:
        """Add an entry to history."""
        text = text.strip()
        if not text:
            return
        # Avoid consecutive duplicates
        if self._history and self._history[-1] == text:
            self._reset_index()
            return
        self._history.append(text)
        if len(self._history) > self._max_size:
            self._history.pop(0)
        self._reset_index()

    def navigate_up(self, current_text: str = "") -> str | None:
        """Move up in history. Returns the history entry or None if at top."""
        if not self._history:
            return None
        if self._index == -1:
            # Entering history — save current input as draft
            self._draft = current_text
            self._index = len(self._history) - 1
        elif self._index > 0:
            self._index -= 1
        else:
            return None  # Already at oldest
        return self._history[self._index]

    def navigate_down(self) -> str | None:
        """Move down in history. Returns the entry, draft, or None."""
        if self._index == -1:
            return None  # Not navigating
        if self._index < len(self._history) - 1:
            self._index += 1
            return self._history[self._index]
        else:
            # Past the newest — restore draft
            draft = self._draft
            self._reset_index()
            return draft

    def _reset_index(self) -> None:
        self._index = -1
        self._draft = ""

    @property
    def is_navigating(self) -> bool:
        return self._index != -1
