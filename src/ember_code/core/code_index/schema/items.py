"""Domain models for files, folders, and chunks indexed by code_index.

Quality and category metadata are first-class typed fields, not a
catch-all ``tags`` list. Each chroma row carries one column per
quality dimension (``security``, ``complexity``, ...) and one column
per multi-value category (``vulnerabilities``, ``frameworks``, ...).
The agent-facing tool translates structured args into chroma
``where`` filters; downstream code never writes raw chroma queries.
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from ember_code.core.code_index.enums import FileSystemType
from ember_code.core.code_index.schema import convert_weaviate_types, now_iso


class CodeIndexItemBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    content: str | None = None
    timestamp: str = Field(default_factory=now_iso)

    _agno_documents: list | None = PrivateAttr(default=None)

    @property
    def content_hash(self) -> str | None:
        if self.content is None:
            return None
        return hashlib.sha256(self.content.encode()).hexdigest()

    def set_agno_documents(self, agno_documents: list) -> None:
        """Attach pre-chunked Agno reader output for downstream vectorization."""
        self._agno_documents = agno_documents

    @property
    def content_chunks(self) -> list[CodeIndexFileChunkBase]:
        if not self._agno_documents:
            return []
        return [
            CodeIndexFileChunkBase(index=i, content=doc.content)
            for i, doc in enumerate(self._agno_documents)
        ]


class CodeIndexFileChunkBase(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="allow")

    item_id: str = Field(default_factory=lambda: str(uuid4()))
    index: int
    content: str

    @model_validator(mode="before")
    @classmethod
    def _convert_weaviate_types(cls, data):
        if isinstance(data, dict):
            return convert_weaviate_types(data)
        return data

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode()).hexdigest()

    @property
    def uuid(self) -> str:
        return self.item_id


class CodeIndexItemCreate(CodeIndexItemBase):
    """The shape an indexer sends in. Mirrors the JSONL ``upsert_item`` op."""

    item_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str | None = None
    parent_id: str | None = None
    type: FileSystemType = FileSystemType.FILE
    path: str | None = None
    file_extension: str | None = None
    repository_id: str | None = None
    token_count: int | None = None
    line_from: int | None = None
    line_to: int | None = None

    # Code vs docs — the only place that distinction lives.
    kind: str | None = None  # "code" | "docs"

    # Entity classification (None for files/folders).
    entity_type: str | None = None

    # Quality categoricals. Empty string is the "not assessed" sentinel
    # since chroma metadata can't hold None.
    quality: str | None = None
    complexity: str | None = None
    security: str | None = None
    testing: str | None = None
    testability: str | None = None
    documentation: str | None = None
    performance: str | None = None
    issues: str | None = None
    maintainability: str | None = None
    architecture: str | None = None
    technical_debt: str | None = None
    cohesion: str | None = None
    coupling: str | None = None
    stability: str | None = None
    priority: str | None = None
    needs_refactoring: bool | None = None

    # Multi-value categories. Stored on chroma as ``\x1f``-bracketed strings.
    vulnerabilities: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    domain: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    layers: list[str] = Field(default_factory=list)
    patterns: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    file_issues: list[str] = Field(default_factory=list)

    def to_item(self) -> CodeIndexItem:
        return CodeIndexItem.model_validate(self.model_dump())


class CodeIndexItemUpdate(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    timestamp: str | None = None
    archived: bool | None = None

    def to_non_empty_dict(self) -> dict:
        return {k: v for k, v in self.model_dump().items() if v is not None}


class Metadata(BaseModel):
    distance: float | None = None
    certainty: float | None = None


class CodeIndexFileChunk(CodeIndexFileChunkBase):
    metadata: Metadata | None = None
    vector: list[float] | None = None
    document: CodeIndexItem | None = None


class Edge(BaseModel):
    """A reference edge between two items, stored in SQLite.

    ``relation`` is the canonical edge kind ("calls", "imports", etc.) —
    indexed as a column for fast filter joins. ``meta`` carries the
    identifier payload (caller/callee names, paths) — none of it
    needs an index.
    """

    relation: str
    meta: dict = Field(default_factory=dict)
    file: CodeIndexItem


class References(BaseModel):
    parent: CodeIndexItem | None = None
    document_references: list[Edge] = Field(default_factory=list)
    referenced_by: list[Edge] = Field(default_factory=list)

    @property
    def safe_to_delete(self) -> bool:
        return not any(
            [
                self.document_references,
                self.referenced_by,
            ]
        )


# ── Reference summary (agent-facing, attached to CodeIndexResult) ────


class ReferenceTarget(BaseModel):
    """The other end of a reference edge — what the agent needs to
    follow up: the target's uuid (for ``codeindex_tree(id=…)``
    drill-down), name, full path, and a one-line summary of what the
    target *does* (extracted from the indexer's SUMMARY section,
    truncated to ~200 chars)."""

    id: str
    name: str
    path: str
    summary: str = ""


class CodeIndexResult(BaseModel):
    """Shape returned by :meth:`CodeIndex.search` / ``filter_items`` / ``get_item``.

    Mirrors the chroma metadata 1:1 plus retrieval-time fields
    (``commit``, ``score``, ``chunk_preview``, ``content``). Distinct
    from :class:`CodeIndexItemCreate` because:

    - ``type`` here is a free string ("entity" / "file" / "folder") —
      :class:`FileSystemType` only models the on-disk distinction and
      doesn't carry "entity".
    - ``score`` and ``chunk_preview`` only exist after a search hit.
    - ``content`` is read from the chroma document, not constructed.
    """

    model_config = ConfigDict(from_attributes=True)

    item_id: str
    name: str = ""
    type: str = ""
    kind: str = ""
    entity_type: str = ""
    path: str = ""
    parent_id: str = ""
    file_extension: str = ""
    repository_id: str = ""
    archived: bool = False
    timestamp: str = ""
    token_count: int = 0
    line_from: int | None = None
    line_to: int | None = None
    needs_refactoring: bool = False

    # Quality categoricals — empty string means "not assessed".
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

    # Multi-value categories.
    vulnerabilities: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    domain: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    layers: list[str] = Field(default_factory=list)
    patterns: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    file_issues: list[str] = Field(default_factory=list)

    # Retrieval-time fields.
    commit: str
    content: str = ""
    score: float | None = None
    chunk_preview: str | None = None

    # Reference graph — populated by ``codeindex_tree`` only.
    # Maps relation name (``calls``, ``called_by``, ``imports``,
    # ``imported_by``, …) to the full list of ``ReferenceTarget``s
    # for that edge kind. ``None`` here means either no tree-style
    # query was issued (``codeindex_query`` never populates it), or
    # there are no edges of any kind on this item. ``exclude_none=True``
    # strips the field from the response in both cases.
    references: dict[str, list[ReferenceTarget]] | None = None


class CodeIndexItem(CodeIndexItemCreate):
    references: References = Field(default_factory=References)
    archived: bool = False

    _chunks_override: list[CodeIndexFileChunk | CodeIndexFileChunkBase] | None = PrivateAttr(
        default=None
    )

    def __init__(self, **data: Any):
        chunks_data = data.pop("chunks", None)
        super().__init__(**data)
        if chunks_data is not None:
            if chunks_data and isinstance(chunks_data[0], dict):
                self._chunks_override = [
                    CodeIndexFileChunk(**c) if isinstance(c, dict) else c for c in chunks_data
                ]
            else:
                self._chunks_override = chunks_data

    @model_validator(mode="before")
    @classmethod
    def _convert_weaviate_types(cls, data):
        if isinstance(data, dict):
            return convert_weaviate_types(data)
        return data

    @property
    def chunks(self) -> list[CodeIndexFileChunk | CodeIndexFileChunkBase]:
        source = self._chunks_override if self._chunks_override is not None else self.content_chunks
        return sorted(source, key=lambda c: c.index)

    @chunks.setter
    def chunks(self, value: list[CodeIndexFileChunk | CodeIndexFileChunkBase]) -> None:
        self._chunks_override = value

    @property
    def uuid(self) -> str:
        return self.item_id

    @property
    def is_file(self) -> bool:
        return self.type == FileSystemType.FILE

    @property
    def is_folder(self) -> bool:
        return self.type == FileSystemType.FOLDER

    @property
    def has_parent(self) -> bool:
        return bool(self.parent_id)


CodeIndexItem.model_rebuild()
CodeIndexFileChunk.model_rebuild()


class FileSystemItemCount(BaseModel):
    files: int
    folders: int


class FileSystemItemArchivedCount(BaseModel):
    files: int
    archived_files: int


class FileSystemItemAggregatedCount(BaseModel):
    items: dict[str, FileSystemItemCount]
