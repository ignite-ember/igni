"""Knowledge-base RPCs — search / add / list / get / remove.

Extracted from :mod:`ember_code.backend.server`. Five free
functions taking ``BackendServer`` as arg — the class holds
one-line delegates. All operations route through
:class:`Session.knowledge_mgr`, which owns the vector-store
connection and the ingest dispatch. Any function that hits the
raw ``knowledge`` handle returns a graceful "KB is disabled"
result when the session's KB is unset.

Rule 2 clean — all imports at module top.
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from pydantic import BaseModel

from ember_code.protocol import messages as msg

if TYPE_CHECKING:
    from ember_code.backend.server import BackendServer

logger = logging.getLogger(__name__)


class KnowledgeHit(BaseModel):
    """One search hit — surfaced in the Browse panel and by the
    ``/knowledge search`` slash command. ``metadata`` values are
    stringified at wire time so the FE never has to guess types."""

    name: str
    content: str
    score: float
    metadata: dict[str, str]


class KnowledgeListEntry(BaseModel):
    """One row of :func:`knowledge_list` — the Browse tab uses
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
    metadata: dict[str, str] = {}
    error: str = ""


class KnowledgeRemoveResult(BaseModel):
    """Delete outcome — ``removed`` is False for both "not found"
    and "KB disabled" cases (``error`` differentiates)."""

    removed: bool
    error: str = ""


async def knowledge_search(backend: "BackendServer", query: str) -> list[KnowledgeHit]:
    """Search the knowledge base — one :class:`KnowledgeHit` per
    result. Metadata values are stringified for the wire."""
    response = await backend._session.knowledge_mgr.search(query)
    return [
        KnowledgeHit(
            name=r.name,
            content=r.content,
            score=r.score,
            metadata={k: str(v) for k, v in r.metadata.items()},
        )
        for r in response.results
    ]


async def knowledge_add(backend: "BackendServer", source: str) -> msg.Info:
    """Add content to the knowledge base from the panel. Dispatch
    rules mirror ``/knowledge add <source>``: HTTP URLs → URL
    ingest, path-shaped strings → file/dir ingest, anything else
    → inline text."""
    mgr = backend._session.knowledge_mgr
    if source.startswith(("http://", "https://")):
        result = await mgr.add_url(source)
    elif "/" in source or source.startswith("."):
        result = await mgr.add_path(source)
    else:
        result = await mgr.add(text=source)
    if not result.success:
        return msg.Info(text=result.error or "Add failed.")
    return msg.Info(text=result.message)


async def knowledge_list(backend: "BackendServer") -> list[KnowledgeListEntry]:
    """Every document in the KB — used by the panel's Browse tab.

    ``name`` is the source basename or the first non-empty line
    of content when no source path is available (e.g. inline
    text), so the Browse list always has a meaningful label.
    """
    knowledge = backend._session.knowledge_mgr.knowledge
    if knowledge is None:
        return []
    try:
        entries = await knowledge.list_entries()
    except Exception as exc:
        logger.debug("knowledge_list failed: %s", exc)
        return []

    out: list[KnowledgeListEntry] = []
    for e in entries:
        content = e.get("content") or ""
        meta = e.get("metadata") or {}
        out.append(
            KnowledgeListEntry(
                id=e.get("id", ""),
                name=_name_for(e),
                source=e.get("source", ""),
                size=len(content),
                preview=content[:240],
                added_at=str(meta.get("added_at", "")),
                kind=str(meta.get("kind", "")),
                metadata={k: str(v) for k, v in meta.items() if v is not None},
            )
        )
    # Newest first when ``added_at`` is comparable; otherwise
    # stable order.
    out.sort(key=lambda d: d.added_at, reverse=True)
    return out


def _name_for(entry: dict) -> str:
    """Best display label for a KB entry — source basename, or
    first non-empty line of content for inline text."""
    source = (entry.get("source") or "").strip()
    if source:
        if source.startswith(("http://", "https://")):
            return source
        return PurePosixPath(source).name or source
    content = (entry.get("content") or "").strip()
    for line in content.splitlines():
        line = line.strip().lstrip("# ").strip()
        if line:
            return line[:80]
    return "(untitled)"


async def knowledge_get(backend: "BackendServer", entry_id: str) -> KnowledgeGetResult:
    """Full content for one document — used by the detail page."""
    knowledge = backend._session.knowledge_mgr.knowledge
    if knowledge is None:
        return KnowledgeGetResult(error="Knowledge base is disabled.")
    try:
        entries = await knowledge.list_entries()
    except Exception as exc:
        return KnowledgeGetResult(error=f"knowledge_get failed: {exc}")
    match = next((e for e in entries if e.get("id") == entry_id), None)
    if not match:
        return KnowledgeGetResult(error=f"Document {entry_id} not found.")
    meta = match.get("metadata") or {}
    return KnowledgeGetResult(
        id=entry_id,
        name=(match.get("source") or "").strip() or entry_id,
        source=match.get("source", ""),
        content=match.get("content", ""),
        metadata={k: str(v) for k, v in meta.items() if v is not None},
    )


async def knowledge_remove(backend: "BackendServer", entry_id: str) -> KnowledgeRemoveResult:
    """Delete one document by id — the FE uses ``removed`` to
    refresh the list optimistically."""
    knowledge = backend._session.knowledge_mgr.knowledge
    if knowledge is None:
        return KnowledgeRemoveResult(removed=False, error="Knowledge base is disabled.")
    try:
        removed = await knowledge.delete_entry(entry_id)
        return KnowledgeRemoveResult(removed=removed)
    except Exception as exc:
        return KnowledgeRemoveResult(removed=False, error=str(exc))
