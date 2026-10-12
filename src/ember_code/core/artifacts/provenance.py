"""Which passage of this document came from which passage of its source.

Lineage answers "this document was built on that one". It does not
answer "this *sentence* came from that paragraph", and that is the
question somebody actually has when a source moves: not *is* this
stale, but *where*.

Nothing records the passage-level link, so it is inferred — every
chunk carries an embedding, and a passage is matched to the source
passage nearest it in meaning. Two consequences follow, and both are
the reason this module keeps saying "likely":

- **It is a guess.** A confident-looking line drawn between two
  paragraphs that have nothing to do with each other is worse than no
  line, so matches below :data:`FLOOR` are dropped rather than shown
  faintly, and what survives is labelled as a resemblance.
- **It is symmetric and dumb.** It knows the two passages are about
  the same thing. It does not know which way the influence ran, and it
  will happily match a passage to a source passage the author never
  read. The document-level edge is the thing with authority; this
  refines it, and is never evidence on its own.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

#: A fenced-code delimiter, indented or not, in either spelling.
_FENCE = re.compile(r"^\s*(?:```|~~~)")

#: The ``| --- | --- |`` line under a table's header.
_TABLE_DELIMITER = re.compile(r"^[\s|:-]+$")

#: Cosine similarity below which a match is not worth drawing.
#:
#: Measured, not guessed. Against a real store: passages that share
#: only a topic scored 0.12–0.56, and genuine derivations scored 0.74
#: and 0.75. The gap between those is wide, and this sits in it —
#: comfortably above the noise so a document does not read as derived
#: from the whole of its source, and comfortably below the weakest
#: real match so a restatement in different words still lands.
FLOOR: float = 0.70

#: Source passages kept per document passage. A paragraph that draws
#: on two parts of a source is ordinary; one that draws on six is a
#: match that has stopped discriminating.
MAX_MATCHES: int = 3

#: Similarity at or above which a match is as strong as the weakest
#: derivation ever measured here.
#:
#: 0.74, because that is the number — the two genuine derivations in
#: the measurement behind :data:`FLOOR` scored 0.74 and 0.75. The band
#: under this one used to start at 0.80, which put every real match
#: measured into the weakest band on offer and labelled it "loose". A
#: scale whose lowest grade is where the true answers live is not
#: grading anything.
CLOSE: float = 0.74

#: Similarity at or above which the passages are near-restatements.
#:
#: Not measured — nothing in the sample scored near it. It is kept
#: because it is uncontradicted and because a band that means
#: "practically the same sentence" is worth having; it is the one
#: threshold here still set by judgement rather than by evidence.
VERY_CLOSE: float = 0.90


def strength_of(similarity: float) -> str:
    """How sure, in words.

    Cut against the measurement recorded on :data:`FLOOR`, not against
    round numbers. Three bands, and the middle one is the point:

    ``very close``  ≥ 0.90 — near-restatement.
    ``close``       ≥ 0.74 — at or above every derivation measured.
    ``weak``        ≥ 0.70 — above every noise score measured, below
                    every genuine one. Four hundredths wide, and it
                    means *we do not know*, which is why it is not
                    called "loose": loose describes the match, weak
                    describes the evidence for it.

    The honest caveat is that "every derivation measured" is two of
    them. The bands are as good as that sample and should be re-cut
    when there is a bigger one — but two real measurements beat the
    round numbers they replaced.
    """
    if similarity >= VERY_CLOSE:
        return "very close"
    if similarity >= CLOSE:
        return "close"
    return "weak"


def _table_head_for(lines: list[str], at: int) -> list[str]:
    """The header rows a slice needs to keep being a table.

    Same problem as a split code fence, and it reads worse: a GFM
    table without its header and delimiter is not a broken table, it
    is not a table, so the rows come out as literal text — "| writes |
    10 / min | 120 / min |" across the card.
    """

    def is_row(line: str) -> bool:
        return "|" in line and line.strip() != ""

    def is_delimiter(line: str) -> bool:
        return "|" in line and "-" in line and _TABLE_DELIMITER.match(line) is not None

    i = at - 2
    if i < 0 or i >= len(lines) or not is_row(lines[i]):
        return []
    while i >= 0 and is_row(lines[i]):
        i -= 1

    head = i + 1
    if head + 1 >= len(lines) or not is_delimiter(lines[head + 1]):
        return []
    # Nothing to restore when the slice already carries its own header.
    if at - 1 <= head + 1:
        return []
    return [lines[head], lines[head + 1]]


def quote_lines(content: str, line_from: int, line_to: int) -> str:
    """The lines a match points at, quotable as they stand.

    A chunk is cut to a character budget, not to lines, so its text
    begins and ends wherever the budget ran out — a quote of one read
    "of the caller's budget:" and showed a bare ``` on a line of its
    own. Neither is wrong about *where* the match is; both are unusable
    as a quotation.

    So the quote is rebuilt from the source's own text on the line
    numbers the match already carries, and a code fence the range cuts
    through is repaired. The repair has a direction — a range can end
    after opening a block, or begin inside one it never opened — and
    which it is cannot be read off the slice, since ``` both opens and
    closes. It can be read off the *document*, which is why this lives
    where the content is rather than in the browser.

    Returns an empty string when the range does not land in the
    content, so the caller can keep whatever it already had.
    """
    if not content:
        return ""
    lines = content.split("\n")
    start = max(1, line_from)
    end = min(line_to, len(lines))
    if end < start:
        return ""

    body = "\n".join(lines[start - 1 : end])
    inside = sum(1 for line in lines[: start - 1] if _FENCE.match(line)) % 2 == 1
    if inside:
        body = f"```\n{body}"
    else:
        # Inside a fence a pipe is just a pipe, so this is only asked
        # for when the slice is in prose.
        head = _table_head_for(lines, start)
        if head:
            body = "\n".join([*head, body])
    if sum(1 for line in body.split("\n") if _FENCE.match(line)) % 2 == 1:
        body = f"{body}\n```"
    return body


@dataclass(frozen=True)
class Passage:
    """One chunk of one version, with where it sits in the text."""

    chunk_id: str
    text: str
    line_from: int
    line_to: int
    embedding: list[float] | None = None

    def touches(self, line_from: int, line_to: int) -> bool:
        """Whether this passage overlaps a line range at all."""
        return not (self.line_to < line_from or self.line_from > line_to)


@dataclass(frozen=True)
class Match:
    """A source passage this passage resembles."""

    source_chunk_id: str
    source_line_from: int
    source_line_to: int
    source_text: str
    similarity: float
    #: True when this source passage is inside the source's changed
    #: lines — the difference between "built on a source that moved"
    #: and "built on the part of it that moved".
    changed: bool = False


@dataclass
class PassageProvenance:
    """One passage of the document, and what it traces to."""

    chunk_id: str
    line_from: int
    line_to: int
    text: str
    matches: list[Match] = field(default_factory=list)

    @property
    def affected(self) -> bool:
        """Whether anything it resembles has since changed."""
        return any(m.changed for m in self.matches)

    @property
    def best(self) -> Match | None:
        return self.matches[0] if self.matches else None


def cosine(a: list[float] | None, b: list[float] | None) -> float:
    """Cosine similarity, or ``0.0`` when either side has no vector.

    Chunks written before embeddings were stored have none. They are
    not errors and not matches — they simply cannot be placed, which
    is the honest answer rather than a zero-distance one.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


