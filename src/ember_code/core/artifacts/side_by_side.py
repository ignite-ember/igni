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
import re
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

#: Below this much similarity, a rewritten passage stops being marked
#: word by word.
#:
#: Word-level marks help when somebody changed a few words. When two
#: passages share almost nothing, *every* word lights up and the
#: reader is back to comparing two highlighted paragraphs by eye —
#: the thing the marks were meant to save them from. Past this point
#: the honest signal is "this was rewritten", so the passage keeps its
#: block tint and the words are left alone.
#:
#: 0.6 because the two populations separate cleanly there. Measured on
#: the store's own documents: genuine small edits score 0.87-0.92
#: ("good for 24 hours" -> "good for 15 hours" is 0.92), while whole
#: rewrites and paragraphs that merely got paired by position score
#: 0.42-0.49. Nothing real sits in between.
WORD_LEVEL_FLOOR: float = 0.6


class RowKind(str, Enum):
    """What happened to one aligned pair of lines."""

    EQUAL = "equal"
    ADDED = "added"
    REMOVED = "removed"
    #: Both sides present and different — a rewritten line.
    CHANGED = "changed"


class Part(BaseModel):
    """A run of words inside a line, and whether it changed.

    The reason a document diff needs this and a code diff mostly does
    not: prose is edited *inside* a sentence. Marking the whole line
    tells a reader that something in it moved and leaves them to find
    out what — which, for a paragraph, is most of the work.
    """

    text: str
    changed: bool = False


class DiffRow(BaseModel):
    """One row of the comparison.

    ``left_n`` / ``right_n`` are 1-based line numbers within their own
    version, or ``0`` where that side has no line. They are carried
    rather than derived at render time because a row's position in the
    list is not its line number once anything has been added.

    ``left_parts`` / ``right_parts`` break a rewritten line into the
    words that actually differ. Empty for every other kind, because
    there is nothing to compare against on one side.
    """

    kind: RowKind
    left_n: int = 0
    left: str = ""
    right_n: int = 0
    right: str = ""
    left_parts: list[Part] = Field(default_factory=list)
    right_parts: list[Part] = Field(default_factory=list)


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


def _words(line: str) -> list[str]:
    """Split into words *and* the whitespace between them.

    Keeping the separators means the parts can be concatenated back
    into the original line exactly, which is what lets the renderer
    treat them as spans of one paragraph rather than as a list of
    tokens it has to re-space.
    """
    return re.findall(r"\s+|\S+", line)


def word_parts(
    before: str, after: str, *, floor: float = WORD_LEVEL_FLOOR
) -> tuple[list[Part], list[Part]]:
    """Mark the words that differ between two versions of one passage.

    Returns empty lists when the two share less than ``floor`` — see
    :data:`WORD_LEVEL_FLOOR`. The caller renders the passage whole in
    that case, and the block tint carries the change.

    Whitespace-only runs are never marked on their own. A changed word
    sits between two spaces, and marking those spaces stretches the
    highlight past the word on both sides — which reads as though the
    punctuation changed too.
    """
    left_words = _words(before)
    right_words = _words(after)
    matcher = difflib.SequenceMatcher(a=left_words, b=right_words, autojunk=False)
    if matcher.ratio() < floor:
        return [], []

    left: list[Part] = []
    right: list[Part] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        equal = tag == "equal"
        for word in left_words[i1:i2]:
            left.append(Part(text=word, changed=not equal and bool(word.strip())))
        for word in right_words[j1:j2]:
            right.append(Part(text=word, changed=not equal and bool(word.strip())))
    return _merge(left), _merge(right)


def _merge(parts: list[Part]) -> list[Part]:
    """Join neighbours that agree, so the renderer emits fewer spans.

    Whitespace *between two changed words* is absorbed into the
    highlight. "24 hours" becoming "15 minutes" is two changed words
    with an unchanged space between them, and marking them separately
    draws two boxes with a gap — which reads as two unrelated edits
    rather than one replaced phrase. The space is still not marked on
    its own; it only comes along when both sides of it changed.
    """
    merged: list[Part] = []
    for index, part in enumerate(parts):
        bridges = (
            not part.changed
            and not part.text.strip()
            and bool(merged)
            and merged[-1].changed
            and any(later.changed for later in parts[index + 1 : index + 2])
        )
        flag = part.changed or bridges
        if merged and merged[-1].changed == flag:
            merged[-1] = Part(text=merged[-1].text + part.text, changed=flag)
        else:
            merged.append(Part(text=part.text, changed=flag))
    return merged


