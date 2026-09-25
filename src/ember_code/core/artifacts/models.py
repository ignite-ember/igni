"""Typed models for the artifact store.

An **artifact** is a document you author, revise and ship. It has a
stable identity (:class:`ArtifactSummary`) and an append-only chain of
immutable revisions (:class:`ArtifactVersion`). Writing does not
mutate — it appends ``n+1`` and moves the head — so "show me v1" and
"what was this built on when it was written" both stay answerable
after twenty revisions.

The load-bearing type here is :class:`VersionPin`. A derivation edge
points at *a specific version* of its source, not at the source
artifact. That single choice is what makes staleness computable:
``pin.n < source.head_n`` means the ground moved under a dependent,
and the pinned number is also the left-hand side of the diff that
tells the agent what to fix.
"""

from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from ember_code.core.artifacts.metadata_codec import ArtifactMetadataCodec

logger = logging.getLogger(__name__)


# ── Derivation ────────────────────────────────────────────────────────


class EdgeState(str, Enum):
    """Lifecycle of a ``DERIVED_FROM`` edge.

    ``CANDIDATE`` edges are written automatically from what the agent
    retrieved; ``CONFIRMED`` edges were cited in the write or
    confirmed by a human. **Only confirmed edges drive staleness** —
    a version the agent skimmed once is not a dependency, and
    treating it as one makes the staleness signal worthless.
    """

    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"


class VersionPin(BaseModel):
    """A reference to one exact version of one artifact.

    ``n`` is captured at read time and never updated in place —
    re-pinning after a reconcile writes a new value deliberately, via
    :meth:`ArtifactIndex.accept_reconcile`.
    """

    artifact_id: str
    n: int
    state: EdgeState = EdgeState.CANDIDATE
    at: str = ""
    session_id: str = ""

    @property
    def ref(self) -> str:
        """The ``artifact@n`` spelling used in tool arguments and prose."""
        return f"{self.artifact_id}@{self.n}"

    @classmethod
    def parse(cls, raw: str, *, state: EdgeState = EdgeState.CONFIRMED) -> VersionPin | None:
        """Parse ``artifact_id@n`` or a bare ``artifact_id``.

        A bare id yields ``n=0``, which the store reads as "pin to
        whatever head is at write time" — the agent citing a source
        by name should not have to know its version number, and
        resolving it at the write is what keeps the pin honest.
        Returns ``None`` for input that isn't a reference at all, so
        the caller can route it to :class:`Citation` instead.
        """
        text = (raw or "").strip()
        if not text or "://" in text:
            return None
        artifact_id, _, suffix = text.partition("@")
        artifact_id = artifact_id.strip()
        if not artifact_id:
            return None
        if not suffix:
            return cls(artifact_id=artifact_id, n=0, state=state)
        try:
            n = int(suffix)
        except ValueError:
            return None
        return cls(artifact_id=artifact_id, n=max(n, 0), state=state)


class Citation(BaseModel):
    """An external source — a URL, a file path, a fetched document.

    Distinct from :class:`VersionPin` because there is no version to
    pin and nothing to go stale against: we record what was read and
    when, and that is the whole contract.
    """

    url: str
    kind: str = ""
    title: str = ""
    fetched_at: str = ""

    @property
    def source_id(self) -> str:
        return ArtifactMetadataCodec().content_hash(self.url)


class SourceDeclaration(BaseModel):
    """What a write declares it was built on.

    Produced by :meth:`from_raw` from the ``sources`` argument every
    artifact-creating tool requires. Splitting refs from citations at
    this boundary is what lets the tool layer stay a thin string API
    while the store still gets typed pins.
    """

    pins: list[VersionPin] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)

    @classmethod
    def from_raw(
        cls,
        raw: list[str] | None,
        *,
        codec: ArtifactMetadataCodec | None = None,
    ) -> SourceDeclaration:
        codec = codec or ArtifactMetadataCodec()
        now = codec.now_iso()
        pins: list[VersionPin] = []
        citations: list[Citation] = []
        unresolved: list[str] = []
        for item in raw or []:
            text = (item or "").strip()
            if not text:
                continue
            if "://" in text:
                citations.append(Citation(url=text, kind=_citation_kind(text), fetched_at=now))
                continue
            pin = VersionPin.parse(text, state=EdgeState.CONFIRMED)
            if pin is None:
                unresolved.append(text)
            else:
                pins.append(pin.model_copy(update={"at": now}))
        return cls(pins=pins, citations=citations, unresolved=unresolved)

    @property
    def is_empty(self) -> bool:
        return not self.pins and not self.citations


def _citation_kind(url: str) -> str:
    lowered = url.lower()
    if lowered.endswith(".pdf"):
        return "pdf"
    if "youtube.com" in lowered or "youtu.be" in lowered:
        return "video"
    if "wikipedia.org" in lowered:
        return "wiki"
    if "arxiv.org" in lowered:
        return "paper"
    return "web"


# ── Artifacts and versions ────────────────────────────────────────────