class ProvenanceMapper:
    """Maps a document's passages onto its source's passages."""

    @staticmethod
    def changed_ranges(rows: list[object]) -> list[tuple[int, int]]:
        """Line ranges the source's diff reports as not equal.

        Takes :class:`~ember_code.core.artifacts.side_by_side.DiffRow`
        values — the same rows the comparison view renders, so what is
        highlighted here and what is highlighted there cannot disagree.
        Only the *right* side matters: those are line numbers in the
        version the source is at now.
        """
        out: list[tuple[int, int]] = []
        for row in rows:
            kind = getattr(getattr(row, "kind", None), "value", None) or str(
                getattr(row, "kind", "")
            )
            if kind == "equal":
                continue
            n = int(getattr(row, "right_n", 0) or 0)
            if n > 0:
                out.append((n, n))
        return _merge_ranges(out)

    @classmethod
    def map(
        cls,
        passages: list[Passage],
        source_passages: list[Passage],
        *,
        changed: list[tuple[int, int]] | None = None,
        floor: float = FLOOR,
        max_matches: int = MAX_MATCHES,
    ) -> list[PassageProvenance]:
        """Match each passage to the source passages it resembles.

        ``changed`` marks which source lines moved, so a passage can
        be told apart from one whose source text is untouched. Without
        it every match is reported as unchanged, which is right for
        the "show me where this came from" reading of the same data.
        """
        ranges = changed or []
        out: list[PassageProvenance] = []
        for passage in passages:
            scored = [
                (cosine(passage.embedding, candidate.embedding), candidate)
                for candidate in source_passages
            ]
            # Sorted by id as well as score so an exact tie between two
            # source passages resolves the same way every call — a
            # highlight that moves between reloads reads as a bug.
            scored.sort(key=lambda pair: (-pair[0], pair[1].chunk_id))
            matches = [
                Match(
                    source_chunk_id=candidate.chunk_id,
                    source_line_from=candidate.line_from,
                    source_line_to=candidate.line_to,
                    source_text=candidate.text,
                    similarity=round(score, 4),
                    changed=any(candidate.touches(lo, hi) for lo, hi in ranges),
                )
                for score, candidate in scored[:max_matches]
                if score >= floor
            ]
            out.append(
                PassageProvenance(
                    chunk_id=passage.chunk_id,
                    line_from=passage.line_from,
                    line_to=passage.line_to,
                    text=passage.text,
                    matches=matches,
                )
            )
        return out


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Collapse adjacent and overlapping line ranges.

    A diff reports changed lines one at a time; six consecutive ones
    are one edit, and testing a passage against six ranges instead of
    one is the same answer six times over.
    """
    if not ranges:
        return []
    ordered = sorted(ranges)
    merged = [ordered[0]]
    for lo, hi in ordered[1:]:
        last_lo, last_hi = merged[-1]
        if lo <= last_hi + 1:
            merged[-1] = (last_lo, max(last_hi, hi))
        else:
            merged.append((lo, hi))
    return merged
