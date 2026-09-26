"""Typed wire schemas for the artifacts panel.

Consolidates every artifact wire type in one place, matching the
existing ``schemas_run.py`` / ``schemas_pause.py`` convention so the
controller module (``server_artifacts.py``) stays focused on
behaviour.

These types are the privacy boundary for the panel the same way
``ExportBundle.to_payload`` is for the share path: they are
hand-declared, so a field added to the store does not reach a client
until someone decides it should.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ArtifactStatus(BaseModel):
    """Panel header. ``embedder`` carries the active embedding
    provider (empty when artifacts are disabled)."""

    enabled: bool
    collection_name: str = ""
    document_count: int = 0
    version_count: int = 0
    stale_count: int = 0
    pending_candidates: int = 0
    embedder: str = ""


class ArtifactHit(BaseModel):
    """One search hit. ``metadata`` values are stringified at wire
    time so the FE never has to guess types."""

    artifact_id: str = ""
    name: str = ""
    kind: str = ""
    n: int = 0
    content: str = ""
    score: float | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class ArtifactListEntry(BaseModel):
    """One row of the Browse tab — ``preview`` renders the collapsed
    row and the rest is pulled on click."""

    id: str
    name: str
    source: str = ""
    kind: str = ""
    n: int = 0
    version_count: int = 0
    size: int = 0
    preview: str = ""
    added_at: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)


class ArtifactGetResult(BaseModel):
    """One artifact's full detail at one version.

    Error paths (disabled, not found, no such version) populate
    ``error`` and leave the content fields empty so the panel renders
    a clear message instead of a blank document.
    """

    id: str = ""
    name: str = ""
    source: str = ""
    kind: str = ""
    n: int = 0
    head_n: int = 0
    is_head: bool = False
    content: str = ""
    created_at: str = ""
    author: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)
    error: str = ""


class ArtifactVersionRow(BaseModel):
    """One entry in the version switcher."""

    n: int
    created_at: str = ""
    author: str = ""
    size: int = 0
    is_head: bool = False


class ArtifactLineageRow(BaseModel):
    """One derivation edge, in whichever direction it was asked for.

    ``pinned_n`` and ``head_n`` are both sent so the panel can render
    "pinned to v1, now at v3" without a second round trip, which is
    the whole question a reader has about a source.
    """

    artifact_id: str
    name: str = ""
    n: int = 0
    pinned_n: int = 0
    head_n: int = 0
    state: str = "candidate"
    is_stale: bool = False


class ArtifactLineageView(BaseModel):
    """Both directions around one version."""

    artifact_id: str
    n: int = 0
    sources: list[ArtifactLineageRow] = Field(default_factory=list)
    dependents: list[ArtifactLineageRow] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)


class ArtifactStaleRow(BaseModel):
    """One out-of-date dependency for the review list."""

    artifact_id: str
    name: str = ""
    n: int = 0
    source_artifact_id: str = ""
    source_name: str = ""
    pinned_n: int = 0
    source_head_n: int = 0
    versions_behind: int = 0


class ArtifactExportResult(BaseModel):
    """Where a local export landed."""

    path: str = ""
    error: str = ""


class ArtifactShareResult(BaseModel):
    """The URL of a published snapshot, and which version it holds."""

    url: str = ""
    version: int = 0
    error: str = ""


class ArtifactRemoveResult(BaseModel):
    """Delete outcome — ``removed`` is False for both "not found" and
    "disabled" (``error`` differentiates)."""

    removed: bool
    error: str = ""


class ArtifactGraphNode(BaseModel):
    """One node in the picture drawn beside a result."""

    id: str
    label: str
    kind: str = "artifact"
    version: int | None = None


class ArtifactGraphEdge(BaseModel):
    source: str
    target: str
    label: str = ""
    stale: bool = False


class ArtifactGraphView(BaseModel):
    nodes: list[ArtifactGraphNode] = Field(default_factory=list)
    edges: list[ArtifactGraphEdge] = Field(default_factory=list)


class ArtifactQueryResult(BaseModel):
    """A question, the Cypher written for it, and what came back.

    ``cypher`` is returned even when the run failed — the panel puts
    it in an editable box, so a query that was nearly right is one
    correction away rather than a dead end.
    """

    question: str = ""
    cypher: str = ""
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    graph: ArtifactGraphView = Field(default_factory=ArtifactGraphView)
    truncated: bool = False
    attempts: int = 0
    unanswerable: bool = False
    error: str = ""
