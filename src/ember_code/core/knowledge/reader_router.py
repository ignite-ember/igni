"""Reader routing for URL / file-path ingestion.

Owns the mapping from URL host (and URL path suffix) to the right Agno
reader, and the mapping from file suffix to reader class. Also owns the
frozenset of "read raw as text" extensions — files whose newline
structure we want to preserve verbatim rather than run through Agno's
``clean_text``.

All Agno reader classes are imported at module top so ingestion never
does a runtime ``__import__`` — one Rule 2 violation removed. Optional
Agno readers are guarded by ``try/except ImportError`` so this module
still imports if Agno drops a reader in a future release.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agno.knowledge.reader.arxiv_reader import ArxivReader
from agno.knowledge.reader.pdf_reader import PDFReader
from agno.knowledge.reader.text_reader import TextReader
from agno.knowledge.reader.website_reader import WebsiteReader
from agno.knowledge.reader.wikipedia_reader import WikipediaReader
from agno.knowledge.reader.youtube_reader import YouTubeReader

# Optional readers — each import guarded so a downstream Agno rename
# doesn't wedge the whole package on load. Missing readers silently
# drop out of ``EXTENSION_READERS``; those suffixes then fall through
# to the generic TextReader.
try:
    from agno.knowledge.reader.docx_reader import DocxReader as _DocxReader
except ImportError:
    _DocxReader = None  # type: ignore[assignment]
try:
    from agno.knowledge.reader.pptx_reader import PPTXReader as _PPTXReader
except ImportError:
    _PPTXReader = None  # type: ignore[assignment]
try:
    from agno.knowledge.reader.excel_reader import ExcelReader as _ExcelReader
except ImportError:
    _ExcelReader = None  # type: ignore[assignment]
try:
    from agno.knowledge.reader.csv_reader import CSVReader as _CSVReader
except ImportError:
    _CSVReader = None  # type: ignore[assignment]
try:
    from agno.knowledge.reader.json_reader import JSONReader as _JSONReader
except ImportError:
    _JSONReader = None  # type: ignore[assignment]

_OPTIONAL_READERS: dict[str, type[Any]] = {
    suffix: cls
    for suffix, cls in (
        (".docx", _DocxReader),
        (".doc", _DocxReader),
        (".pptx", _PPTXReader),
        (".xlsx", _ExcelReader),
        (".xls", _ExcelReader),
        (".csv", _CSVReader),
        (".json", _JSONReader),
    )
    if cls is not None
}


# Extensions we read raw (preserves newlines / markdown structure).
# Anything else falls through to an Agno reader.
_DEFAULT_TEXT_EXTENSIONS: frozenset[str] = frozenset(
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


_DEFAULT_EXTENSION_READERS: dict[str, type[Any]] = {
    ".pdf": PDFReader,
    **_OPTIONAL_READERS,
}


UrlPredicate = Callable[[str, str], bool]


def _host_contains(needle: str) -> UrlPredicate:
    return lambda host, _path: needle in host


def _host_equals(needle: str) -> UrlPredicate:
    return lambda host, _path: host == needle


def _path_ends_with(needle: str) -> UrlPredicate:
    return lambda _host, path: path.endswith(needle)


# Ordered list of ``(predicate, reader_cls)`` tuples. The first matching
# predicate wins — this is the data-driven replacement for the if/elif
# chain that used to live in ``_reader_for_url``. Order matters: host
# checks come before path checks because a PDF hosted on youtube.com
# should still get the YouTube reader.
_DEFAULT_URL_HOST_HANDLERS: list[tuple[UrlPredicate, type[Any]]] = [
    (_host_contains("youtube.com"), YouTubeReader),
    (_host_equals("youtu.be"), YouTubeReader),
    (_host_contains("wikipedia.org"), WikipediaReader),
    (_host_contains("arxiv.org"), ArxivReader),
    (_path_ends_with(".pdf"), PDFReader),
]


class ReaderRouter:
    """Routes URL and file-path inputs to the right Agno reader.

    Instance attributes (all overridable at construction time so tests
    and per-project setups can inject custom readers):

    * ``text_extensions`` — file suffixes that should be read raw as
      text rather than delegated to an Agno reader.
    * ``extension_readers`` — mapping from file suffix to reader
      class. Unknown suffixes fall through to :class:`TextReader`.
    * ``url_host_handlers`` — ordered list of
      ``(predicate, reader_cls)`` tuples. The first matching
      predicate wins; the fallback is :class:`WebsiteReader`.

    The class-level ``TEXT_EXTENSIONS`` / ``EXTENSION_READERS`` /
    ``URL_HOST_HANDLERS`` attributes are the defaults used when no
    per-instance override is supplied.
    """

    TEXT_EXTENSIONS: frozenset[str] = _DEFAULT_TEXT_EXTENSIONS
    EXTENSION_READERS: dict[str, type[Any]] = _DEFAULT_EXTENSION_READERS
    URL_HOST_HANDLERS: list[tuple[UrlPredicate, type[Any]]] = _DEFAULT_URL_HOST_HANDLERS

    def __init__(
        self,
        *,
        text_extensions: frozenset[str] | None = None,
        extension_readers: dict[str, type[Any]] | None = None,
        url_host_handlers: list[tuple[UrlPredicate, type[Any]]] | None = None,
        url_fallback: type[Any] = WebsiteReader,
        path_fallback: type[Any] = TextReader,
    ) -> None:
        self.text_extensions = (
            text_extensions if text_extensions is not None else self.TEXT_EXTENSIONS
        )
        self.extension_readers = (
            dict(extension_readers)
            if extension_readers is not None
            else dict(self.EXTENSION_READERS)
        )
        self.url_host_handlers = (
            list(url_host_handlers)
            if url_host_handlers is not None
            else list(self.URL_HOST_HANDLERS)
        )
        self.url_fallback = url_fallback
        self.path_fallback = path_fallback

    def for_url(self, url: str) -> Any:
        """Pick the right reader for ``url`` (youtube / wiki / arxiv / pdf / web)."""
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path.lower()
        for predicate, reader_cls in self.url_host_handlers:
            if predicate(host, path):
                return reader_cls()
        return self.url_fallback()

    def for_path(self, path: Path) -> Any:
        """Pick the right reader for a file path; unknown suffixes get TextReader."""
        reader_cls = self.extension_readers.get(path.suffix.lower())
        if reader_cls is None:
            return self.path_fallback()
        return reader_cls()

    def is_text_path(self, path: Path) -> bool:
        """True when the file should be read raw as text (preserves newlines)."""
        return path.suffix.lower() in self.text_extensions
