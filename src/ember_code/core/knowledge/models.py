"""Pydantic models for the knowledge system."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from ember_code.core.knowledge.metadata_codec import KnowledgeMetadataCodec

logger = logging.getLogger(__name__)


class ChromaQueryPage(BaseModel):
    """Typed wrapper around one page of :meth:`ChunksCollection.query`.

    Chroma's raw ``query`` returns ``dict[str, list[list[Any]]]`` — one
    outer list per query-text and one inner list per hit. We only ever
    issue single-query calls, so callers want the *first row*: this
    model exposes :meth:`first_row_iter` for that path so the
    ``or [[]]`` sentinel dance disappears from the caller.
    """

    ids: list[list[str]] = Field(default_factory=list)
    documents: list[list[str | None]] = Field(default_factory=list)
    metadatas: list[list[dict[str, Any] | None]] = Field(default_factory=list)
    distances: list[list[float | None]] = Field(default_factory=list)

    @classmethod
    def from_chroma(cls, raw: dict[str, Any]) -> ChromaQueryPage:
        return cls(
            ids=raw.get("ids") or [],
            documents=raw.get("documents") or [],
            metadatas=raw.get("metadatas") or [],
            distances=raw.get("distances") or [],
        )

    def first_row_iter(
        self,
    ) -> Iterator[tuple[str, str | None, dict[str, Any] | None, float | None]]:
        """Yield ``(id, document, metadata, distance)`` for the first query row.

        Empty pages yield nothing — callers just iterate.
        """
        if not self.ids or not self.ids[0]:
            return
        ids_row = self.ids[0]
        docs_row = self.documents[0] if self.documents else []
        metas_row = self.metadatas[0] if self.metadatas else []
        dists_row = self.distances[0] if self.distances else []
        yield from zip(ids_row, docs_row, metas_row, dists_row, strict=False)

    @property
    def is_empty(self) -> bool:
        return not self.ids or not self.ids[0]


class ChromaGetPage(BaseModel):
    """Typed wrapper around one page of :meth:`DocumentsCollection.get_by_ids` / :meth:`get_all`.

    Chroma's raw ``get`` returns ``dict[str, list[Any]]`` — flat lists
    (no outer nesting, unlike ``query``). Same seam-containment
    principle: :meth:`rows` yields typed tuples so callers stop
    fishing keys out of an ``Any``-typed dict.
    """

    ids: list[str] = Field(default_factory=list)
    documents: list[str | None] = Field(default_factory=list)
    metadatas: list[dict[str, Any] | None] = Field(default_factory=list)

    @classmethod
    def from_chroma(cls, raw: dict[str, Any]) -> ChromaGetPage:
        return cls(
            ids=raw.get("ids") or [],
            documents=raw.get("documents") or [],
            metadatas=raw.get("metadatas") or [],
        )

    def rows(
        self,
    ) -> Iterator[tuple[str, str | None, dict[str, Any] | None]]:
        """Yield ``(id, document, metadata)`` per stored row."""
        yield from zip(self.ids, self.documents, self.metadatas, strict=False)

    @property
    def is_empty(self) -> bool:
        return not self.ids


class ParentRow(BaseModel):
    """One parent-doc row fetched during roll-up.

    Kills the anonymous ``dict[str, tuple[str, dict]]`` intermediate
    that the old ``_roll_up_chunks`` used to keep — the rollup builds
    a ``dict[parent_id, ParentRow]`` now.
    """

    document: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)


class DeleteOutcome(BaseModel):
    """Per-entry Result from :meth:`KnowledgeStore.delete_entry`.

    Composed into the aggregate :class:`KnowledgeDeleteResult` by
    :meth:`KnowledgeIndex.delete_by_query` — replaces the
    try/except-per-entry pattern that used to swallow errors inside
    the delete loop.
    """

    entry_id: str
    error: str | None = None

    @classmethod
    def success(cls, entry_id: str) -> DeleteOutcome:
        return cls(entry_id=entry_id, error=None)

    @classmethod
    def fail(cls, entry_id: str, error: str) -> DeleteOutcome:
        return cls(entry_id=entry_id, error=error)

    @property
    def ok(self) -> bool:
        return self.error is None


class KnowledgeAddResult(BaseModel):
    """Result of adding content to the knowledge base."""

    success: bool = True
    message: str = ""
    error: str | None = None
    entry_id: str | None = None

    @classmethod
    def ok(cls, message: str, *, entry_id: str | None = None) -> KnowledgeAddResult:
        return cls(success=True, message=message, entry_id=entry_id)

    @classmethod
    def fail(cls, error: str) -> KnowledgeAddResult:
        return cls(success=False, error=error)

    @classmethod
    def from_ingest(
        cls,
        result: IngestResult,
        *,
        source_label: str,
    ) -> KnowledgeAddResult:
        """Translate an :class:`IngestResult` to a :class:`KnowledgeAddResult`.

        Pins the two-Result-model bridge in one place so
        ``knowledge_ops.py`` doesn't hand-roll the same if/else at each
        callsite. Distinguishes "ingest failed" (``result.error`` set)
        from "ingest succeeded but produced 0 chunks"
        (``result.count == 0``) — both surface as ``fail``, but with
        different messages preserving the original UX.
        """
        if result.error:
            return cls.fail(result.error)
        if result.count == 0:
            return cls.fail(f"No content extracted from {source_label}")
        return cls.ok(f"Added {result.count} document(s) from {source_label}")


class KnowledgeSearchResult(BaseModel):
    """A single search result from the knowledge base.

    ``content`` is the (up-to-1000-char) chunk preview that hit the
    query; ``parent_content`` is the full parent document text —
    callers that want the whole entry use ``parent_content``, callers
    rendering compact hit cards use ``content``.
    """

    entry_id: str = ""
    content: str = ""
    name: str = ""
    source: str = ""
    project: str = ""
    parent_content: str = ""
    score: float | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    # Support ``result["key"]`` legacy access alongside attribute access
    # so callers migrating off ``list[dict]`` don't all have to flip in
    # the same commit. Prefer ``.attr`` — this shim is transitional.
    def __getitem__(self, key: str) -> object:
        return getattr(self, key)

    @property
    def truncated_content(self) -> str:
        """First 1000 chars + ellipsis — matches the panel's compact preview."""
        if len(self.content) > 1000:
            return self.content[:1000] + "..."
        return self.content