class ArtifactVersion(BaseModel):
    """One immutable revision."""

    artifact_id: str
    version_id: str = ""
    n: int = 1
    content: str = ""
    content_hash: str = ""
    head: bool = False
    created_at: str = ""
    author: str = ""
    session_id: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)

    @property
    def ref(self) -> str:
        return f"{self.artifact_id}@{self.n}"


class ArtifactSummary(BaseModel):
    """Stable identity plus the head pointer.

    ``head_n`` is the only mutable number in the store, and every
    staleness comparison reads it.
    """

    artifact_id: str
    name: str = ""
    kind: str = "note"
    head_n: int = 0
    created_at: str = ""
    updated_at: str = ""
    author: str = ""
    origin: str = ""
    version_count: int = 0

    @property
    def is_imported(self) -> bool:
        """Imported copies carry an origin and have no live source.

        Derivation is project-local by design, so an artifact shared
        in from elsewhere records where it came from as a string and
        will never go stale against it. Naming that explicitly here
        keeps callers from treating ``origin`` as a traversable link.
        """
        return bool(self.origin)


class ArtifactSearchHit(BaseModel):
    """One head-version hit from the vector index."""

    artifact_id: str
    name: str = ""
    kind: str = ""
    n: int = 0
    content: str = ""
    score: float | None = None
    project: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)

    @property
    def ref(self) -> str:
        return f"{self.artifact_id}@{self.n}"


class ArtifactSearchResponse(BaseModel):
    query: str
    results: list[ArtifactSearchHit] = Field(default_factory=list)
    total: int = 0


class LineageEdge(BaseModel):
    """One derivation edge, rendered from either direction.

    ``source_head_n`` is carried alongside ``pinned_n`` so a caller
    can tell "pinned to v1, source is at v3" without a second query.
    """

    from_artifact_id: str
    from_n: int
    to_artifact_id: str
    to_n: int
    to_name: str = ""
    from_name: str = ""
    state: EdgeState = EdgeState.CANDIDATE
    pinned_n: int = 0
    source_head_n: int = 0
    at: str = ""

    @property
    def is_stale(self) -> bool:
        return self.state is EdgeState.CONFIRMED and self.pinned_n < self.source_head_n


class ArtifactLineage(BaseModel):
    """Both directions around one version."""

    artifact_id: str
    n: int = 0
    sources: list[LineageEdge] = Field(default_factory=list)
    dependents: list[LineageEdge] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)

    @property
    def stale_sources(self) -> list[LineageEdge]:
        return [e for e in self.sources if e.is_stale]

    @property
    def candidates(self) -> list[LineageEdge]:
        return [e for e in self.sources if e.state is EdgeState.CANDIDATE]


class StaleDependent(BaseModel):
    """A confirmed derivation whose source has moved since it was pinned."""

    dependent_artifact_id: str
    dependent_name: str = ""
    dependent_n: int = 0
    source_artifact_id: str
    source_name: str = ""
    pinned_n: int = 0
    source_head_n: int = 0

    @property
    def versions_behind(self) -> int:
        return max(self.source_head_n - self.pinned_n, 0)

    @property
    def summary(self) -> str:
        return (
            f"{self.dependent_name or self.dependent_artifact_id} "
            f"is pinned to {self.source_name or self.source_artifact_id}"
            f"@{self.pinned_n}, now at @{self.source_head_n} "
            f"({self.versions_behind} version(s) behind)"
        )


class ReconcilePlan(BaseModel):
    """What changed under a dependent, and the proposed patch.

    The plan is a proposal — nothing is written until
    :meth:`ArtifactIndex.accept_reconcile` appends the new version and
    re-pins the edge. Keeping the diff and the proposal in one object
    means the reviewer sees the evidence next to the suggestion.
    """

    dependent_artifact_id: str
    dependent_name: str = ""
    dependent_n: int = 0
    dependent_content: str = ""
    source_artifact_id: str
    source_name: str = ""
    pinned_n: int = 0
    source_head_n: int = 0
    source_diff: str = ""
    proposed_content: str = ""

    @property
    def has_proposal(self) -> bool:
        return bool(self.proposed_content.strip())


# ── Operation results ─────────────────────────────────────────────────


class ArtifactWriteResult(BaseModel):
    """Result of a create/revise."""

    success: bool = True
    message: str = ""
    error: str | None = None
    artifact_id: str | None = None
    n: int = 0
    version_id: str | None = None
    recorded_sources: int = 0
    captured_candidates: int = 0

    @classmethod
    def ok(
        cls,
        message: str,
        *,
        artifact_id: str | None = None,
        n: int = 0,
        version_id: str | None = None,
        recorded_sources: int = 0,
        captured_candidates: int = 0,
    ) -> ArtifactWriteResult:
        return cls(
            success=True,
            message=message,
            artifact_id=artifact_id,
            n=n,
            version_id=version_id,
            recorded_sources=recorded_sources,
            captured_candidates=captured_candidates,
        )

    @classmethod
    def fail(cls, error: str) -> ArtifactWriteResult:
        return cls(success=False, error=error, message=error)

    @classmethod
    def from_ingest(cls, result: IngestResult, *, source_label: str) -> ArtifactWriteResult:
        """Translate an :class:`IngestResult` into the write surface.

        Zero documents is a failure *here* even though it isn't one
        for the ingester: a user who pointed us at a file expects
        content, and "stored 0" reads as success in a tool response.
        """
        if not result.success:
            return cls.fail(result.error or f"Failed to ingest {source_label}")
        if result.count == 0:
            return cls.fail(f"No readable content found in {source_label}")
        return cls.ok(f"Stored {result.count} document(s) from {source_label}")


