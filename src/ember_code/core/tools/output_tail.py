"""The last lines of a process's output — and, when that is not all of it, saying so.

The shell tools hand the model the last ``tail`` lines of a command's
output (100 by default). They used to do it silently: a 6,000-line log
reached the model as its last 100 lines with nothing to say the rest
existed, and against the real igni-1.0-4b the model confidently reported
a WARN line near the end as "the ERROR entry" — the error was at line
4,217.

:class:`OutputTail` keeps the cut and the counts together, and
:meth:`OutputTail.render` puts both in the result: a note above the
lines with how many were left out and how to get at them, and at the
cut the ``... (N characters truncated) ...`` marker
:class:`~ember_code.core.tools.tool_result.LLMResultBuffer` uses — the
one the fine-tuning corpus was written with (``trim_overlength_traces.py``
in ``igni__fine-tuning``), so the model meets the marker it was trained
on rather than a new one.
"""

from __future__ import annotations

from pydantic import BaseModel


class OutputTail(BaseModel):
    """The last lines of an output, and what came before them unseen."""

    lines: list[str]
    #: Lines before :attr:`lines` that the result leaves out — not shown,
    #: or no longer kept at all.
    omitted_lines: int = 0
    #: Characters in those lines, newlines included.
    omitted_chars: int = 0

    @classmethod
    def of(
        cls,
        kept: list[str],
        tail: int,
        *,
        dropped_lines: int = 0,
        dropped_chars: int = 0,
    ) -> OutputTail:
        """The last ``tail`` of ``kept``, after ``dropped_lines`` that
        came before ``kept`` and are already gone."""
        shown = kept[-tail:] if tail > 0 else []
        hidden = kept[: len(kept) - len(shown)]
        return cls(
            lines=shown,
            omitted_lines=dropped_lines + len(hidden),
            omitted_chars=dropped_chars + sum(len(line) + 1 for line in hidden),
        )

    @property
    def total_lines(self) -> int:
        return self.omitted_lines + len(self.lines)

    @property
    def cut(self) -> bool:
        return self.omitted_lines > 0

    def render(self, see_more: str) -> str:
        """The lines as the model reads them. Unchanged when nothing was
        left out; otherwise led by a note saying what was, and
        ``see_more`` — how to get at it from where the model is."""
        text = "\n".join(self.lines)
        if not self.cut:
            return text
        return (
            f"[Output truncated: {self.total_lines} lines; the first "
            f"{self.omitted_lines} are not shown, only the last {len(self.lines)}. "
            f"{see_more}]\n"
            f"... ({self.omitted_chars} characters truncated) ...\n\n"
            f"{text}"
        )
