"""Rendering an artifact, and what leaves when it is shared.

The export projection is a privacy boundary, so the tests that matter
most here are about what is **absent**. The load-bearing one is the
dangling-reference rule: an edge pointing at an artifact outside the
shared set is dropped entirely, because rendering it as "built on
<name we didn't share>" leaks the name and the existence of private
work to whoever opens the page.
"""

from __future__ import annotations

import pytest

from ember_code.core.artifacts.export import (
    ArtifactPublisher,
    ExportBundle,
    _slug,
)
from ember_code.core.artifacts.models import (
    ArtifactLineage,
    ArtifactSummary,
    ArtifactVersion,
    Citation,
    EdgeState,
    LineageEdge,
)
from ember_code.core.artifacts.render import (
    ArtifactRenderer,
    ExportScope,
    RenderRequest,
)


def summary(**kw) -> ArtifactSummary:
    base = {"artifact_id": "a1", "name": "Auth Design", "kind": "design", "head_n": 2}
    return ArtifactSummary(**{**base, **kw})


def version(**kw) -> ArtifactVersion:
    base = {
        "artifact_id": "a1",
        "version_id": "a1::2",
        "n": 2,
        "content": "# Heading\n\nBody text.",
        "head": True,
        "created_at": "2026-09-25T10:30:12+00:00",
    }
    return ArtifactVersion(**{**base, **kw})


def edge(**kw) -> LineageEdge:
    base = {
        "from_artifact_id": "a1",
        "from_n": 2,
        "to_artifact_id": "s1",
        "to_name": "Schema",
        "to_n": 1,
        "state": EdgeState.CONFIRMED,
        "pinned_n": 1,
        "source_head_n": 1,
    }
    return LineageEdge(**{**base, **kw})


def render(request: RenderRequest) -> str:
    return ArtifactRenderer().render(request)


class TestThePageIsSelfContained:
    def test_nothing_is_fetched_at_view_time(self):
        """A page that fetches anything phones home from the
        recipient's machine, which is exactly what sharing must not
        do."""
        html = render(RenderRequest(summary=summary(), version=version()))
        assert "<script src=" not in html
        assert 'rel="stylesheet"' not in html
        assert "https://" not in html.split("</style>")[0]

    def test_the_stylesheet_is_inline(self):
        html = render(RenderRequest(summary=summary(), version=version()))
        assert "<style>" in html and "prefers-color-scheme" in html

    def test_the_title_is_the_artifact_name(self):
        html = render(RenderRequest(summary=summary(), version=version()))
        assert "<title>Auth Design</title>" in html


class TestContentRendering:
    def test_headings_and_paragraphs_become_html(self):
        html = render(RenderRequest(summary=summary(), version=version()))
        assert "<h2>Heading</h2>" in html
        assert "<p>Body text.</p>" in html

    def test_fenced_code_is_preserved(self):
        v = version(content="```\nx = 1\n```")
        html = render(RenderRequest(summary=summary(), version=v))
        assert "<pre><code>" in html and "x = 1" in html

    def test_bullets_become_a_list(self):
        v = version(content="- one\n- two")
        html = render(RenderRequest(summary=summary(), version=v))
        assert html.count("<li>") == 2

    def test_html_in_content_is_escaped_not_executed(self):
        v = version(content="<script>alert(1)</script>")
        html = render(RenderRequest(summary=summary(), version=v))
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_a_name_with_markup_is_escaped_in_the_title(self):
        html = render(RenderRequest(summary=summary(name="<b>x</b>"), version=version()))
        assert "<title>&lt;b&gt;x&lt;/b&gt;</title>" in html


class TestTheStaleBanner:
    def test_a_moved_source_raises_a_banner(self):
        lineage = ArtifactLineage(
            artifact_id="a1", n=2, sources=[edge(pinned_n=1, source_head_n=4)]
        )
        html = render(RenderRequest(summary=summary(), version=version(), lineage=lineage))
        assert "Sources have changed" in html
        assert "moved from v1 to v4" in html

    def test_an_up_to_date_source_raises_nothing(self):
        lineage = ArtifactLineage(artifact_id="a1", n=2, sources=[edge()])
        html = render(RenderRequest(summary=summary(), version=version(), lineage=lineage))
        assert "Sources have changed" not in html

    def test_a_superseded_version_shows_no_banner(self):
        """Old versions being out of date is a fact about history,
        not something the reader can act on."""
        lineage = ArtifactLineage(
            artifact_id="a1", n=1, sources=[edge(pinned_n=1, source_head_n=4)]
        )
        html = render(
            RenderRequest(
                summary=summary(),
                version=version(n=1, head=False),
                lineage=lineage,
            )
        )
        assert "Sources have changed" not in html


class TestVersionHistory:
    def test_a_single_version_renders_no_switcher(self):
        html = render(RenderRequest(summary=summary(), version=version(), history=[version()]))
        assert "Versions" not in html

    def test_multiple_versions_are_listed(self):
        history = [version(n=2), version(n=1, head=False, version_id="a1::1")]
        html = render(RenderRequest(summary=summary(), version=version(), history=history))
        assert "Versions" in html and "v1" in html and "v2" in html


