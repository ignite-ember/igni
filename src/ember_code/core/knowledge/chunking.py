"""Chunking strategy for the knowledge index.

Agno's base :meth:`RecursiveChunking.clean_text` runs
``re.sub(r"\\s+", " ", ...)`` which folds every newline into a single
space — destroying markdown layout (headings, lists, fenced code) at
ingest time. The chunked content is exactly what the detail page
renders, so the loss is visible.

Single-responsibility home for chunking policy. If a second chunker
lands (e.g. token-aware or semantic), it belongs here alongside
:class:`NewlinePreservingChunker` so ``KnowledgeIndex`` continues to
accept any :class:`ChunkingStrategy` without caring about the
implementation.
"""

from __future__ import annotations

import re

from agno.knowledge.chunking.recursive import RecursiveChunking


class NewlinePreservingChunker(RecursiveChunking):
    """RecursiveChunking that preserves paragraph structure.

    Overrides :meth:`clean_text` to collapse only intra-line whitespace,
    keeping ``\\n`` untouched. Runs of three-or-more blank lines are
    capped at two newlines so paragraph spacing stays normalized.
    """

    def clean_text(self, text: str) -> str:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t\f\v]+", " ", text)
        return text
