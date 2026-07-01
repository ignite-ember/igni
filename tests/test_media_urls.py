"""Tests for the URL-side path of ``core/utils/media`` —
``extract_media_urls`` and the private ``_classify_extension``
dispatcher.

The path-side (``resolve_file_references`` /
``attach_resolved_files``) is covered in ``test_images.py``.
This file fills the URL side: vision-capable models can take
remote URLs as Image/Audio/Video/File parts, and the helper
that picks them out of the user text is what makes that work.

A URL with a missing trailing slash, a query string, or an
uppercase scheme is still a legitimate media URL; the regex
needs to handle each.
"""

from __future__ import annotations

from agno.media import Audio, File, Image, Video

from ember_code.core.utils.media import _classify_extension, extract_media_urls


class TestClassifyExtension:
    """Private dispatcher. Trivial mapping, but each branch maps
    to a different Agno media class downstream — wrong row =
    wrong API call to the provider."""

    def test_image_extensions(self):
        # Sample the common image formats. The set is wider in
        # the source (heic, heif, avif, …) but pinning these
        # core ones catches the most likely regression.
        for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"]:
            assert _classify_extension(ext) == "image"

    def test_audio_extensions(self):
        for ext in [".mp3", ".wav", ".ogg", ".flac", ".m4a"]:
            assert _classify_extension(ext) == "audio"

    def test_video_extensions(self):
        for ext in [".mp4", ".mov", ".avi", ".webm", ".mkv"]:
            assert _classify_extension(ext) == "video"

    def test_pdf_is_document(self):
        # PDF is the only document type today. Pinning here so
        # a future addition of .docx etc. doesn't silently
        # change the routing of existing PDFs.
        assert _classify_extension(".pdf") == "document"

    def test_unknown_extension_returns_unknown(self):
        # Defensive — the dispatcher must not crash on an
        # extension that's not in the table. Callers branch on
        # the string return value (``if kind == "image"``);
        # an empty / None return would be a silent miss.
        assert _classify_extension(".xyz") == "unknown"
        assert _classify_extension("") == "unknown"

    def test_case_insensitive(self):
        # macOS Finder surfaces ``.PNG`` and ``.JPEG`` from
        # camera roll. The dispatcher lowercases before lookup.
        assert _classify_extension(".PNG") == "image"
        assert _classify_extension(".MP3") == "audio"


class TestExtractMediaUrls:
    """The URL extractor pulls media URLs from a free-form
    user message. Each URL is wrapped in the appropriate Agno
    media class and bucketed by kind."""

    def test_no_urls_returns_none(self):
        # The function returns ``None`` when nothing was found
        # — the call site uses that as "don't pass media kwargs
        # at all" (vs ``{}`` which would also work but is
        # syntactically noisier).
        assert extract_media_urls("just plain text") is None

    def test_empty_string_returns_none(self):
        assert extract_media_urls("") is None

    def test_no_extension_url_ignored(self):
        # ``https://example.com/`` has no media extension; not
        # a media URL.
        assert extract_media_urls("see https://example.com/page") is None

    def test_image_url_extracted(self):
        out = extract_media_urls("look at https://example.com/photo.png")
        assert out is not None
        assert "images" in out
        assert len(out["images"]) == 1
        # The Image object carries the URL — verify it lands
        # on the right field.
        assert isinstance(out["images"][0], Image)
        assert out["images"][0].url == "https://example.com/photo.png"

    def test_audio_url_extracted(self):
        out = extract_media_urls("clip: https://example.com/song.mp3")
        assert out is not None
        assert "audio" in out
        assert isinstance(out["audio"][0], Audio)

    def test_video_url_extracted(self):
        out = extract_media_urls("watch https://example.com/clip.mp4")
        assert out is not None
        assert "videos" in out
        assert isinstance(out["videos"][0], Video)

    def test_pdf_url_extracted(self):
        out = extract_media_urls("paper: https://example.com/draft.pdf")
        assert out is not None
        assert "files" in out
        assert isinstance(out["files"][0], File)

    def test_http_and_https_both_match(self):
        # Both schemes are valid. Don't gate on https-only —
        # there are still legitimate http-hosted assets.
        out = extract_media_urls("http://example.com/a.png")
        assert out is not None and "images" in out

    def test_url_with_query_string(self):
        # Many CDN URLs append ``?w=600&fmt=auto``. The regex
        # explicitly tolerates a query string after the
        # extension so these still match.
        out = extract_media_urls("see https://cdn.example.com/photo.png?w=600&fmt=auto")
        assert out is not None
        assert "images" in out
        # The Image.url should include the query string — strip
        # only happens for the extension-classification step
        # (path_part = url.split("?")[0]).
        assert out["images"][0].url.endswith("?w=600&fmt=auto")

    def test_multiple_urls_in_one_message(self):
        # Most production calls bundle several attachments.
        # The extractor walks the whole text and buckets by
        # kind.
        text = (
            "compare https://example.com/a.png and "
            "https://example.com/b.jpg with audio "
            "https://example.com/c.mp3"
        )
        out = extract_media_urls(text)
        assert out is not None
        assert len(out["images"]) == 2
        assert len(out["audio"]) == 1

    def test_uppercase_extension_in_url_matches(self):
        # The regex is ``re.IGNORECASE``. Some sites preserve
        # uploaded-file casing (``Photo.PNG``).
        out = extract_media_urls("https://example.com/Photo.PNG")
        assert out is not None
        assert "images" in out

    def test_kind_buckets_only_emitted_when_non_empty(self):
        # If only images are in the text, the result dict
        # contains ``images`` but NOT ``audio``/``videos``/
        # ``files``. The caller iterates the dict and forwards
        # each key as a kwarg — empty lists would still be
        # forwarded and could surface as the provider rejecting
        # an ``images=[]`` call.
        out = extract_media_urls("photo https://example.com/x.png")
        assert out is not None
        assert set(out.keys()) == {"images"}

    def test_unknown_extension_in_url_not_picked_up(self):
        # ``https://example.com/data.xyz`` is not media. The
        # regex itself is built from the known-extension set,
        # so an unknown extension doesn't even match.
        out = extract_media_urls("see https://example.com/data.xyz")
        assert out is None
