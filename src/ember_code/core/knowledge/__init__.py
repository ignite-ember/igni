"""Project knowledge — Chroma-backed per-project index with YAML sync."""

from ember_code.core.knowledge.index import KnowledgeIndex
from ember_code.core.knowledge.ingest import Ingester
from ember_code.core.knowledge.manager import KnowledgeManager
from ember_code.core.knowledge.models import (
    EntryProvenance,
    IngestedContent,
    IngestMetadata,
    IngestResult,
    KnowledgeAddResult,
    KnowledgeDeleteResult,
    KnowledgeEntry,
    KnowledgeIndexEntry,
    KnowledgeSearchResponse,
    KnowledgeSearchResult,
    KnowledgeStatus,
    KnowledgeSyncResult,
    KnowledgeYamlFile,
)
from ember_code.core.knowledge.reader_router import ReaderRouter
from ember_code.core.knowledge.sync import KnowledgeSyncer

__all__ = [
    "KnowledgeIndex",
    "KnowledgeManager",
    "KnowledgeSyncer",
    "Ingester",
    "IngestResult",
    "IngestMetadata",
    "IngestedContent",
    "ReaderRouter",
    "EntryProvenance",
    "KnowledgeAddResult",
    "KnowledgeDeleteResult",
    "KnowledgeEntry",
    "KnowledgeIndexEntry",
    "KnowledgeSearchResponse",
    "KnowledgeSearchResult",
    "KnowledgeStatus",
    "KnowledgeSyncResult",
    "KnowledgeYamlFile",
]
