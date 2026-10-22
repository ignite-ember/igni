"""What became of the passage a comment was left on.

A comment quotes a passage of one version. The document moves on, and
the reviewer is left holding a quote and a question nobody answers:
*does this still apply?* The review queue said "the document has moved
on since" against every row, which is true of all of them and
separates none of them — a passage still present word for word and a
passage deleted outright get the same sentence.

Two steps, and the order matters.

**First, what the text did.** Deterministic, cheap, and not a matter
of opinion: the quote is either still there, or it is there in altered
form, or it is gone. Finding *which* is a string problem, and the
answer carries what the passage says now, so the reviewer can read the
change rather than be told there was one.

**Then, whether the comment survived it.** That is not a string
problem. "Name the cookie and its lifetime" is answered by an edit
that names the cookie, and overtaken by one that removes cookies
entirely — both are "the anchor changed". Only something that reads
the comment against the change can tell those apart, so the model is
asked, and asked *second*, with the diff already in hand.

The split is the point. The first step never guesses, and the second
never runs without evidence in front of it.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from enum import Enum

#: Similarity at or above which two passages are the same passage,
#: edited. Below it they are different passages and the old one is
#: treated as gone.
#:
#: 0.55 rather than something tidier. Measured against the comment
#: fixtures: a passage with one clause rewritten scores 0.78-0.93, a
#: passage sharing only its subject scores 0.19-0.41. The gap is wide
#: and this sits in it, nearer the bottom because calling a survivor
#: "gone" costs a reviewer more than the reverse — "gone" ends the
#: thread, "changed" invites a read.
SAME_PASSAGE: float = 0.55

#: Similarity above which the edit is not worth calling a change.
#: Whitespace, a capital letter, a comma. The reviewer is told the
#: anchor survived, because for the purpose of judging the comment it
#: did.
COSMETIC: float = 0.97

#: Characters of surrounding text carried with a passage, so the
#: reviewer sees it in its paragraph rather than alone.
CONTEXT_CHARS: int = 400


class AnchorState(str, Enum):
    """What happened to the quoted passage."""

    #: Still in the current version, word for word or near enough.
    INTACT = "intact"
    #: Recognisably the same passage, edited.
    EDITED = "edited"
    #: No passage in the current version resembles it.
    GONE = "gone"
    #: The comment quoted nothing — it is about the document at large.
    NO_ANCHOR = "no-anchor"


@dataclass
class AnchorFate:
    """The passage then, the passage now, and which of the two it is."""

    state: AnchorState = AnchorState.NO_ANCHOR
    #: The quote, as the comment recorded it.
    then: str = ""
    #: The passage that replaced it, when one did.
    now: str = ""
    #: How alike they are, 0-1. Zero when there is nothing to compare.
    similarity: float = 0.0

    @property
    def survived(self) -> bool:
        return self.state in (AnchorState.INTACT, AnchorState.EDITED)


def _squash(text: str) -> str:
    return " ".join(str(text or "").split())


def _blocks(content: str) -> list[str]:
    """The document as passages, the way a reader sees it.

    Split on blank lines rather than newlines: prose is hard-wrapped,
    so a line is an arbitrary slice of a sentence and comparing one
    against a quote measures the wrapping as much as the words.
    """
    return [block for block in re.split(r"\n\s*\n", str(content or "")) if block.strip()]


def track_anchor(quote: str, now_content: str) -> AnchorFate:
    """Find what became of ``quote`` in the text as it stands.

    Exact first, because the common case is common and a string
    comparison is not a judgement. Only when that fails is anything
    inferred, and what is inferred is reported with the number it
    rests on.
    """
    wanted = _squash(quote)
    if not wanted:
        return AnchorFate(state=AnchorState.NO_ANCHOR)

    body = _squash(now_content)
    if wanted in body:
        return AnchorFate(
            state=AnchorState.INTACT, then=quote.strip(), now=quote.strip(), similarity=1.0
        )

    # Not there verbatim. Which passage is it *now*? Compared block by
    # block rather than against the whole document: a ratio against
    # ten thousand characters is dominated by everything the quote is
    # not about.
    best = ""
    score = 0.0
    for block in _blocks(now_content):
        ratio = difflib.SequenceMatcher(None, wanted, _squash(block)).ratio()
        if ratio > score:
            best, score = block.strip(), ratio

    if score >= COSMETIC:
        # A comma, a capital, a re-wrap. Saying "edited" here would
        # send a reviewer to read a diff that does not exist.
        return AnchorFate(state=AnchorState.INTACT, then=quote.strip(), now=best, similarity=score)
    if score >= SAME_PASSAGE:
        return AnchorFate(state=AnchorState.EDITED, then=quote.strip(), now=best, similarity=score)
    return AnchorFate(state=AnchorState.GONE, then=quote.strip(), now="", similarity=score)


class Relevance(str, Enum):
    """Whether the comment survived what happened to its passage."""

    STILL_APPLIES = "still-applies"
    OVERTAKEN = "overtaken"
    UNCLEAR = "unclear"


_SPELLINGS = {
    "still-applies": Relevance.STILL_APPLIES,
    "still applies": Relevance.STILL_APPLIES,
    "applies": Relevance.STILL_APPLIES,
    "overtaken": Relevance.OVERTAKEN,
    "addressed": Relevance.OVERTAKEN,
    "resolved": Relevance.OVERTAKEN,
    "unclear": Relevance.UNCLEAR,
    "uncertain": Relevance.UNCLEAR,
    "unsure": Relevance.UNCLEAR,
}

_VERDICT_LINE = re.compile(r"^\s*verdict\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)

#: How long a reason may be before it stops being read.
MAX_REASON_CHARS: int = 500


@dataclass
class RelevanceVerdict:
    """The agent's read on a comment whose document moved."""

    relevance: Relevance = Relevance.UNCLEAR
    reason: str = ""


