"""Truncation helpers for tool-hook payloads.

Extracted from :mod:`ember_code.core.hooks.tool_hook` — the two
free helpers ``_safe_args`` / ``_preview`` are re-homed as
classmethods on :class:`PayloadSanitizer` so they share a named
subject (the "sanitize this before it hits a hook subprocess"
concept) and are trivially subclassable if a caller ever needs a
different truncation policy.

The original module keeps ``_safe_args`` / ``_preview`` as
two-line delegator shims that call these classmethods — several
test modules import them by name.
"""

from __future__ import annotations

from typing import Any


class PayloadSanitizer:
    """Truncate tool_args / tool results so hook payloads stay
    small enough to round-trip through a subprocess pipe or an
    HTTP body without swallowing multi-megabyte file contents.

    Both entry points are :func:`classmethod` — they need no
    instance state, but keeping them on the class (rather than
    module-level free functions) satisfies the OOP mandate:
    "utility-module-of-related-helpers → a class whose name
    captures the subject."
    """

    MAX_ARG_CHARS = 500
    """Per-arg truncation length. Chosen to preserve enough
    context for hook debug output while keeping a full multi-arg
    payload well under a typical 64 KiB pipe buffer."""

    MAX_PREVIEW_CHARS = 500
    """Truncation length for tool result previews. Same rationale
    as :data:`MAX_ARG_CHARS` — enough for a diff / commit-message
    preview, not enough to leak a whole file into an observer."""

    @classmethod
    def safe_args(cls, args: dict[str, Any]) -> dict[str, str]:
        """Return ``args`` stringified and per-value truncated.

        Every value is stringified (so a caller passing bytes / a
        Path / a dict still gets a printable value) and truncated
        to :data:`MAX_ARG_CHARS` characters.
        """
        limit = cls.MAX_ARG_CHARS
        safe: dict[str, str] = {}
        for k, v in args.items():
            s = str(v)
            safe[k] = s[:limit] if len(s) > limit else s
        return safe

    @classmethod
    def preview(cls, result: Any) -> str:
        """Return a stringified, truncated preview of ``result``.

        ``None`` maps to the empty string — hook observers get an
        unambiguous "no result" signal without an explicit
        ``result is None`` branch on their side.
        """
        if result is None:
            return ""
        limit = cls.MAX_PREVIEW_CHARS
        s = str(result)
        return s[:limit] if len(s) > limit else s
