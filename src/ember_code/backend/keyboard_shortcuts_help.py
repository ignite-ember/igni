"""Static markdown block describing the TUI keyboard shortcuts.

Extracted from :mod:`ember_code.backend.command_handler` — the
old ``SHORTCUT_HELP`` module constant was a plain ``str`` sitting
alongside the slash-command dispatcher; the god-file audit
flagged that as unrelated cargo. Moved here as a
:class:`KeyboardShortcutsHelp` view so the block is a class-owned
piece of behaviour (Rule 1: data + rendering live together)
rather than a top-level constant.

The single consumer is :mod:`ember_code.backend.cmd_help`, whose
lazy import of ``command_handler.SHORTCUT_HELP`` still lands
because the old module keeps a re-export shim of
:attr:`KeyboardShortcutsHelp.markdown` — so external patches /
imports don't have to change.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel


class ShortcutItem(BaseModel):
    """One row of the shortcuts help block: key chord + description."""

    keys: str
    description: str

    def to_markdown_line(self) -> str:
        return f"- `{self.keys}` — {self.description}"


class KeyboardShortcutsHelp:
    """Class-owned view for the TUI keyboard-shortcuts help block.

    :attr:`_ITEMS` is the source of truth; :meth:`markdown`
    renders it. Callers get the same string the old
    ``SHORTCUT_HELP`` constant produced so nothing downstream has
    to change.
    """

    _ITEMS: ClassVar[list[ShortcutItem]] = [
        ShortcutItem(keys="Enter", description="send message"),
        ShortcutItem(keys="\\ + Enter", description="new line"),
        ShortcutItem(keys="Ctrl+D", description="quit"),
        ShortcutItem(keys="Ctrl+L", description="clear screen"),
        ShortcutItem(keys="Ctrl+O", description="expand/collapse all messages"),
        ShortcutItem(keys="Ctrl+V", description="toggle verbose mode"),
        ShortcutItem(keys="Up/Down", description="input history"),
        ShortcutItem(keys="Escape", description="cancel"),
    ]

    @classmethod
    def markdown(cls) -> str:
        """Return the shortcuts markdown block — same string as the
        legacy ``SHORTCUT_HELP`` constant so cmd_help's topic table
        keeps the exact wording."""
        return (
            "## Keyboard Shortcuts\n"
            + "\n".join(item.to_markdown_line() for item in cls._ITEMS)
            + "\n"
        )


__all__ = ["KeyboardShortcutsHelp", "ShortcutItem"]
