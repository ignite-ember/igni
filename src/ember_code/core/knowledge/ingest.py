"""URL/path ingestion for the knowledge index.

Detects the input kind (URL → website / YouTube / Wikipedia / ArXiv,
file → PDF / DOCX / PPTX / XLSX / CSV / JSON / Markdown / text), uses
the matching Agno reader to fetch and chunk, then stores the result as
**one** :class:`KnowledgeIndex` document with N chunks.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agno.knowledge.reader.arxiv_reader import ArxivReader
from agno.knowledge.reader.pdf_reader import PDFReader
from agno.knowledge.reader.text_reader import TextReader
from agno.knowledge.reader.website_reader import WebsiteReader
from agno.knowledge.reader.wikipedia_reader import WikipediaReader
from agno.knowledge.reader.youtube_reader import YouTubeReader

from ember_code.core.knowledge.index import KnowledgeIndex

logger = logging.getLogger(__name__)


class IngestError(Exception):
    """Raised when ingestion fails for a reason worth surfacing to the user."""


class Ingester:
    """Routes URL / file path inputs to the right reader, then stores results."""

    def __init__(self, knowledge: KnowledgeIndex):
        self.knowledge = knowledge

    async def add_url(self, url: str, *, metadata: dict[str, str] | None = None) -> int:
        reader = _reader_for_url(url)
        try:
            documents = await reader.async_read(url)
        except Exception as exc:
            raise IngestError(f"Failed to fetch {url}: {exc}") from exc
        return await self._store(documents, source=url, metadata=metadata)

    async def add_path(self, path: str | Path, *, metadata: dict[str, str] | None = None) -> int:
        p = Path(str(path)).expanduser()
        if not p.exists():
            raise IngestError(f"Path does not exist: {p}")

        if p.is_dir():
            return await self._add_directory(p, metadata=metadata)

        # Plain-text formats (markdown / source / notes): Agno readers
        # call clean_text() which collapses every newline into a space,
        # destroying the structure the detail page renders. Read raw and
        # let KnowledgeIndex.add() do the chunking with our newline-
        # preserving chunker.
        if _is_text_path(p):
            try:
                # Offload the file read so the BE event loop keeps
                # dispatching other sessions' RPCs while a large note
                # is loaded into memory.
                content = await asyncio.to_thread(p.read_text, encoding="utf-8")
            except (UnicodeDecodeError, OSError) as exc:
                raise IngestError(f"Failed to read {p}: {exc}") from exc
            if not content.strip():
                return 0
            try:
                await self.knowledge.add(
                    content=content,
                    source=str(p),
                    metadata=dict(metadata or {}),
                )
                return 1
            except Exception:
                logger.exception("Failed to store text document from %s", p)
                return 0

        reader = _reader_for_path(p)
        try:
            documents = await reader.async_read(p)
        except Exception as exc:
            raise IngestError(f"Failed to read {p}: {exc}") from exc
        return await self._store(documents, source=str(p), metadata=metadata)

    async def _add_directory(self, directory: Path, *, metadata: dict[str, str] | None) -> int:
        """Walk a directory and ingest every readable file inside it.

        Hidden files and ``__pycache__`` directories are skipped — they
        rarely contain useful knowledge and tend to balloon counts.
        """
        total = 0
        for child in sorted(directory.rglob("*")):
            if not child.is_file():
                continue
            if any(part.startswith(".") or part == "__pycache__" for part in child.parts):
                continue
            try:
                total += await self.add_path(child, metadata=metadata)
            except IngestError as exc:
                logger.debug("Skipping %s: %s", child, exc)
        return total

    async def _store(
        self,
        documents: list[Any],
        *,
        source: str,
        metadata: dict[str, str] | None,
    ) -> int:
        chunks = [c for c in (getattr(d, "content", None) for d in documents) if c]
        if not chunks:
            return 0

        doc_meta = dict(metadata or {})
        if documents:
            doc_meta.update(_string_meta(getattr(documents[0], "meta_data", None)))

        try:
            await self.knowledge.add_document(
                chunks=chunks,
                source=source,
                metadata=doc_meta,
            )
            return 1
        except Exception:
            logger.exception("Failed to store ingested document from %s", source)
            return 0


# -- Reader dispatch ----------------------------------------------------------


def _reader_for_url(url: str):
    host = (urlparse(url).hostname or "").lower()
    path = urlparse(url).path.lower()

    if "youtube.com" in host or host == "youtu.be":
        return YouTubeReader()
    if "wikipedia.org" in host:
        return WikipediaReader()
    if "arxiv.org" in host:
        return ArxivReader()
    if path.endswith(".pdf"):
        return PDFReader()
    return WebsiteReader()


# Extensions we read raw (preserves newlines / markdown structure).
# Anything else falls through to an Agno reader below.
_TEXT_EXTENSIONS = frozenset(
    {
        ".md",
        ".markdown",
        ".mdx",
        ".txt",
        ".text",
        ".rst",
        ".log",
        ".py",
        ".pyi",
        ".ipynb",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".rb",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".swift",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".sh",
        ".bash",
        ".zsh",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".html",
        ".htm",
        ".xml",
        ".css",
        ".scss",
        ".sql",
    }
)


def _is_text_path(path: Path) -> bool:
    return path.suffix.lower() in _TEXT_EXTENSIONS


_PATH_READERS: dict[str, str] = {
    ".pdf": "pdf_reader.PDFReader",
    ".docx": "docx_reader.DocxReader",
    ".doc": "docx_reader.DocxReader",
    ".pptx": "pptx_reader.PPTXReader",
    ".xlsx": "excel_reader.ExcelReader",
    ".xls": "excel_reader.ExcelReader",
    ".csv": "csv_reader.CSVReader",
    ".json": "json_reader.JSONReader",
}


def _reader_for_path(path: Path):
    spec = _PATH_READERS.get(path.suffix.lower())
    if spec is None:
        return TextReader()

    module_name, _, class_name = spec.partition(".")
    module = __import__(f"agno.knowledge.reader.{module_name}", fromlist=[class_name])
    return getattr(module, class_name)()


def _string_meta(meta: Any) -> dict[str, str]:
    if not isinstance(meta, dict):
        return {}
    return {str(k): str(v) for k, v in meta.items() if v is not None}