class ArtifactDeleteResult(BaseModel):
    deleted: int = 0
    errors: list[str] = Field(default_factory=list)
    reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors


class ArtifactStatus(BaseModel):
    enabled: bool = False
    collection_name: str = ""
    document_count: int = 0
    version_count: int = 0
    stale_count: int = 0
    pending_candidates: int = 0
    embedder: str = ""


class ArtifactSyncResult(BaseModel):
    """Result of a YAML sync.

    ``errors`` accumulates per-entry insert failures so partial
    failures have a real error surface instead of a swallowed
    ``except Exception: log``.
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


# ── Ingestion ─────────────────────────────────────────────────────────


class IngestResult(BaseModel):
    """Result of a single ingestion call (URL or file path).

    A successful call with 0 chunks (e.g. an empty text file) is
    ``count=0, error=None`` — the caller decides whether that is a
    UX-level failure.
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
    """Typed wrapper for the caller-supplied metadata dict."""

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

    ``list[Any]`` on the classmethod input is deliberate — this is the
    *only* place ``Any`` should live for Agno document handoff, so the
    typing hole is contained. Do not spread it past
    :meth:`from_agno_documents`.
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

        ``None`` values are dropped entirely (the caller doesn't want
        ``{"author": "None"}``); everything else is str-coerced.
        """
        if not isinstance(meta, dict):
            return {}
        return {str(k): str(v) for k, v in meta.items() if v is not None}

    @property
    def is_empty(self) -> bool:
        return not self.chunks


# ── Git-shared file ───────────────────────────────────────────────────


class ArtifactEntry(BaseModel):
    """A single entry in ``.igni/artifacts.yaml``.

    Head content only — version history is local graph state. The
    alternative (writing the chain to the repo) makes every artifact
    edit produce a diff nobody can review, and the file stops being
    worth having. Consequence, stated plainly: a fresh clone has
    artifacts but no lineage and no history.
    """

    id: str
    content: str
    source: str = ""
    added_at: str = ""
    name: str = ""
    kind: str = "note"
    n: int = 1
    sources: list[str] = Field(default_factory=list)

    @classmethod
    def from_content(
        cls,
        content: str,
        *,
        source: str = "",
        name: str = "",
        kind: str = "note",
        codec: ArtifactMetadataCodec | None = None,
    ) -> ArtifactEntry:
        codec = codec or ArtifactMetadataCodec()
        return cls(
            id=codec.content_hash(content),
            content=content,
            source=source,
            name=name,
            kind=kind,
            added_at=codec.now_iso(),
        )


class ArtifactYamlFile(BaseModel):
    """On-disk YAML shape for ``.igni/artifacts.yaml``."""

    version: int = 2
    synced_at: str = ""
    entries: list[ArtifactEntry] = Field(default_factory=list)

    @classmethod
    def load_from(cls, path: Path) -> list[ArtifactEntry]:
        """Read an artifacts YAML, returning ``[]`` on any parse failure.

        File-missing, yaml-parse error and wrong-shape all surface as
        ``[]`` plus a warning — a sync should still complete against a
        corrupt file rather than taking the session down with it.
        """
        if not path.exists():
            return []
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
        except Exception:
            logger.warning("Failed to load artifacts file: %s", path)
            return []
        if not isinstance(data, dict):
            return []
        try:
            return cls.model_validate(data).entries
        except Exception:
            logger.warning("artifacts file at %s has unexpected shape — ignoring", path)
            return []

    @classmethod
    def write_to(
        cls,
        path: Path,
        entries: list[ArtifactEntry],
        *,
        codec: ArtifactMetadataCodec | None = None,
    ) -> None:
        """Write entries to ``path`` — creates parent dirs as needed."""
        codec = codec or ArtifactMetadataCodec()
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


class ArtifactProvenance(BaseModel):
    """Typed carrier for the per-version provenance metadata.

    :meth:`to_metadata` / :meth:`from_metadata` are the sole crossings
    — no string-keyed dict access at call sites.
    """

    added_at: str = ""
    author: str = ""
    session_id: str = ""

    def to_metadata(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.added_at:
            out["added_at"] = self.added_at
        if self.author:
            out["author"] = self.author
        if self.session_id:
            out["session_id"] = self.session_id
        return out

    @classmethod
    def from_metadata(cls, meta: dict[str, str] | None) -> ArtifactProvenance:
        if not meta:
            return cls()
        return cls(
            added_at=str(meta.get("added_at") or ""),
            author=str(meta.get("author") or ""),
            session_id=str(meta.get("session_id") or ""),
        )
