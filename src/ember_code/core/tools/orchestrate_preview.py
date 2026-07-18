"""Streamed-text preview formatter for the orchestrate stream handlers.

One :class:`PreviewFormatter` owns every "turn a chunk of streamed
text or a tool result into a short user-facing preview" contract
that used to live as three free functions (``_format_args`` /
``_preview`` / ``_build_preview``) plus two module-level constants
(``PREVIEW_WINDOW`` / ``PREVIEW_LINE_MAX``) in the retired
``orchestrate_helpers`` scrap-heap module:

* :meth:`format_args` — first two kwargs of a tool call, each value
  capped at 30 chars. Rendered under the tool-call activity line.
* :meth:`format_result` — one-line preview of a tool result, capped
  at ``RESULT_DEFAULT_LIMIT`` (60) chars by default.
* :meth:`format_content_buffer` — multi-line rolling preview shown
  under each agent header while a stream is live. The last
  :attr:`WINDOW` (5) non-empty lines of accumulated content, each
  truncated to :attr:`LINE_MAX` (120) chars, joined by ``\\n``.

Every previously-buried literal is now a class attribute
(:attr:`WINDOW`, :attr:`LINE_MAX`, :attr:`ARGS_KWARG_COUNT`,
:attr:`ARGS_VALUE_MAX`, :attr:`RESULT_DEFAULT_LIMIT`) so the
formatter is a single-source-of-truth surface — callers reference
these names rather than sprinkling magic numbers across the
streaming handlers.

Because the three call sites all use identical config, the module
exposes a stateless :data:`PREVIEWS` singleton. A future stream that
needs a different window can still construct its own
``PreviewFormatter(WINDOW=..., LINE_MAX=...)`` instance without
shadowing globals — the class is not a Singleton.

FE mirror: :attr:`WINDOW` and :attr:`LINE_MAX` are mirrored in
``clients/web/src/chat/model.ts`` as ``PREVIEW_WINDOW=5`` /
``PREVIEW_LINE_MAX=120`` (only ``PREVIEW_WINDOW`` is exported
today). The BE is the source of truth for the window contents; the
FE just replaces its rolling window on every ``content_preview``
event.
"""

from __future__ import annotations

from typing import Any


class PreviewFormatter:
    """Formatter for streamed-text previews.

    All configuration lives as class attributes so subclasses or
    hand-built instances can override just the pieces they need
    (e.g. a wider window for a debug view) while the defaults remain
    readable at the top of the file.
    """

    #: How many non-empty lines of streamed agent content to keep in
    #: the rolling "thinking" preview shown under each agent header.
    #: Matches the FE constant ``PREVIEW_WINDOW`` in
    #: ``clients/web/src/chat/model.ts`` — BE is the source of truth
    #: for the window; the FE just renders it.
    WINDOW: int = 5

    #: Per-line truncation cap for the rolling preview. Long lines
    #: get their tail replaced with a single U+2026 ellipsis.
    LINE_MAX: int = 120

    #: How many kwargs of a tool call to include in the one-line
    #: activity preview.
    ARGS_KWARG_COUNT: int = 2

    #: Per-value truncation cap for :meth:`format_args`. Long values
    #: get their tail replaced with three ASCII dots.
    ARGS_VALUE_MAX: int = 30

    #: Default cap for :meth:`format_result` — callers can pass a
    #: narrower limit at the call site.
    RESULT_DEFAULT_LIMIT: int = 60

    def format_args(self, tool_args: dict | None) -> str:
        """One-line preview of the first :attr:`ARGS_KWARG_COUNT`
        kwargs in a tool call. Each value is stringified, newlines
        collapsed to spaces, and truncated to
        :attr:`ARGS_VALUE_MAX` chars with a trailing ``...``.
        """
        if not tool_args:
            return ""
        parts = []
        for k, v in list(tool_args.items())[: self.ARGS_KWARG_COUNT]:
            val = str(v).replace("\n", " ")
            if len(val) > self.ARGS_VALUE_MAX:
                val = val[: self.ARGS_VALUE_MAX - 3] + "..."
            parts.append(f"{k}={val}")
        return ", ".join(parts)

    def format_result(self, result: Any, limit: int | None = None) -> str:
        """One-line preview of a tool result, capped at ``limit``
        chars (default :attr:`RESULT_DEFAULT_LIMIT`).
        """
        if result is None:
            return ""
        cap = self.RESULT_DEFAULT_LIMIT if limit is None else limit
        s = str(result).replace("\n", " ").strip()
        return s[:cap] + "..." if len(s) > cap else s

    def format_content_buffer(self, buf: str) -> str:
        """Turn an agent's accumulated streaming text into the
        multi-line preview payload — the last :attr:`WINDOW`
        non-empty lines, each truncated to :attr:`LINE_MAX` chars,
        joined by ``\\n``.

        Returning a multi-line ``text`` is the protocol: the FE
        splits on ``\\n`` and *replaces* its preview window. That
        keeps the BE as the source of truth — Agno deltas are
        token-sized, so the FE used to fill its window with
        token-per-line garbage when it appended each delta as its
        own preview entry.
        """
        if not buf:
            return ""
        cleaned = buf.replace("<think>", "").replace("</think>", "")
        lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
        if not lines:
            return ""
        tail = lines[-self.WINDOW :]
        truncated = [
            (ln[: self.LINE_MAX - 1] + "…") if len(ln) > self.LINE_MAX else ln for ln in tail
        ]
        return "\n".join(truncated)


#: Module-level stateless singleton. The three call sites in the
#: orchestrate stream handlers share the same config, so a single
#: instance is enough; a future stream with a different window can
#: construct its own :class:`PreviewFormatter` without shadowing.
PREVIEWS = PreviewFormatter()


__all__ = ["PreviewFormatter", "PREVIEWS"]
