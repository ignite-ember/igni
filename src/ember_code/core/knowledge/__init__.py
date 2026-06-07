"""Project knowledge — Chroma-backed per-project index with YAML sync."""

from ember_code.core.knowledge.index import KnowledgeIndex
from ember_code.core.knowledge.ingest import Ingester, IngestError
from ember_code.core.knowledge.manager import KnowledgeManager
from ember_code.core.knowledge.models import (
    KnowledgeAddResult,
    KnowledgeSearchResponse,
    KnowledgeSearchResult,
    KnowledgeStatus,
    KnowledgeSyncResult,
)
from ember_code.core.knowledge.sync import KnowledgeSyncer

__all__ = [
    "KnowledgeIndex",
    "KnowledgeManager",
    "KnowledgeSyncer",
    "Ingester",
    "IngestError",
    "KnowledgeAddResult",
    "KnowledgeSearchResponse",
    "KnowledgeSearchResult",
    "KnowledgeStatus",
    "KnowledgeSyncResult",
]
