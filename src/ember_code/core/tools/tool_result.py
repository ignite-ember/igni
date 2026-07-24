"""LLM tool-result buffering.

Home of :class:`LLMResultBuffer` — a small class whose one job is
"shape tool-result strings so we don't send a 500 kB dump to the
LLM." Replaces the module-level ``_MAX_RESULT_CHARS`` constant +
free-function ``_truncate`` in ``process_supervisor.py`` (audit
oop_offenders: utility-module-of-related-helpers +
free-function-with-state-first-arg).

Callers own an instance rather than importing a bare helper — so
tests can dial the limit down, and the concept "trimming a tool
result" lives in one named class.
"""

from __future__ import annotations


class LLMResultBuffer:
    """Shape tool-result strings so we don't overwhelm the LLM.

    Instance state (``self._max_chars``) parametrises the truncation
    threshold; tests pass a smaller value to exercise the head/tail
    branch cheaply. Production callers use the default.

    Head/tail truncation is chosen over head-only because for shell
    tool results the tail (last error, final status line) is
    usually as load-bearing as the head (start of the command's
    output). Cutting the middle preserves both bookends.
    """

    #: Default cap on characters returned to the LLM. 30 kB is
    #: roughly 7-8 k tokens, well under any current context
    #: window but enough to include a reasonable stack trace.
    MAX_CHARS: int = 30_000

    def __init__(self, max_chars: int | None = None) -> None:
        self._max_chars = max_chars if max_chars is not None else self.MAX_CHARS

    @property
    def max_chars(self) -> int:
        """Current character cap (read-only)."""
        return self._max_chars

    def truncate(self, text: str, limit: int | None = None) -> str:
        """Truncate ``text`` down to at most ``limit`` characters.

        Passes ``text`` through unchanged when it already fits. On
        overflow, keeps the first + last halves and stitches a
        one-line "N chars truncated" marker between them so the
        LLM can tell the output was clipped.
        """
        cap = limit if limit is not None else self._max_chars
        if len(text) <= cap:
            return text
        half = cap // 2
        return (
            text[:half] + f"\n\n... ({len(text) - cap} characters truncated) ...\n\n" + text[-half:]
        )