class TestTheExportProjection:
    def _lineage(self) -> ArtifactLineage:
        return ArtifactLineage(
            artifact_id="a1",
            n=2,
            sources=[
                edge(to_artifact_id="shared", to_name="Shared Source"),
                edge(to_artifact_id="private", to_name="Private Notes"),
                edge(
                    to_artifact_id="guess",
                    to_name="A Guess",
                    state=EdgeState.CANDIDATE,
                ),
            ],
            citations=[Citation(url="https://x.test/spec")],
        )

    def test_a_local_view_shows_everything(self):
        html = render(
            RenderRequest(
                summary=summary(),
                version=version(),
                lineage=self._lineage(),
                scope=ExportScope.local(),
            )
        )
        assert "Shared Source" in html
        assert "Private Notes" in html
        assert "A Guess" in html

    def test_an_edge_outside_the_shared_set_is_dropped_entirely(self):
        html = render(
            RenderRequest(
                summary=summary(),
                version=version(),
                lineage=self._lineage(),
                scope=ExportScope.shared({"a1", "shared"}),
            )
        )
        assert "Shared Source" in html
        assert "Private Notes" not in html

    def test_candidates_never_leave(self):
        """Shipping an unreviewed capture asserts a relationship
        nobody confirmed."""
        html = render(
            RenderRequest(
                summary=summary(),
                version=version(),
                lineage=self._lineage(),
                scope=ExportScope.shared({"a1", "shared", "guess"}),
            )
        )
        assert "A Guess" not in html
        assert "Captured" not in html

    def test_citations_survive_an_export(self):
        html = render(
            RenderRequest(
                summary=summary(),
                version=version(),
                lineage=self._lineage(),
                scope=ExportScope.shared({"a1"}),
            )
        )
        assert "https://x.test/spec" in html

    def test_the_stale_banner_respects_scope(self):
        lineage = ArtifactLineage(
            artifact_id="a1",
            n=2,
            sources=[edge(to_artifact_id="private", pinned_n=1, source_head_n=9)],
        )
        html = render(
            RenderRequest(
                summary=summary(),
                version=version(),
                lineage=lineage,
                scope=ExportScope.shared({"a1"}),
            )
        )
        assert "Sources have changed" not in html


class TestTheSharePayload:
    def _bundle(self) -> ExportBundle:
        return ExportBundle(
            artifact_id="a1",
            title="Auth Design",
            kind="design",
            n=2,
            html="<html></html>",
            exported_at="2026-09-25T10:30:12+00:00",
            author="dz",
        )

    def test_the_payload_carries_only_declared_fields(self):
        """Hand-built rather than model-dumped: the field set is the
        privacy contract, and a serializer that picks up new fields
        automatically is one that leaks them."""
        payload = self._bundle().to_payload(visibility="org", audience=[])
        assert set(payload) == {
            "artifact_id",
            "title",
            "kind",
            "version",
            "html",
            "exported_at",
            "author",
            "visibility",
            "audience",
        }

    def test_no_project_hash_or_session_id_is_present(self):
        payload = self._bundle().to_payload(visibility="private", audience=[])
        assert "project_hash" not in payload
        assert "session_id" not in payload

    def test_visibility_and_audience_are_carried_through(self):
        payload = self._bundle().to_payload(visibility="users", audience=["a@b.test"])
        assert payload["visibility"] == "users"
        assert payload["audience"] == ["a@b.test"]


class TestFilenameSlugs:
    def test_a_title_becomes_a_safe_filename(self):
        assert _slug("Auth Design v2") == "auth-design-v2"

    def test_punctuation_collapses_rather_than_repeating(self):
        assert "--" not in _slug("a // b .. c")

    def test_an_unusable_title_yields_an_empty_slug(self):
        assert _slug("???") == ""


class TestPublishing:
    """The seam between the client and the igni server.

    Sharing is the one artifact operation that leaves the machine, so
    the two things pinned here are that it refuses to run without an
    account and that what it sends is the projected bundle rather than
    anything richer.
    """

    def _bundle(self) -> ExportBundle:
        return ExportBundle(
            artifact_id="a1",
            title="Auth Design",
            kind="design",
            n=2,
            html="<html></html>",
            exported_at="2026-09-25T10:30:12+00:00",
        )

    def test_it_is_not_configured_when_logged_out(self):
        """A local-only user is a supported configuration, not an
        error path — callers check rather than catching, so there is no
        stack trace per share attempt."""
        publisher = ArtifactPublisher(server_url="https://igni.test", access_token=None)
        assert publisher.is_configured is False

    def test_it_is_not_configured_without_a_server(self):
        publisher = ArtifactPublisher(server_url="", access_token="t")
        assert publisher.is_configured is False

    async def test_publishing_logged_out_raises_with_the_fix(self):
        publisher = ArtifactPublisher(server_url="https://igni.test", access_token=None)
        with pytest.raises(RuntimeError, match="igni login"):
            await publisher.publish(self._bundle())

    async def test_it_posts_the_projected_payload(self):
        sent: dict = {}

        class _Response:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"url": "https://igni.test/a/abc123"}

        class _Client:
            async def post(self, url, *, json, headers):
                sent["url"] = url
                sent["json"] = json
                sent["headers"] = headers
                return _Response()

            async def aclose(self) -> None:
                return None

        publisher = ArtifactPublisher(
            server_url="https://igni.test/", access_token="tok", client=_Client()
        )
        url = await publisher.publish(self._bundle(), visibility="users", audience=["a@b.test"])

        assert url == "https://igni.test/a/abc123"
        assert sent["url"] == "https://igni.test/v1/artifacts/shared"
        assert sent["headers"]["Authorization"] == "Bearer tok"
        assert sent["json"]["visibility"] == "users"
        assert sent["json"]["audience"] == ["a@b.test"]
        assert "project_hash" not in sent["json"]

    async def test_a_server_that_returns_no_url_is_an_error(self):
        """A 200 with no URL means the caller has nothing to hand
        anyone, which is a failure however the server spells it."""

        class _Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {}

        class _Client:
            async def post(self, url, *, json, headers):
                return _Response()

            async def aclose(self) -> None:
                return None

        publisher = ArtifactPublisher(
            server_url="https://igni.test", access_token="tok", client=_Client()
        )
        with pytest.raises(RuntimeError, match="no URL"):
            await publisher.publish(self._bundle())
