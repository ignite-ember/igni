"""Reconciliation — what changed under a dependent, and what to do.

When a source moves, the dependent is not automatically wrong. What
the reviewer needs is the *evidence*: the diff between the version the
dependent was pinned to and the version the source is at now. That
diff is only reachable because the edge pinned a version number rather
than an artifact — pin the artifact and the left-hand side of this
diff is gone forever.

:class:`Reconciler` builds the plan. It never writes: accepting is a
separate, explicit call, because a patch applied without anyone
reading the diff is how a lineage graph fills with confidently wrong
documents.
"""

from __future__ import annotations

import difflib

from ember_code.core.artifacts.models import ReconcilePlan, StaleDependent

# Diffs are shown to a model as well as a human. Past a few hundred
# lines the model starts summarising the diff instead of applying it,
# so we truncate and say so rather than silently feeding it a wall.
MAX_DIFF_LINES: int = 400

# Lines are not a size. A wiki page keeps each paragraph on one line, so 186
# diff lines came to 21,878 characters, and with the dependent's content the
# reconcile result reached 35 kB — past the 30 kB igni gives any shell result.
# The dependent's content is never cut (the model rewrites it whole and cannot
# rewrite what it was not shown); the diff gives way instead, and says where
# the rest is.
MAX_DIFF_CHARS: int = 16_000


class Reconciler:
    """Builds :class:`ReconcilePlan` objects from a stale edge."""

    def __init__(
        self, *, max_diff_lines: int = MAX_DIFF_LINES, max_diff_chars: int = MAX_DIFF_CHARS
    ) -> None:
        self._max_diff_lines = max_diff_lines
        self._max_diff_chars = max_diff_chars

    def plan(
        self,
        *,
        stale: StaleDependent,
        dependent_content: str,
        pinned_content: str,
        head_content: str,
    ) -> ReconcilePlan:
        """Assemble the diff and the context a patch needs."""
        return ReconcilePlan(
            dependent_artifact_id=stale.dependent_artifact_id,
            dependent_name=stale.dependent_name,
            dependent_n=stale.dependent_n,
            dependent_content=dependent_content,
            source_artifact_id=stale.source_artifact_id,
            source_name=stale.source_name,
            pinned_n=stale.pinned_n,
            source_head_n=stale.source_head_n,
            source_diff=self.diff(
                pinned_content,
                head_content,
                from_label=f"{stale.source_name or stale.source_artifact_id}@{stale.pinned_n}",
                to_label=f"{stale.source_name or stale.source_artifact_id}@{stale.source_head_n}",
            ),
        )

    def diff(
        self,
        before: str,
        after: str,
        *,
        from_label: str = "before",
        to_label: str = "after",
    ) -> str:
        """Unified diff, truncated with an explicit marker.

        Silent truncation would read as "that's the whole change" and
        produce a patch that misses everything past the cut.
        """
        lines = list(
            difflib.unified_diff(
                before.splitlines(),
                after.splitlines(),
                fromfile=from_label,
                tofile=to_label,
                lineterm="",
                n=3,
            )
        )
        if not lines:
            return ""
        return self.cap(lines)

    def cap(self, lines: list[str]) -> str:
        """Join diff lines, stopping at the line or character budget.

        Whole lines only: a cut mid-line would read as a change that was
        never made. The marker says how many lines were held back and where
        the full source is.
        """
        kept: list[str] = []
        size = 0
        for line in lines[: self._max_diff_lines]:
            if kept and size + len(line) + 1 > self._max_diff_chars:
                break
            kept.append(line)
            size += len(line) + 1
        dropped = len(lines) - len(kept)
        if dropped:
            kept.append(
                f"... diff truncated — {dropped} more line(s) not shown; "
                "artifact_read the source to see its current text in full"
            )
        return "\n".join(kept)
