"""Bidirectional sync between ``.ember/knowledge.yaml`` and the knowledge index.

The YAML file is the git-shareable source of truth. The Chroma index
is the runtime vector store. On startup we add any file entries
missing from the index; on shutdown we export any new index entries
back to the file. Each entry has a stable content-hash id so diffing
is cheap.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ember_code.core.knowledge.index import KnowledgeIndex
from ember_code.core.knowledge.models import KnowledgeSyncResult

logger = logging.getLogger(__name__)

DEFAULT_KNOWLEDGE_FILE = ".ember/knowledge.yaml"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class KnowledgeSyncer:
    def __init__(self, file_path: Path, knowledge: KnowledgeIndex | None = None) -> None:
        self.file_path = file_path
        self.knowledge = knowledge

    @staticmethod
    def make_entry(content: str, source: str = "") -> dict[str, Any]:
        return {
            "id": hashlib.sha256(content.encode()).hexdigest()[:16],
            "content": content,
            "source": source,
            "added_at": _now_iso(),
        }

    def load_file(self) -> list[dict[str, Any]]:
        if not self.file_path.exists():
            return []
        try:
            with open(self.file_path) as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict):
                return []
            entries = data.get("entries", [])
            return entries if isinstance(entries, list) else []
        except Exception:
            logger.warning("Failed to load knowledge file: %s", self.file_path)
            return []

    def save_file(self, entries: list[dict[str, Any]]) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": 1, "synced_at": _now_iso(), "entries": entries}
        with open(self.file_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

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
        for entry in file_entries:
            entry_id = entry.get("id") or hashlib.sha256(entry["content"].encode()).hexdigest()[:16]
            try:
                if await self.knowledge.has_entry(entry_id):
                    existing += 1
                    continue
                await self.knowledge.add(
                    content=entry["content"],
                    name=entry_id,
                    source=entry.get("source", ""),
                    metadata={"added_at": entry.get("added_at", "")},
                    entry_id=entry_id,
                )
                inserted += 1
            except Exception as exc:
                logger.warning("Failed to insert entry %s: %s", entry_id, exc)

        return KnowledgeSyncResult(
            direction="file_to_db",
            new_entries=inserted,
            existing_entries=existing,
            total_entries=existing + inserted,
        )

    async def sync_db_to_file(self) -> KnowledgeSyncResult:
        """Chroma → file. Appends any new entries that aren't already in the YAML."""
        if self.knowledge is None:
            return KnowledgeSyncResult(direction="db_to_file", message="Knowledge disabled.")

        file_entries = self.load_file()
        file_ids = {e["id"] for e in file_entries if "id" in e}

        db_entries = await self.knowledge.list_entries()
        new_from_db = [e for e in db_entries if e.get("id") and e["id"] not in file_ids]

        if not new_from_db:
            return KnowledgeSyncResult(
                direction="db_to_file",
                new_entries=0,
                existing_entries=len(file_entries),
                total_entries=len(file_entries),
            )

        now = _now_iso()
        merged = file_entries + [
            {
                "id": e["id"],
                "content": e["content"],
                "source": e.get("source", ""),
                "added_at": e.get("metadata", {}).get("added_at") or now,
            }
            for e in new_from_db
        ]
        self.save_file(merged)
        return KnowledgeSyncResult(
            direction="db_to_file",
            new_entries=len(new_from_db),
            existing_entries=len(file_entries),
            total_entries=len(merged),
        )
