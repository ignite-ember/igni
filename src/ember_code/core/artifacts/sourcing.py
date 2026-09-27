"""Whether a captured read was really a source, decided out loud.

Capture is deliberately generous: every artifact version the agent
retrieved while writing becomes a ``candidate`` edge, because missing
a derivation is unrecoverable and a false one is merely annoying. The
cost of that generosity is a queue of reads somebody has to
adjudicate, and the review row used to hand that queue over as a name,
a version number, and two buttons.

Measuring the overlap between the two texts fixed part of it — a
reviewer can now see *what* the two documents share. It did not fix
the rest. Passage similarity says two paragraphs are about the same
thing; it cannot say which way the influence ran, and a reviewer
staring at "0.78" is still being asked to be the judge of a question
the agent is better placed to answer, having read both documents.

So the agent answers first, and the human accepts or rejects the
answer. That is the right way round: a recommendation with its reasons
can be disagreed with, and a wall of evidence cannot.

The verdict is **advisory**. Nothing here writes an edge — the two
buttons still do, and they still mean what they meant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

#: Characters of either document carried into the prompt.
#:
#: A one-shot call with no tools reads only what it is handed, and a
#: verdict on a document the model half saw is worse than no verdict.
MAX_DOCUMENT_CHARS: int = 6_000

#: Measured overlaps quoted into the prompt.
MAX_OVERLAPS: int = 6

#: Characters kept from either side of one overlap.
MAX_QUOTE_CHARS: int = 400

#: How long a reason may be before it stops being read.
MAX_REASON_CHARS: int = 600


class Recommendation(str, Enum):
    """What the agent thinks the reviewer should press.

    ``UNCLEAR`` is a real answer and is offered on purpose. A verdict
    scale with only two values forces a coin toss into the shape of a
    finding, and the reviewer cannot tell which they were handed.
    """

    SOURCE = "source"
    NOT_A_SOURCE = "not-a-source"
    UNCLEAR = "unclear"


#: What the model is allowed to say, mapped from what it might write.
_SPELLINGS = {
    "source": Recommendation.SOURCE,
    "a source": Recommendation.SOURCE,
    "not-a-source": Recommendation.NOT_A_SOURCE,
    "not a source": Recommendation.NOT_A_SOURCE,
    "not_a_source": Recommendation.NOT_A_SOURCE,
    "notasource": Recommendation.NOT_A_SOURCE,
    "unclear": Recommendation.UNCLEAR,
    "unsure": Recommendation.UNCLEAR,
    "uncertain": Recommendation.UNCLEAR,
}

_VERDICT_LINE = re.compile(r"^\s*verdict\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class Overlap:
    """One measured resemblance, as the prompt needs it."""

    here_text: str
    here_line_from: int
    here_line_to: int
    there_text: str
    there_line_from: int
    there_line_to: int
    similarity: float


@dataclass
class SourceVerdict:
    """The agent's answer, and why."""

    recommendation: Recommendation = Recommendation.UNCLEAR
    reason: str = ""


def _clip(text: str, limit: int = MAX_QUOTE_CHARS) -> str:
    body = " ".join(str(text or "").split())
    if len(body) <= limit:
        return body
    return f"{body[:limit]}…"


@dataclass
class CandidateReview:
    """Everything the verdict is formed from."""

    dependent_name: str
    dependent_content: str
    source_name: str
    source_n: int
    source_content: str
    overlaps: list[Overlap] = field(default_factory=list)
    passages_touched: int = 0
    total_passages: int = 0

    def prompt_for(self) -> str:
        """Ask for a verdict, not for an essay about one."""
        lines = [
            f'Did "{self.dependent_name}" draw on "{self.source_name}" v{self.source_n}?',
            "",
            f'While writing "{self.dependent_name}", the agent retrieved '
            f'"{self.source_name}" v{self.source_n}. That was recorded automatically — '
            "every retrieval is — so it is evidence that the document was *read*, not "
            "that it was used. You are deciding which.",
        ]

        if self.total_passages:
            lines += [
                "",
                f"Measured by embedding similarity, {self.passages_touched} of "
                f"{self.total_passages} passages in "
                f'"{self.dependent_name}" resemble something in '
                f'"{self.source_name}".',
            ]

        if self.overlaps:
            lines += ["", "The strongest resemblances:", ""]
            for o in self.overlaps[:MAX_OVERLAPS]:
                lines.append(
                    f"  [{o.similarity:.2f}] "
                    f'"{self.dependent_name}" L{o.here_line_from}–{o.here_line_to}: '
                    f"{_clip(o.here_text)}"
                )
                lines.append(
                    f'        ↔ "{self.source_name}" '
                    f"L{o.there_line_from}–{o.there_line_to}: {_clip(o.there_text)}"
                )
        else:
            lines += [
                "",
                "No passage here resembles anything there closely enough to measure. "
                "That is evidence, not proof: a document can be built on an idea it "
                "never restates.",
            ]

        for heading, body in (
            (f'"{self.dependent_name}" — the document under review', self.dependent_content),
            (f'"{self.source_name}" v{self.source_n} — the read being judged', self.source_content),
        ):
            if body.strip():
                lines += ["", f"── {heading} ──", "", body[:MAX_DOCUMENT_CHARS]]

        lines += [
            "",
            "Answer in exactly this shape:",
            "",
            "VERDICT: source | not-a-source | unclear",
            "Then one short paragraph — three sentences at most — saying what the two "
            "documents do or do not share, naming the specific claim or section that "
            "carries it. Write it to somebody who has both documents open.",
            "",
            "How to judge:",
            "",
            "1. **source** — a claim, number, rule or structure here traces back to "
            "that document. Name it. Two documents about the same subject are not "
            "enough; there has to be something here that came from there.",
            "2. **not-a-source** — they share a topic, or some vocabulary, and nothing "
            "else. Say what the resemblance actually is, so the reviewer can see you "
            "looked rather than guessed.",
            "3. **unclear** — you genuinely cannot tell from what you were given. "
            "Prefer this to a confident guess. Confirming a false source makes this "
            "document raise an alarm every time an unrelated one changes, which is how "
            "people learn to ignore alarms.",
            "",
            "Similarity scores are a lead, not a verdict. High similarity between two "
            "restatements of a shared convention is not derivation, and a real "
            "derivation rewritten in different words can score low.",
        ]
        return "\n".join(lines).strip()

    @staticmethod
    def parse(answer: str) -> SourceVerdict:
        """Read the model's answer, and do not invent one it did not give.

        An unparseable answer becomes ``UNCLEAR`` with the raw text as
        the reason. Guessing a recommendation from prose would produce
        the one failure this is supposed to prevent: a confident
        verdict nobody actually made.
        """
        body = str(answer or "").strip()
        if not body:
            return SourceVerdict()

        match = _VERDICT_LINE.search(body)
        recommendation = Recommendation.UNCLEAR
        if match is not None:
            # "source | not-a-source | unclear" echoed back whole is
            # the template, not a choice.
            said = match.group(1).strip().strip("*`").lower()
            if "|" not in said:
                recommendation = _SPELLINGS.get(said, Recommendation.UNCLEAR)
            body = (body[: match.start()] + body[match.end() :]).strip()

        reason = " ".join(body.split())
        if len(reason) > MAX_REASON_CHARS:
            reason = reason[:MAX_REASON_CHARS].rsplit(" ", 1)[0] + "…"
        return SourceVerdict(recommendation=recommendation, reason=reason)
