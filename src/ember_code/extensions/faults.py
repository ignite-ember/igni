"""An extension that keeps not answering is faulty, and costs nobody any more waiting.

Every call igni makes into an extension has a deadline — a hook event
(:class:`~ember_code.extensions.host.hooks.ExtensionHooks`, two seconds),
a tool call (the MCP session's read timeout), a page's call, a prompt
contribution, a health probe. A deadline bounds one wait; it does not
stop igni waiting on the same hung process every time. So the floor
counts: an extension whose process times out or crashes
:attr:`ExtensionFaults.LIMIT` times in a row — hooks and tool calls
alike — is marked **faulty** (``InstalledExtension.faulty``):

* its process is restarted;
* the person is told, and the extensions panel and the host model's
  ``ExtensionInfo`` say so;
* its hooks are not waited on: a fail-closed event (``PreToolUse``,
  ``UserPromptSubmit``) is refused at once with "<extension> isn't
  responding", a fail-open one goes ahead without it.

The first answer after that clears the mark. An answer is anything the
process said — a tool that reports an error has answered; a call that
timed out, or whose process went away, has not.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import ClassVar

from ember_code.extensions.registry import ExtensionRegistry

logger = logging.getLogger(__name__)

#: Told when an extension becomes faulty: ``(extension_id, reason)``.
OnFaulty = Callable[[str, str], None]


class ExtensionFaults:
    """Counts each extension's failures in a row, and marks it faulty at the limit."""

    #: Timeouts or crashes in a row that make an extension faulty.
    LIMIT: ClassVar[int] = 3

    _shared: ClassVar[ExtensionFaults | None] = None

    def __init__(self, registry: ExtensionRegistry | None = None) -> None:
        self._registry = registry
        self._in_a_row: dict[str, int] = {}
        self._watchers: list[OnFaulty] = []

    @classmethod
    def shared(cls) -> ExtensionFaults:
        """The process's faults: one count per extension, whoever called it."""
        if cls._shared is None:
            cls._shared = cls()
        return cls._shared

    @classmethod
    def reset_shared(cls) -> None:
        """Forget the process's faults. For tests."""
        cls._shared = None

    def attach(self, registry: ExtensionRegistry) -> None:
        self._registry = registry

    def on_faulty(self, watcher: OnFaulty) -> None:
        """Be told when an extension becomes faulty — to restart it, and to tell the person."""
        self._watchers.append(watcher)

    # ── What igni saw ─────────────────────────────────────────────

    def failed(self, extension_id: str, what: str) -> None:
        """A call into it timed out, or its process crashed. ``what`` says which call."""
        count = self._in_a_row.get(extension_id, 0) + 1
        self._in_a_row[extension_id] = count
        if count < self.LIMIT or self.faulty(extension_id):
            return
        name = self._name(extension_id)
        reason = (
            f"{name} isn't responding: {count} calls in a row timed out or crashed "
            f"(the last, {what}). igni is restarting it."
        )
        logger.warning("%s", reason)
        registry = self._registry
        if registry is not None:
            registry.set_faulty(extension_id, reason)
        for watcher in list(self._watchers):
            try:
                watcher(extension_id, reason)
            except Exception:
                logger.exception("a faulty-extension watcher failed for %s", extension_id)

    def answered(self, extension_id: str) -> None:
        """It answered. The count starts again, and a faulty mark clears."""
        self._in_a_row.pop(extension_id, None)
        registry = self._registry
        if registry is not None and self.faulty(extension_id):
            logger.info("%s is answering again", extension_id)
            registry.set_faulty(extension_id, "")

    # ── What callers ask ──────────────────────────────────────────

    def faulty(self, extension_id: str) -> bool:
        registry = self._registry
        entry = registry.get(extension_id) if registry is not None else None
        return entry is not None and bool(entry.faulty)

    def not_responding(self, extension_id: str) -> str:
        """What a fail-closed event is refused with while it is faulty."""
        return f"{self._name(extension_id)} isn't responding."

    def _name(self, extension_id: str) -> str:
        registry = self._registry
        entry = registry.get(extension_id) if registry is not None else None
        return entry.manifest.display_name if entry is not None else extension_id


__all__ = ["ExtensionFaults", "OnFaulty"]
