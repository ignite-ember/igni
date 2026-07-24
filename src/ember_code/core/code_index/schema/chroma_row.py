"""Pydantic wrappers over Chroma's on-disk wire shape.

Chroma stores per-item metadata as a flat ``dict[str, str | int | bool]``
(no ``None`` — the collection rejects null values), and returns query
results as parallel ``dict[str, list[list[...]]]`` payloads. These
models give both shapes a Pydantic surface so:

  - The write path (``ChromaRowCodec.flatten``) and read path
    (``ChromaRowCodec.parse``) share one definition of the ~30 fields
    that live on a document row.
  - The chunk write path (``ChromaChunkRow``) stops hand-rolling a
    dict literal in ``CodeIndex.add_item``.
  - Result unpacking (``ChromaQueryPage`` / ``ChromaGetPage``) stops
    positionally indexing into ``result["ids"][0]`` all over the
    search hot path.

Sentinels: chroma metadata rejects ``None``. We preserve the
long-standing convention that unassessed strings land as ``""``,
missing ints land as ``-1``, and missing bools land as ``False``.
The codec converts these back to nullable Python values at parse time.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChromaRowMetadata(BaseModel):
    """Flat metadata payload for a single document row in chroma.

    Every field is non-optional with a sentinel default — that's the
    only way to survive the chroma metadata boundary, which can't
    hold ``None``.
    """

    model_config = ConfigDict(extra="allow")

    name: str = ""
    type: str = ""
    kind: str = ""
    entity_type: str = ""
    parent_id: str = ""
    file_extension: str = ""
    repository_id: str = ""
    path: str = ""
    archived: bool = False
    timestamp: str = ""
    token_count: int = 0
    line_from: int = -1
    line_to: int = -1
    needs_refactoring: bool = False

    # Quality categoricals — ``""`` is the "not assessed" sentinel.
    quality: str = ""
    complexity: str = ""
    security: str = ""
    testing: str = ""
    testability: str = ""
    documentation: str = ""
    performance: str = ""
    issues: str = ""
    maintainability: str = ""
    architecture: str = ""
    technical_debt: str = ""
    cohesion: str = ""
    coupling: str = ""
    stability: str = ""
    priority: str = ""

    # Multi-value list fields — stored as ``\x1f``-bracketed strings.
    vulnerabilities: str = ""
    frameworks: str = ""
    domain: str = ""
    concerns: str = ""
    layers: str = ""
    patterns: str = ""
    keywords: str = ""
    file_issues: str = ""

    def to_chroma_dict(self) -> dict[str, Any]:
        """Serialize to the flat dict shape chroma's collection API accepts."""
        return self.model_dump()

    @classmethod
    def from_chroma_dict(cls, data: dict[str, Any] | None) -> ChromaRowMetadata:
        """Parse the flat dict shape chroma's collection API returns."""
        return cls.model_validate(data or {})


class ChromaChunkRow(BaseModel):
    """Per-chunk metadata subset — only the fields needed to identify
    a chunk's parent and its display context.

    Replaces the hand-rolled dict literal in ``CodeIndex.add_item``
    that used to build one row per chunk.
    """

    parent_doc_id: str
    chunk_index: int
    name: str = ""
    type: str = ""
    kind: str = ""
    path: str = ""
    file_extension: str = ""
    repository_id: str = ""

    def to_chroma_dict(self) -> dict[str, Any]:
        return self.model_dump()


class ChromaQueryPage(BaseModel):
    """Typed wrapper around chroma's ``collection.query(...)`` return shape.

    Chroma returns each field as ``list[list[...]]`` — the outer list
    is one entry per query text, the inner is the top-N hits for that
    query. We only ever pass one query text, so :meth:`row` unwraps
    the outer level.
    """

    model_config = ConfigDict(extra="allow")

    ids: list[list[str]] = Field(default_factory=list)
    documents: list[list[str | None]] = Field(default_factory=list)
    metadatas: list[list[dict[str, Any] | None]] = Field(default_factory=list)
    distances: list[list[float | None]] = Field(default_factory=list)

    @classmethod
    def from_chroma(cls, payload: dict[str, Any] | None) -> ChromaQueryPage:
        p = payload or {}
        return cls(
            ids=p.get("ids") or [],
            documents=p.get("documents") or [],
            metadatas=p.get("metadatas") or [],
            distances=p.get("distances") or [],
        )

    def row(
        self, index: int = 0
    ) -> tuple[list[str], list[str | None], list[dict[str, Any] | None], list[float | None]]:
        """Return ``(ids, docs, metas, dists)`` for the ``index``-th query text.

        Returns four empty lists when the page has no rows.
        """
        ids = self.ids[index] if index < len(self.ids) else []
        docs = self.documents[index] if index < len(self.documents) else []
        metas = self.metadatas[index] if index < len(self.metadatas) else []
        dists = self.distances[index] if index < len(self.distances) else []
        return ids, docs, metas, dists


class ChromaGetPage(BaseModel):
    """Typed wrapper around chroma's ``collection.get(...)`` return shape.

    Flat one-level lists (no per-query nesting), one entry per row.
    """

    model_config = ConfigDict(extra="allow")

    ids: list[str] = Field(default_factory=list)
    documents: list[str | None] = Field(default_factory=list)
    metadatas: list[dict[str, Any] | None] = Field(default_factory=list)

    @classmethod
    def from_chroma(cls, payload: dict[str, Any] | None) -> ChromaGetPage:
        p = payload or {}
        return cls(
            ids=p.get("ids") or [],
            documents=p.get("documents") or [],
            metadatas=p.get("metadatas") or [],
        )
