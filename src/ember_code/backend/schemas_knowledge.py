"""Typed wire schemas for the knowledge-base panel.

Consolidates the four knowledge wire types (``KnowledgeHit``,
``KnowledgeListEntry``, ``KnowledgeGetResult``,
``KnowledgeRemoveResult``) previously defined inside
:mod:`ember_code.backend.server_knowledge` alongside the procedural
RPC functions, plus the top-level ``KnowledgeStatus`` panel-header
model previously living inline in
:mod:`ember_code.backend.server`.

Keeping schemas in a sibling file (matching the existing
``schemas_run.py`` / ``schemas_pause.py`` convention) lets the
controller module (``server_knowledge.py``) stay focused on
behaviour.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class KnowledgeStatus(BaseModel):
    """Wire shape for :meth:`KnowledgeController.status` — KB panel
    header. ``embedder`` carries the active embedding provider
    (empty when KB disabled)."""

    enabled: bool
    collection_name: str
    document_count: int
    embedder: str


class KnowledgeHit(BaseModel):
    """One search hit — surfaced in the Browse panel and by the
    ``/knowledge search`` slash command. ``metadata`` values are
    stringified at wire time so the FE never has to guess types."""

    name: str
    content: str
    score: float
    metadata: dict[str, str]


class KnowledgeListEntry(BaseModel):
    """One row of :meth:`KnowledgeController.list` — the Browse tab uses
    ``preview`` for the collapsed row and pulls the rest on click."""

    id: str
    name: str
    source: str
    size: int
    preview: str
    added_at: str
    kind: str
    metadata: dict[str, str]


class KnowledgeGetResult(BaseModel):
    """One document's full detail. Error paths (KB disabled,
    entry not found) populate ``error`` and leave the content
    fields empty so the panel renders a clear message instead of
    an empty preview."""

    id: str = ""
    name: str = ""
    source: str = ""
    content: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)
    error: str = ""


class KnowledgeRemoveResult(BaseModel):
    """Delete outcome — ``removed`` is False for both "not found"
    and "KB disabled" cases (``error`` differentiates)."""

    removed: bool
    error: str = ""