class KnowledgeSearchResponse(BaseModel):
    """Collection of search results."""

    query: str
    results: list[KnowledgeSearchResult] = Field(default_factory=list)
    total: int = 0


class KnowledgeIndexEntry(BaseModel):
    """One entry in the knowledge index — the shape returned by
    :meth:`KnowledgeIndex.list_entries`.

    Distinct from :class:`KnowledgeEntry` (that one describes the
    ``.ember/knowledge.yaml`` on-disk row and carries ``added_at``).
    This model carries the full metadata dict the panel needs to
    render Browse/Detail views.
    """

    id: str = ""
    content: str = ""
    source: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)

    def __getitem__(self, key: str) -> object:
        return getattr(self, key)

    def get(self, key: str, default: object = None) -> object:
        """Dict-compat shim — mirrors ``dict.get`` for transitional
        callers that still consume the legacy dict shape."""
        return getattr(self, key, default)


class KnowledgeDeleteResult(BaseModel):
    """Result of :meth:`KnowledgeIndex.delete_by_query`.

    ``deleted`` counts successful per-entry deletes; ``errors``
    accumulates per-entry failures so the operation has a real error
    surface instead of a swallowed ``except Exception: log``.
    """

    deleted: int = 0
    errors: list[str] = Field(default_factory=list)
    reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors


class KnowledgeStatus(BaseModel):
    """Current status of the knowledge base."""

    enabled: bool = False
    collection_name: str = ""
    document_count: int = 0
    embedder: str = ""


