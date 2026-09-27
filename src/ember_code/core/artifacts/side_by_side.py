"""Two versions of a document, aligned line by line.

:class:`Reconciler` already produces a unified diff, and that is the
right shape for a model: it is compact, it is what a patch looks like,
and an agent reads it fluently. It is the wrong shape for a person
comparing prose. A unified diff interleaves the two texts, so reading
"what did this paragraph used to say" means scanning up and down for
the matching ``-`` line, and a rewritten sentence shows as a deletion
and an addition several lines apart.

This module aligns them instead: every row carries a left cell and a
right cell, so the two versions sit in columns and the eye compares
horizontally. The alignment comes from ``SequenceMatcher`` opcodes,
which is also what makes a *replacement* come out as one row with both
sides filled rather than as an unpaired delete followed by an
unpaired insert.

No HTML, no colours, no markup — the rows say what kind of change
each one is and the renderer decides what that looks like. The same
rows serve the panel, an export and a terminal.
"""

from __future__ import annotations

import difflib
from enum import Enum

from pydantic import BaseModel, Field

#: Rows past this are dropped, with the count reported.
#:
#: A person scrolling a thousand-row diff has stopped comparing and
#: started scrolling. The cap is generous enough that a real document
#: revision fits whole, and the truncation is *stated* rather than
#: silent — a diff that quietly stops is one somebody will read as
#: "and nothing else changed".
MAX_ROWS: int = 600


class RowKind(str, Enum):
    """What happened to one aligned pair of lines."""

    EQUAL = "equal"
    ADDED = "added"
    REMOVED = "removed"
    #: Both sides present and different — a rewritten line.
    CHANGED = "changed"


class DiffRow(BaseModel):
    """One row of the comparison.

    ``left_n`` / ``right_n`` are 1-based line numbers within their own
    version, or ``0`` where that side has no line. They are carried
    rather than derived at render time because a row's position in the
    list is not its line number once anything has been added.
    """

    kind: RowKind
    left_n: int = 0
    left: str = ""
    right_n: int = 0
    right: str = ""


class VersionDiff(BaseModel):
    """The whole comparison, plus what it summarises to."""

    artifact_id: str = ""
    name: str = ""
    from_n: int = 0
    to_n: int = 0
    rows: list[DiffRow] = Field(default_factory=list)
    added: int = 0
    removed: int = 0
    changed: int = 0
    #: How many rows were dropped by the cap; 0 when nothing was.
    truncated: int = 0

    @property
    def unchanged(self) -> bool:
        """True when the two versions are identical.

        Worth asking directly: a revision that only touched metadata
        produces an empty diff, and "no changes" is a better answer
        than a blank panel.
        """
        return not (self.added or self.removed or self.changed)


def build(
    before: str,
    after: str,
    *,
    artifact_id: str = "",
    name: str = "",
    from_n: int = 0,
    to_n: int = 0,
    max_rows: int = MAX_ROWS,
) -> VersionDiff:
    """Align ``before`` and ``after`` into side-by-side rows.

    ``splitlines()`` rather than ``splitlines(keepends=True)``: the
    line terminator is not something a reader is comparing, and
    carrying it means every row ends in an invisible character the
    renderer then has to strip.
    """
    left_lines = before.splitlines()
    right_lines = after.splitlines()

    rows: list[DiffRow] = []
    added = removed = changed = 0
    matcher = difflib.SequenceMatcher(a=left_lines, b=right_lines, autojunk=False)

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                rows.append(
                    DiffRow(
                        kind=RowKind.EQUAL,
                        left_n=i1 + offset + 1,
                        left=left_lines[i1 + offset],
                        right_n=j1 + offset + 1,
                        right=right_lines[j1 + offset],
                    )
                )
            continue

        if tag == "replace":
            # Pair them up as far as both sides go, so a rewritten
            # line reads as one row with two halves rather than as a
            # delete and an insert that the reader has to match up.
            paired = min(i2 - i1, j2 - j1)
            for offset in range(paired):
                rows.append(
                    DiffRow(
                        kind=RowKind.CHANGED,
                        left_n=i1 + offset + 1,
                        left=left_lines[i1 + offset],
                        right_n=j1 + offset + 1,
                        right=right_lines[j1 + offset],
                    )
                )
                changed += 1
            # Whatever is left over on either side is a plain
            # insertion or deletion.
            for offset in range(paired, i2 - i1):
                rows.append(
                    DiffRow(
                        kind=RowKind.REMOVED,
                        left_n=i1 + offset + 1,
                        left=left_lines[i1 + offset],
                    )
                )
                removed += 1
            for offset in range(paired, j2 - j1):
                rows.append(
                    DiffRow(
                        kind=RowKind.ADDED,
                        right_n=j1 + offset + 1,
                        right=right_lines[j1 + offset],
                    )
                )
                added += 1
            continue

        if tag == "delete":
            for offset in range(i2 - i1):
                rows.append(
                    DiffRow(
                        kind=RowKind.REMOVED,
                        left_n=i1 + offset + 1,
                        left=left_lines[i1 + offset],
                    )
                )
                removed += 1
            continue

        # tag == "insert"
        for offset in range(j2 - j1):
            rows.append(
                DiffRow(
                    kind=RowKind.ADDED,
                    right_n=j1 + offset + 1,
                    right=right_lines[j1 + offset],
                )
            )
            added += 1

    truncated = max(0, len(rows) - max_rows)
    return VersionDiff(
        artifact_id=artifact_id,
        name=name,
        from_n=from_n,
        to_n=to_n,
        rows=rows[:max_rows],
        added=added,
        removed=removed,
        changed=changed,
        truncated=truncated,
    )
