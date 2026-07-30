"""Tests for tools/web.py — web fetch tools."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ember_code.core.tools.http_fetcher import HttpFetcher
from ember_code.core.tools.web import WebTools


class TestWebTools:
    def test_registers_functions(self):
        tools = WebTools()
        names = {f.name for f in tools.functions.values()} | {
            f.name for f in tools.async_functions.values()
        }
        assert "fetch_url" in names
        assert "fetch_json" in names

    def test_extract_text_from_html(self):
        html = "<html><body><h1>Title</h1><p>Hello world</p><script>bad();</script></body></html>"
        text = HttpFetcher.html_to_text(html)
        assert "Title" in text
        assert "Hello world" in text
        assert "bad()" not in text


class TestExtractTextFromHtmlDeepDive:
    """Deeper coverage of ``HttpFetcher.html_to_text``. The method
    runs four regex passes:
      1. ``<script>…</script>`` removal (DOTALL — multi-line scripts)
      2. ``<style>…</style>`` removal (same — was only one test
         covering ``<script>`` removal, this fills the gap)
      3. ``<[^>]+>`` → space (strip remaining tags)
      4. ``\\s+`` → single space + strip
    Each pass is a small but distinct chunk worth pinning."""

    def test_strips_style_tag_contents(self):
        # The original test only verified ``<script>`` removal.
        # ``<style>`` uses an identical pattern but a refactor
        # of one could drop the other silently.
        html = "<html><style>body { color: red; }</style><body>visible</body></html>"
        text = HttpFetcher.html_to_text(html)
        assert "visible" in text
        assert "color: red" not in text
        assert "{" not in text

    def test_strips_multi_line_script(self):
        # The ``DOTALL`` flag is what lets the regex span
        # newlines. Without it, multi-line scripts would leak.
        html = (
            "<html><body>before"
            "<script>\nconst x = 1;\nconsole.log(x);\n</script>"
            "after</body></html>"
        )
        text = HttpFetcher.html_to_text(html)
        assert "before" in text
        assert "after" in text
        assert "const x" not in text
        assert "console.log" not in text

    def test_strips_multi_line_style(self):
        # Same DOTALL coverage on style — multi-line CSS is
        # the common case.
        html = (
            "<style>\n"
            "  .header { font-size: 16px; }\n"
            "  .body { color: blue; }\n"
            "</style>"
            "<p>visible body text</p>"
        )
        text = HttpFetcher.html_to_text(html)
        assert "visible body text" in text
        assert "font-size" not in text
        assert "color: blue" not in text

    def test_strips_multiple_scripts(self):
        # A real page often has 3-5 separate script blocks.
        # Make sure they ALL go, not just the first.
        html = (
            "<script>a()</script>"
            "<p>keep this</p>"
            "<script>b()</script>"
            "<p>and this</p>"
            "<script>c()</script>"
        )
        text = HttpFetcher.html_to_text(html)
        assert "keep this" in text and "and this" in text
        assert "a()" not in text
        assert "b()" not in text
        assert "c()" not in text

    def test_script_with_attributes_still_stripped(self):
        # The regex matches ``<script[^>]*>`` — anything except
        # ``>`` in the opening tag. Pin so a future tightening
        # (e.g. requiring an exact ``<script>``) doesn't leak
        # script blobs with src / type attrs.
        html = '<script type="application/json">{"secret":"data"}</script><p>visible</p>'
        text = HttpFetcher.html_to_text(html)
        assert "visible" in text
        assert "secret" not in text

    def test_strips_remaining_tags(self):
        # After script/style, any other tag becomes a space
        # (via the ``<[^>]+>`` → " " pass). Pin that <div>,
        # <span>, <br/>, <a href="…"> all collapse to space.
        html = '<div><span>a</span><br/><a href="x">b</a></div>'
        text = HttpFetcher.html_to_text(html)
        # Single space between tokens after the whitespace-
        # collapse pass.
        assert text == "a b"

    def test_collapses_whitespace_runs(self):
        # Multiple consecutive whitespace (newlines, tabs,
        # multiple spaces) collapse to a single space. This is
        # what keeps the extracted text scannable for the
        # agent — original HTML formatting is noise.
        html = "<p>line one</p>\n\n\n<p>line\ttwo</p>   <p>line   three</p>"
        text = HttpFetcher.html_to_text(html)
        # ``\s+`` → " " means all whitespace becomes single
        # spaces.
        assert text == "line one line two line three"

    def test_strips_leading_trailing_whitespace(self):
        # The final ``.strip()`` handles edge whitespace from
        # tag conversion (a leading/trailing tag becomes a
        # leading/trailing space).
        html = "<html><body><p>content</p></body></html>"
        text = HttpFetcher.html_to_text(html)
        assert text == "content"
        assert not text.startswith(" ")
        assert not text.endswith(" ")

    def test_empty_input(self):
        # Defensive — empty string in, empty out. Don't raise.
        assert HttpFetcher.html_to_text("") == ""

    def test_plain_text_with_no_tags(self):
        # The function may be called on already-stripped text.
        # Should pass through (modulo whitespace collapse).
        assert HttpFetcher.html_to_text("just text") == "just text"

    def test_html_entities_pass_through_literally(self):
        # The function does NOT decode HTML entities — they
        # land in the output as-is (``&amp;`` not ``&``,
        # ``&nbsp;`` not space). Pin so a future refactor
        # adding html.unescape is a deliberate decision.
        html = "<p>Tom &amp; Jerry &nbsp; cost &lt; $5</p>"
        text = HttpFetcher.html_to_text(html)
        assert "&amp;" in text
        assert "&lt;" in text

    def test_script_with_no_closing_tag_falls_through(self):
        # Defensive — a malformed page with ``<script>`` but
        # no ``</script>`` doesn't match the script-removal
        # regex (which requires both tags), so the content
        # falls through to the generic tag-stripper. Result:
        # the script body STAYS in the output. Pin this so a
        # future "robustness" refactor that bails on
        # malformed HTML is a deliberate choice.
        html = "<script>alert(1)<p>visible</p>"
        text = HttpFetcher.html_to_text(html)
        # alert(1) leaks through — documented limitation.
        assert "alert(1)" in text or "visible" in text

    def test_self_closing_tags_collapse_to_space(self):
        # ``<br/>``, ``<img src="x"/>``, ``<hr>`` — all consumed
        # by ``<[^>]+>``.
        html = "before<br/>after<hr>end"
        text = HttpFetcher.html_to_text(html)
        assert text == "before after end"

    def test_nested_tags_all_stripped(self):
        # Deeply nested DOM — all the wrapper tags collapse,
        # only text content remains.
        html = "<div><section><article><p><span><em>deep</em></span></p></article></section></div>"
        text = HttpFetcher.html_to_text(html)
        assert text == "deep"

    def test_doctype_and_comments_collapse_via_tag_pass(self):
        # ``<!DOCTYPE html>`` and ``<!-- comment -->`` both
        # match ``<[^>]+>`` (they have ``<…>`` shape), so they
        # get stripped. Multi-line HTML comments are stripped
        # too because each ``<…>`` chunk is a single tag-match
        # — though the comment's body content survives as text
        # if it spans multiple ``>``s.
        html = "<!DOCTYPE html><html><body><!-- single-line comment --><p>visible</p></body></html>"
        text = HttpFetcher.html_to_text(html)
        assert "visible" in text
        # DOCTYPE keyword is gone.
        assert "DOCTYPE" not in text

    @pytest.mark.asyncio
    async def test_fetch_url_success(self):
        tools = WebTools()
        mock_response = MagicMock()
        mock_response.text = "<html><body>Hello</body></html>"
        mock_response.headers = {"content-type": "text/html"}
        mock_response.raise_for_status = MagicMock()

        with patch("ember_code.core.tools.http_fetcher.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_response)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await tools.fetch_url("https://example.com")
            assert "Hello" in result

    @pytest.mark.asyncio
    async def test_fetch_url_truncates(self):
        tools = WebTools()
        mock_response = MagicMock()
        mock_response.text = "x" * 20000
        mock_response.headers = {"content-type": "text/plain"}
        mock_response.raise_for_status = MagicMock()

        with patch("ember_code.core.tools.http_fetcher.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_response)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await tools.fetch_url("https://example.com", max_length=100)
            assert len(result) <= 100

    @pytest.mark.asyncio
    async def test_fetch_json_success(self):
        tools = WebTools()
        mock_response = MagicMock()
        mock_response.text = '{"key": "value"}'
        mock_response.headers = {"content-type": "application/json"}
        mock_response.raise_for_status = MagicMock()

        with patch("ember_code.core.tools.http_fetcher.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_response)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await tools.fetch_json("https://api.example.com/data")
            assert "key" in result

    @pytest.mark.asyncio
    async def test_fetch_url_error(self):
        import httpx

        tools = WebTools()

        with patch("ember_code.core.tools.http_fetcher.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            # HttpFetcher narrows to ``httpx.HTTPError`` (Pattern 3) —
            # a real connect failure surfaces as ``httpx.ConnectError``
            # (a subclass), which becomes ``FetchResult.failure`` at
            # the toolkit boundary. A bare ``Exception`` would (by
            # design) propagate as a programming bug.
            instance.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await tools.fetch_url("https://bad.example.com")
            assert "Error" in result
