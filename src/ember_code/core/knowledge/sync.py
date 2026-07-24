"""Bidirectional sync between ``.ember/knowledge.yaml`` and the knowledge index.

The YAML file is the git-shareable source of truth. The Chroma index
is the runtime vector store. On startup we add any file entries
missing from the index; on shutdown we export any new index entries
back to the file. Each entry has a stable content-hash id so diffing
is cheap.

The domain models (:class:`KnowledgeEntry`, :class:`KnowledgeYamlFile`,
:class:`EntryProvenance`) live in :mod:`.models`; this module owns
only the orchestration.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ember_code.core.knowledge.index import KnowledgeIndex
from ember_code.core.knowledge.metadata_codec import KnowledgeMetadataCodec
from ember_code.core.knowledge.models import (
    EntryProvenance,
    KnowledgeEntry,
    KnowledgeSyncResult,
    KnowledgeYamlFile,
)

# Re-export ``KnowledgeEntry`` so ``ember_code.core.knowledge.sync.KnowledgeEntry``
# still resolves for any deep-import consumer that predates the move
# into ``models.py``.
__all__ = ["KnowledgeSyncer", "KnowledgeEntry", "DEFAULT_KNOWLEDGE_FILE"]

logger = logging.getLogger(__name__)

DEFAULT_KNOWLEDGE_FILE = ".ember/knowledge.yaml"


class KnowledgeSyncer:
    """Orchestrates YAML ↔ :class:`KnowledgeIndex` bidirectional sync.

    Delegates file I/O to :class:`KnowledgeYamlFile` and entry-id +
    provenance-timestamp policy to :class:`KnowledgeMetadataCodec`,
    so every free function that used to live at module scope now
    belongs to the class that owns its invariant.
    """

    def __init__(
        self,
        file_path: Path,
        knowledge: KnowledgeIndex | None = None,
        *,
        codec: KnowledgeMetadataCodec | None = None,
    ) -> None:
        self.file_path = file_path
        self.knowledge = knowledge
        self.codec = codec or KnowledgeMetadataCodec()

    def make_entry(self, content: str, source: str = "") -> KnowledgeEntry:
        return KnowledgeEntry.from_content(content, source=source, codec=self.codec)

    def load_file(self) -> list[KnowledgeEntry]:
        return KnowledgeYamlFile.load_from(self.file_path)

    def save_file(self, entries: list[KnowledgeEntry]) -> None:
        KnowledgeYamlFile.write_to(self.file_path, entries, codec=self.codec)

    async def sync_file_to_db(self) -> KnowledgeSyncResult:
        """File → Chroma. Idempotent — only adds missing entries."""
        if self.knowledge is None:
            return KnowledgeSyncResult(direction="file_to_db", message="Knowledge disabled.")

        file_entries = self.load_file()
        if not file_entries:
            return KnowledgeSyncResult(
                direction="file_to_db", new_entries=0, existing_entries=0, total_entries=0
            )

        inserted = 0
        existing = 0
        errors: list[str] = []
        for entry in file_entries:
            entry_id = entry.id or self.codec.content_hash(entry.content)
            try:
                if await self.knowledge.has_entry(entry_id):
                    existing += 1
                    continue
                await self.knowledge.add(
                    content=entry.content,
                    name=entry_id,
                    source=entry.source,
                    metadata=EntryProvenance(added_at=entry.added_at).to_metadata(),
                    entry_id=entry_id,
                )
                inserted += 1
            except Exception as exc:
                message = f"Failed to insert entry {entry_id}: {exc}"
                logger.warning(message)
                errors.append(message)

        return KnowledgeSyncResult(
            direction="file_to_db",
            new_entries=inserted,
            existing_entries=existing,
            total_entries=existing + inserted,
            errors=errors,
        )

    async def sync_db_to_file(self) -> KnowledgeSyncResult:
        """Chroma → file. Appends any new entries that aren't already in the YAML."""
        if self.knowledge is None:
            return KnowledgeSyncResult(direction="db_to_file", message="Knowledge disabled.")

        file_entries = self.load_file()
        file_ids = {e.id for e in file_entries}

        db_entries = await self.knowledge.list_entries()
        new_from_db: list[KnowledgeEntry] = []
        for db_entry in db_entries:
            entry_id = db_entry.id
            if not entry_id or entry_id in file_ids:
                continue
            provenance = EntryProvenance.from_metadata(db_entry.metadata)
            new_from_db.append(
                KnowledgeEntry(
                    id=entry_id,
                    content=db_entry.content,
                    source=db_entry.source,
                    added_at=provenance.added_at or self.codec.now_iso(),
                )
            )

        if not new_from_db:
            return KnowledgeSyncResult(
                direction="db_to_file",
                new_entries=0,
                existing_entries=len(file_entries),
                total_entries=len(file_entries),
            )

        merged = file_entries + new_from_db
        self.save_file(merged)
        return KnowledgeSyncResult(
            direction="db_to_file",
            new_entries=len(new_from_db),
            existing_entries=len(file_entries),
            total_entries=len(merged),
        )