#: Lines that are a block on their own, outside a code fence.
#:
#: A heading, a table row and a list item are each one thing a person
#: edits. Joining them into the paragraph above — which is what a
#: naive blank-line split does, because none of them is followed by a
#: blank line — turns "added one bullet" into "rewrote this list".
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
_TABLE_ROW = re.compile(r"^\s*\|")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
_FENCE = re.compile(r"^\s*(?:```|~~~)")
#: A blockquote line. The marker is stripped when the quote's lines
#: are joined and put back once on the whole block — otherwise the
#: second and later ``>`` end up in the middle of the sentence, which
#: is what a naive join produced: "...know what somebody > did, read
#: the audit log".
_QUOTE = re.compile(r"^\s{0,3}>\s?")


def _blocks(text: str) -> list[str]:
    """Split a document into the units a person edits.

    Prose is hard-wrapped, so paragraphs — not lines — are the unit
    there: a writer who edits one sentence re-flows every line after
    it, and a line-based diff then reports the whole paragraph as
    changed and pairs sentences that have nothing to do with each
    other. Measured on the store's own Auth model: a two-word edit lit
    up 35 words across four rows.

    But a document is not only prose, and that is the second half of
    this function. Bullets, numbered steps, table rows and code have
    no blank lines between them, so joining on blank lines alone
    swallowed each list, each table and each code block into a single
    line — and then "added one bullet" rendered as a rewritten
    paragraph, while a one-key change in a YAML block rewrote the
    whole block. Verified on a long document carrying all four.

    So: inside a fence, every line stands alone and is never joined,
    because indentation is the content there. Outside one, a heading,
    a table row or a list item starts its own block, and anything else
    continues the block above it.
    """
    blocks: list[str] = []
    current: list[str] = []
    fenced = False
    quoting = False

    def flush() -> None:
        nonlocal quoting
        if current:
            joined = " ".join(current)
            blocks.append(f"> {joined}" if quoting else joined)
            current.clear()
        quoting = False

    for line in text.splitlines():
        if _FENCE.match(line):
            # The fence itself is a block, so opening and closing one
            # shows as its own change rather than merging with the
            # code beside it.
            flush()
            blocks.append(line)
            fenced = not fenced
            continue

        if fenced:
            # Verbatim. Trailing whitespace is dropped because it is
            # invisible and would report a change nobody can see.
            blocks.append(line.rstrip())
            continue

        if not line.strip():
            flush()
            continue

        if _QUOTE.match(line):
            # A quote continues the quote above it and nothing else.
            if not quoting:
                flush()
                quoting = True
            current.append(_QUOTE.sub("", line).strip())
            continue

        if quoting:
            # Prose directly under a quote is not part of it.
            flush()

        if _HEADING.match(line) or _TABLE_ROW.match(line):
            # Complete in themselves. A heading that stayed open would
            # absorb the paragraph beneath it, so editing that
            # paragraph would render as an edited section title.
            flush()
            blocks.append(line.strip())
            continue

        if _LIST_ITEM.match(line):
            # Left open, because a bullet can wrap onto the next line
            # and that continuation belongs to it.
            flush()
            current.append(line.strip())
            continue

        current.append(line.strip())

    flush()
    return blocks


def build(
    before: str,
    after: str,
    *,
    artifact_id: str = "",
    name: str = "",
    from_n: int = 0,
    to_n: int = 0,
    max_rows: int = MAX_ROWS,
    by_paragraph: bool = True,
) -> VersionDiff:
    """Align ``before`` and ``after`` into side-by-side rows.

    Blocks by default — see :func:`_blocks` — because this compares
    documents. Pass ``by_paragraph=False`` for a line-for-line
    comparison, which is the right unit when the content really is
    line-oriented.
    """
    split = _blocks if by_paragraph else (lambda s: s.splitlines())
    left_lines = split(before)
    right_lines = split(after)

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
                before_line = left_lines[i1 + offset]
                after_line = right_lines[j1 + offset]
                left_parts, right_parts = word_parts(before_line, after_line)
                rows.append(
                    DiffRow(
                        kind=RowKind.CHANGED,
                        left_n=i1 + offset + 1,
                        left=before_line,
                        right_n=j1 + offset + 1,
                        right=after_line,
                        left_parts=left_parts,
                        right_parts=right_parts,
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