@dataclass
class RelevanceReview:
    """Everything the verdict is formed from."""

    document_name: str
    comment_body: str
    author: str
    commented_n: int
    head_n: int
    fate: AnchorFate
    #: The passage in its paragraph, as it stands now.
    context: str = ""
    suggested_text: str = ""

    def prompt_for(self) -> str:
        f = self.fate
        lines = [
            f'A comment was left on "{self.document_name}" v{self.commented_n}. '
            f"The document is now at v{self.head_n}. Does the comment still apply?",
            "",
            f"{self.author or 'Someone'} wrote:",
            "",
            f"  {_squash(self.comment_body)}",
        ]

        if self.suggested_text.strip():
            lines += [
                "",
                "They also proposed replacement text:",
                "",
                f"  {_squash(self.suggested_text)[:800]}",
            ]

        if f.state is AnchorState.NO_ANCHOR:
            lines += [
                "",
                "The comment quotes no passage — it is about the document as a whole.",
            ]
        elif f.state is AnchorState.INTACT:
            lines += [
                "",
                "The passage they commented on is unchanged:",
                "",
                f"  {_squash(f.then)}",
            ]
        elif f.state is AnchorState.EDITED:
            lines += [
                "",
                "The passage they commented on has been edited since "
                f"(similarity {f.similarity:.2f}).",
                "",
                "  Then: " + _squash(f.then),
                "  Now:  " + _squash(f.now),
            ]
        else:
            lines += [
                "",
                "The passage they commented on is no longer in the document. "
                f"The closest thing to it scores only {f.similarity:.2f}, which is "
                "why it is being called gone rather than edited.",
                "",
                "  Then: " + _squash(f.then),
            ]

        if self.context.strip():
            lines += ["", "── The document as it stands, around there ──", "", self.context]

        lines += [
            "",
            "Answer in exactly this shape:",
            "",
            "VERDICT: still-applies | overtaken | unclear",
            "Then two sentences at most, saying what the edit did to what was asked "
            "for. Name the specific thing, not the category.",
            "",
            "How to judge:",
            "",
            "1. **still-applies** — the thing asked for is still missing or still "
            "wrong. An edit near the passage does not settle a comment; only an "
            "edit that does what was asked does.",
            "2. **overtaken** — the edit already does what was asked, or removed the "
            "thing the comment was about, so there is nothing left to act on.",
            "3. **unclear** — you cannot tell from what you were given. Prefer this "
            "to a confident guess. Closing somebody's comment as overtaken when it "
            "was not is how people stop leaving comments.",
            "",
            "A changed anchor is not the same as an answered comment, and an intact "
            "anchor is not the same as an unanswered one — the fix may have landed "
            "in the paragraph below.",
        ]
        return "\n".join(lines).strip()

    @staticmethod
    def parse(answer: str) -> RelevanceVerdict:
        """Read the answer, and do not invent one it did not give."""
        body = str(answer or "").strip()
        if not body:
            return RelevanceVerdict()

        match = _VERDICT_LINE.search(body)
        relevance = Relevance.UNCLEAR
        if match is not None:
            said = match.group(1).strip().strip("*`").lower()
            # The template echoed back whole is not a choice.
            if "|" not in said:
                relevance = _SPELLINGS.get(said, Relevance.UNCLEAR)
            body = (body[: match.start()] + body[match.end() :]).strip()

        reason = _squash(body)
        if len(reason) > MAX_REASON_CHARS:
            reason = reason[:MAX_REASON_CHARS].rsplit(" ", 1)[0] + "…"
        return RelevanceVerdict(relevance=relevance, reason=reason)


def surrounding(content: str, passage: str, chars: int = CONTEXT_CHARS) -> str:
    """The passage in its paragraph, plus its neighbours.

    A passage alone answers "is it still here" and not "has the thing
    asked for been done" — the fix often lands in the sentence after.
    """
    blocks = _blocks(content)
    if not blocks:
        return ""
    wanted = _squash(passage)
    at = 0
    if wanted:
        best = 0.0
        for i, block in enumerate(blocks):
            ratio = difflib.SequenceMatcher(None, wanted, _squash(block)).ratio()
            if ratio > best:
                best, at = ratio, i

    out: list[str] = []
    for i in range(max(0, at - 1), min(len(blocks), at + 2)):
        out.append(blocks[i].strip())
    joined = "\n\n".join(out)
    return joined[:chars] if len(joined) > chars else joined
