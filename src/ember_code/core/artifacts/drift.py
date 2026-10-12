"""A document whose foundation has moved, put the way branches work.

The version chain of one document is not a dependency. Revising a
document produces v6 after v5 the way a commit follows a commit on the
branch you are working on — nothing has drifted, and nothing needs
deciding. What matters is the *other* branch: the document this one
was built on, and whether it has moved since we branched from it.

So the three numbers that describe the situation are the ones git
would use:

``pinned_n``
    The merge base. The version of the source this document was
    written against, recorded on the edge at write time.
``source_head_n``
    Where the source is now — the upstream head.
``versions_behind``
    How far the two have diverged.

While those last two are equal there is nothing to say. Once they are
not, the document is in the position of a feature branch whose main
has moved on: still perfectly readable, no longer safely mergeable,
and the only way to know which is to read what landed.

This module builds the reading. It does not decide anything — what the
change *means* for the document is a judgement about prose, which is
what the agent is handed all of this to make.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Changed blocks quoted into the prompt. A source rewritten end to
#: end is not usefully summarised by its first forty edits, and the
#: agent can read the whole version if it needs to.
MAX_CHANGES: int = 20
#: Characters kept from either side of one changed block.
MAX_BLOCK_CHARS: int = 300
#: Passages of the dependent quoted as possibly affected.
MAX_AFFECTED: int = 8
#: Characters of each document carried into the prompt.
#:
#: The report is written by a one-shot model call with no tools, so
#: there is nothing to fetch with — everything it reads has to be in
#: front of it. Generous, because a report about a document the model
#: only half saw is worse than no report.
MAX_DOCUMENT_CHARS: int = 12_000


def _clip(text: str, limit: int = MAX_BLOCK_CHARS) -> str:
    """Shorten from the middle, which is where the least meaning is."""
    body = " ".join(str(text or "").split())
    if len(body) <= limit:
        return body
    half = (limit - 5) // 2
    return f"{body[:half]} […] {body[-half:]}"


@dataclass(frozen=True)
class AffectedPassage:
    """A passage of the dependent that draws on something that moved."""

    text: str
    line_from: int
    line_to: int
    #: What it resembles upstream, and how closely.
    source_text: str = ""
    strength: str = ""


@dataclass
class Divergence:
    """Where two documents parted company, and what landed since."""

    dependent_id: str
    dependent_name: str
    dependent_n: int
    source_id: str
    source_name: str
    #: The merge base.
    pinned_n: int
    #: The upstream head.
    source_head_n: int
    #: Changed blocks upstream, as ``(kind, before, after)``.
    changes: list[tuple[str, str, str]] = field(default_factory=list)
    #: Passages of the dependent that resemble the changed parts.
    affected: list[AffectedPassage] = field(default_factory=list)
    #: The documents themselves. Carried because the report is written
    #: without tools, so nothing can be looked up while writing it.
    dependent_content: str = ""
    source_pinned_content: str = ""
    source_head_content: str = ""

    @property
    def versions_behind(self) -> int:
        return max(self.source_head_n - self.pinned_n, 0)

    @property
    def has_diverged(self) -> bool:
        return self.versions_behind > 0


class DriftUpdate:
    """Asks for the updated document, not for a description of one.

    The report is a different job: it explains, so somebody can
    decide. This produces the text that goes into the store, so the
    only acceptable output is the document itself — any framing around
    it ("Here is the revised version:") would be written into the
    document and become part of it.
    """

    @staticmethod
    def prompt_for(divergence: Divergence) -> str:
        if not divergence.has_diverged:
            raise ValueError("Nothing has diverged; there is nothing to bring up to date.")

        d = divergence
        lines = [
            f'Bring "{d.dependent_name}" up to date with "{d.source_name}" v{d.source_head_n}.',
            "",
            f'It was written against "{d.source_name}" v{d.pinned_n}, which has since '
            f"moved on {d.versions_behind} version"
            f"{'' if d.versions_behind == 1 else 's'}.",
        ]

        if d.changes:
            kept = d.changes[:MAX_CHANGES]
            lines += ["", f'What landed in "{d.source_name}":', ""]
            for kind, before, after in kept:
                if kind == "added":
                    lines.append(f"  + {_clip(after)}")
                elif kind == "removed":
                    lines.append(f"  − {_clip(before)}")
                else:
                    lines.append(f"  ~ {_clip(before)}")
                    lines.append(f"    → {_clip(after)}")
            if len(d.changes) > len(kept):
                lines.append(f"  (…{len(d.changes) - len(kept)} further changes not shown…)")

        for heading, body in (
            (f'"{d.source_name}" v{d.source_head_n} — what it says now', d.source_head_content),
            (f'"{d.dependent_name}" — the document to update', d.dependent_content),
        ):
            if body.strip():
                kept_text = body[:MAX_DOCUMENT_CHARS]
                lines += ["", f"── {heading} ──", "", kept_text]

        lines += [
            "",
            "Rules for what you return:",
            "",
            "1. Return the **complete updated document** and nothing else. No preamble, "
            "no explanation, no code fence around it. What you return is written to the "
            "store verbatim, so anything that is not the document becomes part of it.",
            "2. Change only what the upstream change makes wrong. This is somebody's "
            "document; rewriting the parts that were already correct is not an update, "
            "it is a replacement, and the diff will show every line you touched.",
            "3. Keep its structure, headings and voice.",
            "4. If nothing actually needs changing, return the document exactly as it "
            "stands. That is a real answer, and it is caught and reported as such — "
            "you do not need to invent an edit to make the request feel answered.",
        ]
        return "\n".join(lines).strip()


class DriftReview:
    """Assembles what the agent is told about a drifted dependency."""

    @staticmethod
    def summary(divergence: Divergence) -> str:
        """One line, in the terms the reader already has."""
        behind = divergence.versions_behind
        return (
            f"{divergence.dependent_name} was written against "
            f"{divergence.source_name} v{divergence.pinned_n}; "
            f"{divergence.source_name} is now at v{divergence.source_head_n} "
            f"({behind} version{'' if behind == 1 else 's'} ahead)."
        )