class KnowledgeSyncResult(BaseModel):
    """Result of a knowledge sync operation.

    ``errors`` accumulates per-entry insert failures from
    :meth:`KnowledgeSyncer.sync_file_to_db` so partial failures have a
    real error surface instead of a swallowed ``except Exception: log``
    — mirrors :class:`KnowledgeDeleteResult.errors`.
    """

    direction: str = ""  # "file_to_db" or "db_to_file"
    new_entries: int = 0
    existing_entries: int = 0
    total_entries: int = 0
    message: str = ""
    error: str | None = None
    errors: list[str] = Field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.error:
            return f"Sync error: {self.error}"
        base = (
            f"Already in sync ({self.total_entries} entries)"
            if self.new_entries == 0
            else (
                f"Synced {self.new_entries} new entries "
                f"({self.existing_entries} existing, {self.total_entries} total)"
            )
        )
        if self.errors:
            return f"{base} — {len(self.errors)} error(s)"
        return base


class _BestChunkForParent(BaseModel):
    """Roll-up struct for :class:`ChunkResultRollup`.

    Best (highest-score) chunk found for one parent doc during a chunk
    search. Module-private — replaces the anonymous ``dict[str, dict]``
    that used to live in the roll-up loop.
    """

    score: float
    chunk: str
    chunk_meta: dict[str, str] = Field(default_factory=dict)


class IngestResult(BaseModel):
    """Result of a single ingestion call (URL or file path).

    Replaces the ``int`` + raise ``IngestError`` contract the old
    :class:`Ingester` used. ``count`` is the number of documents
    stored; ``error`` is set when ingestion failed for a reason worth
    surfacing to the user.

    A successful call with 0 chunks (e.g. an empty text file) is
    represented as ``count=0, error=None`` — the caller decides
    whether to treat that as a UX-level failure. This mirrors the
    same distinction :class:`KnowledgeSyncResult` makes between
    "sync ran, nothing to do" and "sync errored".
    """

    count: int = 0
    error: str | None = None

    @classmethod
    def ok(cls, count: int) -> IngestResult:
        return cls(count=count, error=None)

    @classmethod
    def fail(cls, error: str) -> IngestResult:
        return cls(count=0, error=error)

    @property
    def success(self) -> bool:
        return self.error is None


