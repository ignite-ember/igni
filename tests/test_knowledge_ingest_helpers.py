"""Tests for the reader-routing + metadata-coercion primitives.

  * :class:`ReaderRouter.is_text_path` — extension-based text
    detection. Drives the choice between reading raw bytes
    (preserves newlines / markdown structure) vs delegating to an
    Agno reader.
  * :meth:`IngestedContent._coerce_meta` — coerces arbitrary
    metadata to ``dict[str, str]`` so chroma's metadata-store
    doesn't choke.
  * :meth:`ReaderRouter.for_url` — host-routing decision (YouTube /
    Wikipedia / arxiv / PDF / generic website). Swapping any of
    these mappings silently degrades ingestion quality (e.g. a
    YouTube URL falling through to WebsiteReader picks up the HTML
    page chrome instead of the transcript).

The full :class:`Ingester` is left to integration tests — too much
Agno setup to be useful here.
"""

from __future__ import annotations

from pathlib import Path

from ember_code.core.knowledge.models import IngestedContent
from ember_code.core.knowledge.reader_router import ReaderRouter


class TestIsTextPath:
    def setup_method(self) -> None:
        self.router = ReaderRouter()

    def test_markdown_is_text(self):
        # ``.md`` is the most common case — without this, every
        # ingested README would go through Agno's TextReader's
        # tokeniser instead of being kept structurally intact.
        assert self.router.is_text_path(Path("README.md")) is True

    def test_python_source_is_text(self):
        assert self.router.is_text_path(Path("src/foo.py")) is True

    def test_shell_scripts_are_text(self):
        assert self.router.is_text_path(Path("install.sh")) is True
        assert self.router.is_text_path(Path("init.bash")) is True

    def test_config_files_are_text(self):
        # The knowledge base sometimes ingests config files for
        # context. Pin the common ones.
        assert self.router.is_text_path(Path("config.yaml")) is True
        assert self.router.is_text_path(Path("config.toml")) is True
        assert self.router.is_text_path(Path("settings.ini")) is True

    def test_case_insensitive(self):
        # Filesystems on macOS / Windows surface .MD or .Py.
        # The lookup lowercases, so casing must not matter.
        assert self.router.is_text_path(Path("README.MD")) is True
        assert self.router.is_text_path(Path("Module.PY")) is True

    def test_binary_extensions_are_not_text(self):
        # PDFs, images, archives — must go through an Agno
        # reader, not be slurped as text.
        assert self.router.is_text_path(Path("doc.pdf")) is False
        assert self.router.is_text_path(Path("img.png")) is False
        assert self.router.is_text_path(Path("archive.zip")) is False

    def test_no_extension_is_not_text(self):
        # ``LICENSE`` / ``README`` etc. — without a suffix the
        # type is ambiguous. The lookup returns False; the caller
        # decides what to do (typically falls through to the
        # generic TextReader).
        assert self.router.is_text_path(Path("LICENSE")) is False
        assert self.router.is_text_path(Path("Dockerfile")) is False


class _FakeDoc:
    """Stand-in for Agno's ``Document`` — only ``.content`` and
    ``.meta_data`` are consumed by :class:`IngestedContent`."""

    def __init__(self, content: object = None, meta_data: object = None):
        self.content = content
        self.meta_data = meta_data


class TestIngestedContentCoerceMeta:
    """``IngestedContent._coerce_meta`` defends chroma's metadata API
    which requires ``dict[str, str]``. Anything else must be filtered
    out or coerced — chroma raises on int/bool/None values."""

    def _coerce(self, meta: object) -> dict[str, str]:
        # Exercise the coercion via the same public surface Ingester
        # uses: build a single-doc batch, read source_metadata off it.
        return IngestedContent.from_agno_documents([_FakeDoc("x", meta)]).source_metadata

    def test_non_dict_returns_empty(self):
        # Defensive — meta may come from arbitrary upstream places.
        # Don't blow up on a list / None / string.
        assert self._coerce(None) == {}
        assert self._coerce("garbage") == {}
        assert self._coerce([1, 2]) == {}
        assert self._coerce(42) == {}

    def test_empty_dict_returns_empty_dict(self):
        # Identity case — empty in, empty out.
        assert self._coerce({}) == {}

    def test_coerces_all_values_to_str(self):
        # Numbers, bools, etc. → str. Without this, chroma rejects
        # the metadata at write time.
        assert self._coerce({"count": 42, "enabled": True, "ratio": 0.5}) == {
            "count": "42",
            "enabled": "True",
            "ratio": "0.5",
        }

    def test_coerces_keys_to_str(self):
        # Defensive — dict keys could legitimately be ints (rare but
        # possible with namedtuple-as-dict patterns).
        assert self._coerce({1: "one", 2: "two"}) == {"1": "one", "2": "two"}

    def test_filters_none_values(self):
        # ``None`` is the common sentinel for "no value here". We
        # don't want to emit ``{"author": "None"}`` — drop the key
        # entirely so the metadata reflects only set fields.
        out = self._coerce({"title": "doc", "author": None, "year": 2026})
        assert out == {"title": "doc", "year": "2026"}
        assert "author" not in out

    def test_preserves_other_falsy_values(self):
        # Empty string and 0 are valid metadata. Only ``None`` is
        # filtered.
        assert self._coerce({"tag": "", "count": 0, "ok": False}) == {
            "tag": "",
            "count": "0",
            "ok": "False",
        }


