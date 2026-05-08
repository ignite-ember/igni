"""Input handler — manages user input, history, and autocomplete."""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ember_code.frontend.tui.widgets import InputHistory

if TYPE_CHECKING:
    from ember_code.frontend.tui.file_index import FileIndex

# Matches @path tokens: @ at start of line or after whitespace, followed by
# non-whitespace path characters.  Does NOT match email-style user@domain
# (requires whitespace or ^ before @).
_AT_MENTION_RE = re.compile(r"(?:^|(?<=\s))@(\S+)")


def process_file_mentions(text: str) -> tuple[str, list[str]]:
    """Strip @file mentions from message text and return referenced paths.

    Returns (cleaned_text, referenced_paths).  The ``@`` prefix is removed
    so the agent sees a natural file path.  A hint line is prepended when
    files are referenced.
    """
    paths: list[str] = []

    def _replace(m: re.Match) -> str:
        path = m.group(1)
        paths.append(path)
        return path  # strip the @ prefix, keep the path

    cleaned = _AT_MENTION_RE.sub(_replace, text)

    if paths:
        hint = "[Referenced files: " + ", ".join(paths) + " — read before responding]"
        cleaned = hint + "\n" + cleaned

    return cleaned, paths


# ── Platform-aware key labels ────────────────────────────────────

_IS_MACOS = sys.platform == "darwin"


def shortcut_label(key: str) -> str:
    """Return a platform-appropriate shortcut label.

    On macOS: Ctrl+D → ⌃D, on others: Ctrl+D.
    """
    if _IS_MACOS:
        # Map common modifier+key combos to Mac symbols
        if key.startswith("Ctrl+"):
            return f"⌃{key[5:]}"
        if key.startswith("Shift+"):
            return f"⇧{key[6:]}"
    return key


SHORTCUT_HELP = (
    "## Keyboard Shortcuts\n"
    f"- `{shortcut_label('Enter')}` — send message\n"
    f"- `\\` + `{shortcut_label('Enter')}` — new line\n"
    f"- `{shortcut_label('Ctrl+D')}` — quit\n"
    f"- `{shortcut_label('Ctrl+L')}` — clear screen\n"
    f"- `{shortcut_label('Ctrl+O')}` — expand/collapse all messages\n"
    f"- `{shortcut_label('Ctrl+V')}` — toggle verbose mode\n"
    f"- `{shortcut_label('Up/Down')}` — input history\n"
    f"- `{shortcut_label('Escape')}` — cancel\n"
)


class AutocompleteProvider:
    """Resolves slash-command completions from built-in commands and skills."""

    BUILTIN_COMMANDS = (
        "/help",
        "/quit",
        "/exit",
        "/agents",
        "/skills",
        "/hooks",
        "/sessions",
        "/rename",
        "/memory",
        "/knowledge",
        "/clear",
        "/config",
        "/model",
        "/mcp",
        "/codeindex",
        "/compact",
        "/schedule",
        "/login",
        "/logout",
        "/whoami",
        "/bug",
        "/evals",
        "/sync-knowledge",
    )

    def __init__(self, skill_pool: Any | None = None):
        self._skill_pool = skill_pool

    def complete(self, text: str) -> list[str]:
        """Return matching slash commands for the given partial input."""
        if not text.startswith("/") or text.startswith("//"):
            return []
        stripped = text.lstrip("/")
        parts = stripped.split()
        partial = parts[0] if parts else ""
        if not partial:
            return []

        all_commands = list(self.BUILTIN_COMMANDS)
        if self._skill_pool:
            for s in self._skill_pool.list_skills():
                all_commands.append(f"/{s.name}")

        matches = [c for c in all_commands if c.startswith(f"/{partial}")]
        # Don't show completions if the user already typed an exact match
        if f"/{partial}" in matches:
            return []
        return matches[:5]

    def is_valid_command(self, text: str) -> bool:
        """Check if the input is a complete, valid slash command."""
        if not text.startswith("/"):
            return False
        cmd = text.split()[0] if text.split() else text
        all_commands = list(self.BUILTIN_COMMANDS)
        if self._skill_pool:
            for s in self._skill_pool.list_skills():
                all_commands.append(f"/{s.name}")
        return cmd in all_commands

    def expand_unique(self, text: str) -> str:
        """Expand a partial slash command if exactly one command matches.

        ``/codei`` → ``/codeindex``; ``/codei sync`` → ``/codeindex sync``.
        Leaves the text alone when there is no match, multiple matches, or
        an exact match for an existing command.
        """
        if not text.startswith("/") or text.startswith("//"):
            return text
        parts = text.split(None, 1)
        cmd_part = parts[0]
        rest = parts[1] if len(parts) > 1 else ""

        all_commands = list(self.BUILTIN_COMMANDS)
        if self._skill_pool:
            for s in self._skill_pool.list_skills():
                all_commands.append(f"/{s.name}")

        if cmd_part in all_commands:
            return text

        matches = [c for c in all_commands if c.startswith(cmd_part)]
        if len(matches) != 1:
            return text
        return f"{matches[0]} {rest}" if rest else matches[0]


def extract_at_mention(
    cursor_row: int,
    cursor_col: int,
    get_line: Callable[[int], str],
) -> str | None:
    """Extract the @-mention query at the cursor position.

    Scans backward from cursor on the current line to find ``@`` preceded
    by whitespace or at column 0.  Returns the text between ``@`` and the
    cursor, or ``None`` if the cursor is not inside an @-mention.
    """
    line = get_line(cursor_row)
    if cursor_col > len(line):
        cursor_col = len(line)

    # Scan backward from cursor to find @
    pos = cursor_col - 1
    while pos >= 0:
        ch = line[pos]
        if ch == "@":
            # Valid only if @ is at start of line or preceded by whitespace
            if pos == 0 or line[pos - 1] in (" ", "\t"):
                return line[pos + 1 : cursor_col]
            return None
        # Stop if we hit whitespace — no @ in this token
        if ch in (" ", "\t"):
            return None
        pos -= 1
    return None


class InputHandler:
    """Manages the input widget, history navigation, and autocomplete.

    Decoupled from the Textual App so it can be tested independently.
    """

    def __init__(
        self,
        skill_pool: Any | None = None,
        file_index: FileIndex | None = None,
        max_history: int = 100,
    ):
        self.history = InputHistory(max_size=max_history)
        self.autocomplete = AutocompleteProvider(skill_pool)
        self._file_index = file_index

    def on_submit(self, text: str) -> str | None:
        """Record the submitted text in history.

        Returns the stripped text, or None if empty.
        """
        stripped = text.strip()
        if not stripped:
            return None
        self.history.push(stripped)
        return stripped

    def on_up(self, current_text: str) -> str | None:
        """Navigate up in history."""
        return self.history.navigate_up(current_text)

    def on_down(self) -> str | None:
        """Navigate down in history."""
        return self.history.navigate_down()

    def get_completions(self, text: str) -> list[str]:
        """Get autocomplete suggestions for the current input."""
        return self.autocomplete.complete(text)

    def expand_unique_command(self, text: str) -> str:
        """Resolve a partial slash command when exactly one match exists."""
        return self.autocomplete.expand_unique(text)

    def get_file_completions(self, query: str) -> list[str]:
        """Get file path completions for an @-mention query."""
        if self._file_index is None:
            return []
        return self._file_index.match(query)