class IngestMetadata(BaseModel):
    """Typed wrapper for the caller-supplied metadata dict.

    Public method signatures accept ``dict[str, str] | IngestMetadata | None``
    and coerce internally so knowledge_ops.py callers don't need to
    flip in the same commit. ``merged_with`` returns a new
    ``dict[str, str]`` combining the base with per-document metadata
    Agno readers surface on ``document.meta_data``.
    """

    base: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def coerce(cls, value: dict[str, str] | IngestMetadata | None) -> IngestMetadata:
        if value is None:
            return cls()
        if isinstance(value, IngestMetadata):
            return value
        return cls(base=dict(value))

    def merged_with(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        merged = dict(self.base)
        if extra:
            merged.update(extra)
        return merged


class IngestedContent(BaseModel):
    """Pydantic wrapper around a batch of Agno-reader documents.

    Owns the two behaviours that used to live as free helpers in
    ``ingest.py``:

    * The ``getattr(d, "content", None)`` filter that turns Agno's
      ``list[Document]`` into ``list[str]``.
    * The ``_string_meta`` coercion that hardens
      ``document.meta_data`` (typed ``Any`` upstream) into the
      ``dict[str, str]`` Chroma's metadata API requires.

    ``list[Any]`` on the classmethod input is deliberate — this is
    the *only* place ``Any`` should live for Agno document handoff,
    so the typing hole is contained. Do not spread the ``Any`` past
    ``from_agno_documents``.
    """

    chunks: list[str] = Field(default_factory=list)
    source_metadata: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_agno_documents(cls, documents: list[Any]) -> IngestedContent:
        chunks = [c for c in (getattr(d, "content", None) for d in documents) if c]
        source_metadata: dict[str, str] = {}
        if documents:
            source_metadata = cls._coerce_meta(getattr(documents[0], "meta_data", None))
        return cls(chunks=chunks, source_metadata=source_metadata)

    @staticmethod
    def _coerce_meta(meta: Any) -> dict[str, str]:
        """Turn arbitrary reader metadata into ``dict[str, str]``.

        Chroma rejects non-string metadata values at write time.
        ``None`` values are dropped entirely (the caller doesn't want
        ``{"author": "None"}``); everything else is str-coerced.
        """
        if not isinstance(meta, dict):
            return {}
        return {str(k): str(v) for k, v in meta.items() if v is not None}

    @property
    def is_empty(self) -> bool:
        return not self.chunks


class KnowledgeEntry(BaseModel):
    """A single entry in ``.ember/knowledge.yaml``.

    ``id`` is a 16-hex prefix of ``sha256(content)`` — stable across
    round-trips so diffing YAML ↔ Chroma is a set-diff on ids. The
    hash + timestamp policy both flow through
    :class:`KnowledgeMetadataCodec`, so tests can inject a codec to
    pin either value.
    """

    id: str
    content: str
    source: str = ""
    added_at: str = ""

    @classmethod
    def from_content(
        cls,
        content: str,
        *,
        source: str = "",
        codec: KnowledgeMetadataCodec | None = None,
    ) -> KnowledgeEntry:
        codec = codec or KnowledgeMetadataCodec()
        return cls(
            id=codec.content_hash(content),
            content=content,
            source=source,
            added_at=codec.now_iso(),
        )


class KnowledgeYamlFile(BaseModel):
    """On-disk YAML shape for ``.ember/knowledge.yaml``.

    Absorbs the file I/O that used to be inlined in
    :class:`KnowledgeSyncer.load_file` / ``save_file`` — the syncer
    delegates to :meth:`load_from` / :meth:`write_to` classmethods
    so all three failure modes (missing file, bad yaml, wrong shape)
    are handled in one place.
    """

    version: int = 1
    synced_at: str = ""
    entries: list[KnowledgeEntry] = Field(default_factory=list)

    @classmethod
    def load_from(cls, path: Path) -> list[KnowledgeEntry]:
        """Read a knowledge YAML, returning an empty list on any parse failure.

        Absorbs three try/except branches that previously lived in
        :class:`KnowledgeSyncer.load_file`: file-missing, yaml-parse
        error, and wrong-shape (validation) error. Each of those
        surfaces as ``[]`` + a warning log — the sync operation
        should still complete on a corrupt file.
        """
        if not path.exists():
            return []
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
        except Exception:
            logger.warning("Failed to load knowledge file: %s", path)
            return []
        if not isinstance(data, dict):
            return []
        try:
            return cls.model_validate(data).entries
        except Exception:
            logger.warning("knowledge file at %s has unexpected shape — ignoring", path)
            return []

    @classmethod
    def write_to(
        cls,
        path: Path,
        entries: list[KnowledgeEntry],
        *,
        codec: KnowledgeMetadataCodec | None = None,
    ) -> None:
        """Write entries to ``path`` — creates parent dirs as needed."""
        codec = codec or KnowledgeMetadataCodec()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = cls(synced_at=codec.now_iso(), entries=entries).model_dump()
        with open(path, "w") as f:
            yaml.dump(
                payload,
                f,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            )


class EntryProvenance(BaseModel):
    """Typed carrier for the per-entry provenance metadata.

    Replaces the raw ``{"added_at": ...}`` dict smuggled through the
    :class:`KnowledgeIndex` boundary. :meth:`to_metadata` /
    :meth:`from_metadata` are the sole crossings — no string-keyed
    dict access at call sites.
    """

    added_at: str = ""

    def to_metadata(self) -> dict[str, str]:
        """Serialize to the ``dict[str, str]`` Chroma metadata accepts."""
        out: dict[str, str] = {}
        if self.added_at:
            out["added_at"] = self.added_at
        return out

    @classmethod
    def from_metadata(cls, meta: dict[str, str] | None) -> EntryProvenance:
        """Round-trip back from a Chroma metadata dict."""
        if not meta:
            return cls()
        return cls(added_at=str(meta.get("added_at") or ""))
