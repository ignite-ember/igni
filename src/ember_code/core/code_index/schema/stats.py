"""Wire shapes for the CodeIndex panel's per-commit stats."""

from __future__ import annotations

from pydantic import BaseModel


class HeadStats(BaseModel):
    """Wire shape for :meth:`CodeIndex.head_stats` — per-commit
    coverage summary consumed by the CodeIndex panel donut.

    ``files_indexed`` is the count of unique files that have at
    least one ``type=="file"`` doc in the commit's chroma;
    ``languages_indexed`` maps ``file_extension`` (lowercased,
    ``"(other)"`` fallback) to the file count per extension."""

    files_indexed: int
    languages_indexed: dict[str, int]