class TestIngestedContentFromAgnoDocuments:
    """``IngestedContent.from_agno_documents`` collects the
    ``.content`` strings and pulls source metadata off the first
    document — that's the wire format the previous helpers combined
    into by hand."""

    def test_filters_empty_content(self):
        docs = [_FakeDoc("first"), _FakeDoc(""), _FakeDoc(None), _FakeDoc("second")]
        ingested = IngestedContent.from_agno_documents(docs)
        assert ingested.chunks == ["first", "second"]

    def test_is_empty_when_no_content(self):
        ingested = IngestedContent.from_agno_documents([_FakeDoc(""), _FakeDoc(None)])
        assert ingested.is_empty is True

    def test_reads_metadata_off_first_document(self):
        docs = [_FakeDoc("a", {"source_url": "x"}), _FakeDoc("b", {"other": "y"})]
        ingested = IngestedContent.from_agno_documents(docs)
        # Only the first doc's metadata is picked up — the source
        # is a single URL / path, subsequent chunks share it.
        assert ingested.source_metadata == {"source_url": "x"}


class TestReaderForUrl:
    """``ReaderRouter.for_url`` picks the right Agno reader based on
    the URL host / path. Wrong mapping silently degrades ingest
    quality — a YouTube URL falling through to WebsiteReader grabs
    the HTML chrome instead of the transcript."""

    def setup_method(self) -> None:
        self.router = ReaderRouter()

    def _import_class(self, dotted: str):
        # Import a reader class by its ``module.Class`` spec. Returns
        # the class object (not an instance) so we can assert with
        # isinstance.
        module_name, _, class_name = dotted.partition(".")
        module = __import__(f"agno.knowledge.reader.{module_name}", fromlist=[class_name])
        return getattr(module, class_name)

    def test_youtube_dot_com_routes_to_youtube_reader(self):
        cls = self._import_class("youtube_reader.YouTubeReader")
        reader = self.router.for_url("https://www.youtube.com/watch?v=abc123")
        assert isinstance(reader, cls)

    def test_youtu_dot_be_short_url_routes_to_youtube_reader(self):
        # The short URL form. If the host check uses substring match
        # too loosely, we'd accept ``notyoutu.beware.com`` — the
        # source uses an exact host match for this one which is what
        # we want.
        cls = self._import_class("youtube_reader.YouTubeReader")
        reader = self.router.for_url("https://youtu.be/abc123")
        assert isinstance(reader, cls)

    def test_wikipedia_routes_to_wikipedia_reader(self):
        cls = self._import_class("wikipedia_reader.WikipediaReader")
        reader = self.router.for_url("https://en.wikipedia.org/wiki/Article")
        assert isinstance(reader, cls)

    def test_arxiv_routes_to_arxiv_reader(self):
        cls = self._import_class("arxiv_reader.ArxivReader")
        reader = self.router.for_url("https://arxiv.org/abs/2301.00001")
        assert isinstance(reader, cls)

    def test_pdf_path_routes_to_pdf_reader_even_for_unknown_host(self):
        # The PDF check is on the URL path, not the host. A PDF
        # hosted on a random domain should still go to the PDF
        # reader.
        cls = self._import_class("pdf_reader.PDFReader")
        reader = self.router.for_url("https://example.com/papers/draft.pdf")
        assert isinstance(reader, cls)

    def test_generic_url_falls_through_to_website_reader(self):
        # Anything that doesn't match the special-cases falls
        # through. The website reader walks the page DOM — decent
        # default for generic web content.
        cls = self._import_class("website_reader.WebsiteReader")
        reader = self.router.for_url("https://example.com/article")
        assert isinstance(reader, cls)

    def test_youtube_check_is_case_insensitive_on_host(self):
        # Some URLs come with uppercase hosts. The source lowercases
        # the host before matching.
        cls = self._import_class("youtube_reader.YouTubeReader")
        reader = self.router.for_url("https://WWW.YOUTUBE.COM/watch?v=x")
        assert isinstance(reader, cls)

    def test_pdf_check_is_case_insensitive_on_path(self):
        # ``Paper.PDF`` should still route to PDF.
        cls = self._import_class("pdf_reader.PDFReader")
        reader = self.router.for_url("https://example.com/Paper.PDF")
        assert isinstance(reader, cls)
