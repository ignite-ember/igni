"""Pydantic DTOs consumed by :class:`DisplayManager`.

Kept in a sibling module (``display_schemas.py``) so:

* The domain shape lives on the data, not on Rich — a future
  non-Rich sink (JSON log, web transport) can reuse
  :meth:`RunStats.format_summary` and
  :meth:`ToolCallDisplay.format_args` without importing the
  terminal renderer.
* Rule 1 (no raw dicts crossing module boundaries) is enforced
  structurally: :class:`DisplayManager` accepts these models
  instead of loose ``**kwargs`` bags.

Both models are re-exported from :mod:`ember_code.core.utils.display`
so external callers can keep the single ``from ... import ...``
site.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RunStats(BaseModel):
    """Statistics for a completed agent run.

    Rendered by :meth:`DisplayManager.print_run_stats` as
    ``── 3.5s · 150 tokens (100↑ 50↓) · claude-opus-4-7 ──``.
    The formatting logic lives on this model (not on Rich) so
    the same summary can be produced for a non-terminal sink.
    """

    elapsed_seconds: float
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""

    def format_summary(self) -> str:
        """Human-readable single-line summary.

        Rules:

        * Elapsed under 60s renders as ``"X.Xs"``; longer runs
          collapse to ``"Nm Ss"``.
        * Token counts are omitted entirely when both are zero.
        * Model name is omitted when empty.

        Segments are joined with `` · `` in the order
        (elapsed, tokens, model).
        """
        parts: list[str] = []
        if self.elapsed_seconds < 60:
            parts.append(f"{self.elapsed_seconds:.1f}s")
        else:
            m = int(self.elapsed_seconds // 60)
            s = int(self.elapsed_seconds % 60)
            parts.append(f"{m}m {s}s")
        if self.input_tokens or self.output_tokens:
            total = self.input_tokens + self.output_tokens
            parts.append(f"{total} tokens ({self.input_tokens}↑ {self.output_tokens}↓)")
        if self.model:
            parts.append(self.model)
        return " · ".join(parts)


class ToolCallDisplay(BaseModel):
    """A tool invocation that :class:`DisplayManager` will render.

    Args are captured as a dict here (that's the shape the
    caller has), but the truncation policy (any value longer
    than ``max_value_len`` chars gets clipped with an ellipsis)
    lives on the model, not on Rich.
    """

    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)

    def format_args(self, max_value_len: int = 50) -> str:
        """Render args as ``" (k1=v1, k2=v2)"`` with per-value
        truncation. Returns the empty string when there are no
        args so the caller can concatenate without a guard.
        """
        if not self.args:
            return ""
        parts = []
        for k, v in self.args.items():
            val = str(v)
            if len(val) > max_value_len:
                val = val[: max_value_len - 3] + "..."
            parts.append(f"{k}={val}")
        return f" ({', '.join(parts)})"
