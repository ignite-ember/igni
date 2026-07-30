"""URL/path ingestion for the knowledge index.

Detects the input kind (URL vs file path), delegates the reader
selection to :class:`ReaderRouter`, then stores the result as **one**
:class:`KnowledgeIndex` document with N chunks.

Reader dispatch (host / suffix routing, extension lists, the reader
class registry) lives in ``reader_router.py``. This module owns the
orchestration only: file/directory walking, raw-text short-circuit,
:class:`IngestResult` bookkeeping, and the handoff to the knowledge
index.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ember_code.core.knowledge.index import KnowledgeIndex
from ember_code.core.knowledge.models import (
    IngestedContent,
    IngestMetadata,
    IngestResult,
)
from ember_code.core.knowledge.reader_router import ReaderRouter

logger = logging.getLogger(__name__)


class Ingester:
    """Routes URL / file path inputs to the right reader, then stores results.

    Constructor takes an optional :class:`ReaderRouter` — defaulted for
    backwards compatibility, injectable for tests and per-project
    reader overrides. Public methods return :class:`IngestResult` and
    never raise on expected failures (missing paths, reader errors,
    decode errors, storage errors); unexpected failures propagate.
    """

    def __init__(
        self,
        knowledge: KnowledgeIndex,
        *,
        router: ReaderRouter | None = None,
    ) -> None:
        self.knowledge = knowledge
        self.router = router if router is not None else ReaderRouter()

    async def add_url(
        self,
        url: str,
        *,
        metadata: dict[str, str] | IngestMetadata | None = None,
    ) -> IngestResult:
        reader = self.router.for_url(url)
        try:
            documents = await reader.async_read(url)
        except Exception as exc:  # noqa: BLE001 — Agno readers raise heterogeneous errors
            return IngestResult.fail(f"Failed to fetch {url}: {exc}")
        return await self._store(documents, source=url, metadata=IngestMetadata.coerce(metadata))

    async def add_path(
        self,
        path: str | Path,
        *,
        metadata: dict[str, str] | IngestMetadata | None = None,
    ) -> IngestResult:
        p = Path(str(path)).expanduser()
        if not p.exists():
            return IngestResult.fail(f"Path does not exist: {p}")

        meta = IngestMetadata.coerce(metadata)

        if p.is_dir():
            return await self._add_directory(p, metadata=meta)

        # Plain-text formats (markdown / source / notes): Agno readers
        # call clean_text() which collapses every newline into a space,
        # destroying the structure the detail page renders. Read raw and
        # let KnowledgeIndex.add() do the chunking with our newline-
        # preserving chunker.
        if self.router.is_text_path(p):
            return await self._add_text_file(p, metadata=meta)

        reader = self.router.for_path(p)
        try:
            documents = await reader.async_read(p)
        except Exception as exc:  # noqa: BLE001 — Agno readers raise heterogeneous errors
            return IngestResult.fail(f"Failed to read {p}: {exc}")
        return await self._store(documents, source=str(p), metadata=meta)

    async def _add_text_file(self, path: Path, *, metadata: IngestMetadata) -> IngestResult:
        try:
            # Offload the file read so the BE event loop keeps
            # dispatching other sessions' RPCs while a large note is
            # loaded into memory.
            content = await asyncio.to_thread(path.read_text, encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            return IngestResult.fail(f"Failed to read {path}: {exc}")
        if not content.strip():
            return IngestResult.ok(0)
        try:
            await self.knowledge.add(
                content=content,
                source=str(path),
                metadata=metadata.merged_with(),
            )
        except Exception as exc:  # noqa: BLE001 — storage boundary; keep session alive
            logger.exception("Failed to store text document from %s", path)
            return IngestResult.fail(f"Failed to store {path}: {exc}")
        return IngestResult.ok(1)

    async def _add_directory(self, directory: Path, *, metadata: IngestMetadata) -> IngestResult:
        """Walk a directory and ingest every readable file inside it.

        Hidden files and ``__pycache__`` directories are skipped — they
        rarely contain useful knowledge and tend to balloon counts.

        Per-child failures are logged at debug level (preserving the
        pre-refactor observable behaviour) and skipped, so a single
        unreadable file doesn't abort the whole directory ingest.
        """
        total = 0
        for child in sorted(directory.rglob("*")):
            if not child.is_file():
                continue
            if any(part.startswith(".") or part == "__pycache__" for part in child.parts):
                continue
            child_result = await self.add_path(child, metadata=metadata)
            if child_result.error:
                logger.debug("Skipping %s: %s", child, child_result.error)
                continue
            total += child_result.count
        return IngestResult.ok(total)

    async def _store(
        self,
        documents: list[Any],
        *,
        source: str,
        metadata: IngestMetadata,
    ) -> IngestResult:
        ingested = IngestedContent.from_agno_documents(documents)
        if ingested.is_empty:
            return IngestResult.ok(0)

        doc_meta = metadata.merged_with(ingested.source_metadata)

        try:
            result = await self.knowledge.add_document(
                chunks=ingested.chunks,
                source=source,
                metadata=doc_meta,
            )
        except Exception as exc:  # noqa: BLE001 — storage boundary; keep session alive
            logger.exception("Failed to store ingested document from %s", source)
            return IngestResult.fail(f"Failed to store {source}: {exc}")
        if not result.success:
            logger.warning("Skipped ingested document from %s: %s", source, result.error)
            return IngestResult.fail(result.error or f"Storage rejected {source}")
        return IngestResult.ok(1)
