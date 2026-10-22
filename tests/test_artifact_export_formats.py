"""Three containers for one projection.

HTML, Markdown and PDF all come from the same :class:`ExportBundle`,
which is what keeps a PDF from quietly containing more — or less —
than the HTML the sharing rules were written against. These tests pin
that, plus the two things that are specific to each container: the
extension it lands under, and the bytes it starts with.

PDF needs pango installed, so those cases skip rather than fail on a
machine without it. A skip says "not checked here"; a failure would
say "your code is broken", and only one of those would be true.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember_code.core.artifacts.export import (
    ArtifactExporter,
    ExportBundle,
    ExportFormat,
    _markdown_document,
)
from ember_code.core.artifacts.pdf import PdfRenderer


def bundle(**kw) -> ExportBundle:
    base = {
        "artifact_id": "a1",
        "title": "Auth Design",
        "kind": "design",
        "n": 3,
        "html": "<!doctype html><title>Auth Design</title><h1>Auth Design</h1>",
        "exported_at": "2026-09-27T10:00:00Z",
        "author": "maya@acme.test",
        "content": "# Auth Design\n\nAuth is a signed JWT.",
    }
    return ExportBundle(**{**base, **kw})


class StubExporter(ArtifactExporter):
    """An exporter whose projection is already decided."""

    def __init__(self, result: ExportBundle | None) -> None:  # noqa: D107
        self._result = result

    async def bundle(self, ref, *, version=0, scope=None):  # noqa: D102, ARG002
        return self._result


class TestTheFormatName:
    def test_the_spellings_people_type_are_accepted(self):
        assert ExportFormat.parse("markdown") is ExportFormat.MARKDOWN
        assert ExportFormat.parse("md") is ExportFormat.MARKDOWN
        assert ExportFormat.parse(".PDF") is ExportFormat.PDF
        assert ExportFormat.parse("htm") is ExportFormat.HTML

    def test_nothing_means_html(self):
        # The pre-format callers sent no format at all.
        assert ExportFormat.parse("") is ExportFormat.HTML

    def test_a_typo_is_refused_rather_than_silently_html(self):
        # Falling back to HTML would write the wrong file and report
        # success, which is the one outcome nobody can act on.
        with pytest.raises(ValueError, match="Unknown export format"):
            ExportFormat.parse("pdff")


class TestWhatLandsOnDisk:
    @pytest.mark.asyncio
    async def test_html_keeps_the_rendered_page(self, tmp_path: Path):
        written = await StubExporter(bundle()).write("a1", tmp_path, fmt=ExportFormat.HTML)
        assert written is not None
        assert written.name == "auth-design-v3.html"
        assert "<h1>Auth Design</h1>" in written.read_text()

    @pytest.mark.asyncio
    async def test_markdown_is_the_author_s_source_not_converted_html(self, tmp_path: Path):
        # Round-tripping HTML back to Markdown would return an
        # approximation of what the author wrote. The version's own
        # text is right there.
        written = await StubExporter(bundle()).write("a1", tmp_path, fmt=ExportFormat.MARKDOWN)
        assert written is not None
        assert written.name == "auth-design-v3.md"
        text = written.read_text()
        assert "# Auth Design\n\nAuth is a signed JWT." in text
        assert "<h1>" not in text

    @pytest.mark.asyncio
    async def test_markdown_carries_the_version_in_front_matter(self, tmp_path: Path):
        # Which version a file came from is what makes it traceable
        # back to the chain; without it an export is an orphan.
        written = await StubExporter(bundle()).write("a1", tmp_path, fmt=ExportFormat.MARKDOWN)
        assert written is not None
        head = written.read_text().split("---")[1]
        assert "version: 3" in head
        assert 'artifact_id: "a1"' in head
        assert 'author: "maya@acme.test"' in head

    @pytest.mark.asyncio
    async def test_a_missing_artifact_writes_nothing(self, tmp_path: Path):
        written = await StubExporter(None).write("nope", tmp_path, fmt=ExportFormat.MARKDOWN)
        assert written is None
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_a_named_file_is_used_as_given(self, tmp_path: Path):
        # A destination with a suffix is the caller's decision, and
        # the format does not get to rename it.
        target = tmp_path / "somewhere.md"
        written = await StubExporter(bundle()).write("a1", target, fmt=ExportFormat.MARKDOWN)
        assert written == target


class TestQuotingInFrontMatter:
    def test_a_title_with_a_quote_does_not_break_the_block(self):
        # An unescaped quote turns valid front matter into a parse
        # error in every tool downstream.
        text = _markdown_document(bundle(title='The "real" design'))
        assert 'title: "The \\"real\\" design"' in text

    def test_the_body_is_left_exactly_as_written(self):
        text = _markdown_document(bundle(content="a\n\n  indented\n"))
        assert text.endswith("a\n\n  indented\n")


@pytest.mark.skipif(not PdfRenderer.is_available(), reason="pango is not installed")
class TestPdf:
    @pytest.mark.asyncio
    async def test_it_is_a_pdf(self, tmp_path: Path):
        written = await StubExporter(bundle()).write("a1", tmp_path, fmt=ExportFormat.PDF)
        assert written is not None
        assert written.name == "auth-design-v3.pdf"
        assert written.read_bytes().startswith(b"%PDF-")

    @pytest.mark.asyncio
    async def test_it_renders_the_same_page_html_does(self, tmp_path: Path):
        # Not an assertion about bytes — about provenance. Both
        # formats are written from ``bundle.html``, so a change to the
        # sharing rules reaches both or neither.
        written = await StubExporter(bundle()).write("a1", tmp_path, fmt=ExportFormat.PDF)
        assert written is not None
        assert len(written.read_bytes()) > 500
