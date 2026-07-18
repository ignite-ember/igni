"""Value-object wrapper over the indexer's ``[SECTION:…]…[/SECTION]`` markup.

The indexer's LLM-summary pass writes each item's ``content`` as a
sequence of named ``[SECTION:<name>]…[/SECTION]`` blocks. Two
operations on that markup are used across the codeindex tools:

  - :meth:`SectionMarkup.shorten` — extract the SUMMARY-group section
    and return its first sentence (or a hard char cap when the
    summary has no sentence boundary). Used to give the agent a
    one-line "what this thing does" alongside reference edges.
  - :meth:`SectionMarkup.keep` — filter the content to the requested
    semantic section groups (``Section.SECURITY`` → the concrete
    ``security_analysis`` / ``security`` / ``security_posture`` names
    per item type). Returns the joined matching blocks in the order
    they appear.

Both operations lived as free functions next to a section-alias dict
before this class existed; collapsing them into a single value-object
puts the compiled regex + char cap on one owner and lets the alias
lookup go through :meth:`Section.concrete_names` on the enum instead
of a parallel module-level dict.

:class:`SectionMarkup` is a plain class (not a Pydantic model) — the
content string is trusted (it comes from the indexer's own summary
pass) and no validation is needed.
"""

from __future__ import annotations

import logging
import re

from ember_code.core.code_index.enums import Section

logger = logging.getLogger(__name__)


class SectionMarkup:
    """Wraps one item's ``content`` string and exposes markup operations.

    The compiled regex and char-cap constant live on the class so a
    single instance is cheap. Construct one per operation:

        SectionMarkup(row.content).shorten()
        SectionMarkup(row.content).keep((Section.SECURITY,))
    """

    # ``[SECTION:<name>]<body>[/SECTION]`` — the indexer's marker shape.
    _SECTION_RE: re.Pattern[str] = re.compile(
        r"\[SECTION:(?P<name>[a-z_]+)\](?P<body>.*?)\[/SECTION\]",
        re.DOTALL,
    )
    # Hard cap on the "one-line summary" when the LLM-generated summary
    # was written without a sentence boundary (rare but real).
    _SHORT_SUMMARY_MAX_CHARS: int = 200

    def __init__(self, content: str) -> None:
        self._content = content or ""

    def shorten(self) -> str:
        """Extract the first SUMMARY-group section and return its first sentence.

        Falls back to a hard :attr:`_SHORT_SUMMARY_MAX_CHARS` char cap
        when the summary lacks a sentence boundary. Returns ``""`` if
        the content has no markers or no summary section.
        """
        if not self._content:
            return ""
        summary_names = Section.SUMMARY.concrete_names()
        for m in self._SECTION_RE.finditer(self._content):
            if m.group("name") not in summary_names:
                continue
            body = m.group("body").strip()
            if not body:
                return ""
            # Take the first sentence — most LLM-generated summaries open
            # with a one-sentence "this does X" before elaborating.
            first_sentence, _, _ = body.partition(". ")
            first_sentence = first_sentence.strip().rstrip(".")
            # Fall back to a hard char cap so a summary written without
            # sentence boundaries still fits.
            if not first_sentence or len(first_sentence) > self._SHORT_SUMMARY_MAX_CHARS:
                first_sentence = body[: self._SHORT_SUMMARY_MAX_CHARS].rstrip()
            return f"{first_sentence}."
        return ""

    def keep(self, sections: tuple[Section, ...]) -> str:
        """Keep only the requested ``[SECTION:…]…[/SECTION]`` blocks.

        ``sections`` carries semantic groups (e.g. ``Section.SECURITY``);
        :meth:`Section.concrete_names` resolves each group to the
        concrete section names used at file / entity / folder level.
        Returns the joined matching blocks (newline-separated) in the
        order they appear in the original content. If the content has
        no section markers, returns it unchanged — short docs /
        non-summarized items don't get filtered. If the resolved name
        set doesn't match anything in the content, returns an empty
        string (agent gets back what's actually there, which may be
        nothing).
        """
        if not self._content or not sections:
            return self._content
        wanted = self._resolve(sections)
        # If every requested Section value resolved to an empty
        # concrete-name set, the caller hit a gap in the section
        # mapping (typically a new Section enum member without a
        # ``_SECTION_CONCRETE_NAMES`` entry). Pass the content through
        # unchanged and warn so the gap surfaces — silently returning
        # "" used to make whole entity summaries disappear at the
        # agent's eyes for what is purely an internal configuration
        # bug.
        if not wanted:
            logger.warning(
                "SectionMarkup.keep: no concrete names resolved for %r — "
                "check Section.concrete_names coverage. Passing content through.",
                sections,
            )
            return self._content
        matches = list(self._SECTION_RE.finditer(self._content))
        if not matches:
            return self._content
        kept = [
            f"[SECTION:{m.group('name')}]{m.group('body')}[/SECTION]"
            for m in matches
            if m.group("name") in wanted
        ]
        return "\n\n".join(kept)

    @staticmethod
    def _resolve(sections: tuple[Section, ...]) -> set[str]:
        """Expand requested semantic groups into concrete section names.

        Delegates to :meth:`Section.concrete_names` so the alias data
        lives on the enum, not on this class.
        """
        wanted: set[str] = set()
        for s in sections:
            wanted |= s.concrete_names()
        return wanted
