"""Two-way sync: what lands locally, and what a refused push does.

The claim this file defends is that **the whole history ends up
local**. Not a cache of what a screen currently needs — the versions,
who wrote them, and the comments on each one, in the graph, answerable
with the network down. A sync that merely fetched for display would
pass a shallower test and fail the only requirement that matters.

The other half is the conflict. A push carries the version it was
written from; if that is no longer the server's head the push is
refused, and the refusal carries what is missing. Nothing merges on
anyone's behalf, because two people editing the same paragraph is a
decision and a document nobody wrote is the worst way to settle it.

The server is stubbed with ``httpx.MockTransport``, so the real
request building, status handling and JSON parsing all run — only the
socket is replaced.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from ember_code.core.artifacts.collaboration import (
    ChainDiverged,
    CollaborationSync,
    NotAllowed,
)
from ember_code.core.artifacts.index import ArtifactIndex
from ember_code.core.artifacts.metadata_codec import ArtifactMetadataCodec
from ember_code.core.artifacts.models import CommentStatus, SourceDeclaration
from ember_code.core.code_index.embedder import HashEmbedder
from evals.archivist.store import FakeArtifactStore

CODEC = ArtifactMetadataCodec()
SERVER = "https://igni.example"
TOKEN = "test-token"


def comment_payload(**overrides) -> dict:
    base = {
        "id": "c1",
        "artifact_id": CODEC.artifact_id("Auth model"),
        "version": 1,
        "thread_id": "c1",
        "parent_id": None,
        "quote": "session cookie",
        "body": "This contradicts the JWT decision.",
        "suggested_text": None,
        "status": "open",
        "resolved_in_n": None,
        "author_email": "colleague@acme.test",
        "created_at": "2026-09-26T10:00:00Z",
        "updated_at": "2026-09-26T10:00:00Z",
    }
    return {**base, **overrides}


def version_payload(n: int, content: str, **overrides) -> dict:
    base = {
        "n": n,
        "base_n": n - 1,
        "content": content,
        "sources": {},
        "author_email": "colleague@acme.test",
        "created_at": "2026-09-26T10:00:00Z",
    }
    return {**base, **overrides}


class Server:
    """A scripted server. Records what it was asked, so a test can
    assert on the request as well as the response."""

    def __init__(self) -> None:
        self.versions: list[dict] = []
        self.comments: list[dict] = []
        self.head_n = 0
        self.conflict: dict | None = None
        self.requests: list[httpx.Request] = []
        self.status_overrides: dict[str, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        override = self.status_overrides.get(path)
        if override:
            return httpx.Response(override, json={"detail": "Not found"})

        if path.endswith("/versions") and request.method == "GET":
            return httpx.Response(200, json={"head_n": self.head_n, "versions": self.versions})
        if path.endswith("/versions") and request.method == "POST":
            if self.conflict is not None:
                return httpx.Response(409, json=self.conflict)
            return httpx.Response(200, json={"n": self.head_n + 1, "head_n": self.head_n + 1})
        if path == "/v1/artifacts/comments/sync":
            return httpx.Response(
                200,
                json={
                    "comments": self.comments,
                    "cursor": self.comments[-1]["updated_at"] if self.comments else None,
                },
            )
        if "/artifacts/comments/" in path and request.method == "PATCH":
            body = json.loads(request.content or b"{}")
            row = dict(self.comments[0])
            row.update({"status": body["status"], "resolved_in_n": body.get("resolved_in_n")})
            return httpx.Response(200, json=row)
        return httpx.Response(404, json={"detail": "Not found"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture
async def index(tmp_path: Path) -> ArtifactIndex:
    index = ArtifactIndex(
        project=tmp_path,
        neo4j_client=FakeArtifactStore(),  # type: ignore[arg-type]
        embedder=HashEmbedder(),
    )
    await index.create(
        name="Auth model",
        content="Auth is a session cookie.",
        sources=SourceDeclaration(),
        kind="design",
    )
    index.ledger.clear()
    return index


@pytest.fixture
def server() -> Server:
    return Server()


@pytest.fixture
def sync(index, server) -> CollaborationSync:
    return CollaborationSync(index, server_url=SERVER, access_token=TOKEN, client=server.client())


class TestPullingTheChain:
    async def test_a_remote_version_becomes_a_local_version(self, sync, index, server):
        server.head_n = 2
        server.versions = [version_payload(2, "Auth is a signed JWT.")]

        result = await sync.pull(slug="s1", ref="Auth model")

        assert result.versions_applied == 1
        head = await index.version("Auth model", 0)
        assert head is not None
        assert head.n == 2
        assert "signed JWT" in head.content

    async def test_the_superseded_version_is_still_readable(self, sync, index, server):
        """The point of mirroring the chain rather than the head."""
        server.head_n = 2
        server.versions = [version_payload(2, "Auth is a signed JWT.")]
        await sync.pull(slug="s1", ref="Auth model")

        old = await index.version("Auth model", 1)
        assert old is not None
        assert "session cookie" in old.content

    async def test_the_remote_author_is_kept(self, sync, index, server):
        """Attributing pulled work to whoever ran the sync would throw
        away most of the reason to mirror it."""
        server.head_n = 2
        server.versions = [version_payload(2, "x", author_email="ada@acme.test")]
        await sync.pull(slug="s1", ref="Auth model")

        head = await index.version("Auth model", 0)
        assert head is not None
        assert head.author == "ada@acme.test"

    async def test_it_only_asks_for_what_it_lacks(self, sync, server):
        server.head_n = 1
        await sync.pull(slug="s1", ref="Auth model")
        pull = next(r for r in server.requests if r.url.path.endswith("/versions"))
        assert pull.url.params["after"] == "1"

    async def test_a_pulled_version_is_searchable(self, sync, index, server):
        """A projected version that skipped the write path would be in
        the graph and absent from every answer."""
        server.head_n = 2
        server.versions = [version_payload(2, "Tokens expire after fifteen minutes.")]
        await sync.pull(slug="s1", ref="Auth model")

        hits = await index.search(query="fifteen minutes", limit=5, record=False)
        assert any("fifteen minutes" in h.content for h in hits)

    async def test_a_forbidden_document_raises(self, sync, server):
        server.status_overrides["/v1/artifacts/shared/s1/versions"] = 404
        with pytest.raises(NotAllowed):
            await sync.pull(slug="s1", ref="Auth model")


class TestProjectingComments:
    async def test_a_comment_lands_in_the_graph(self, sync, index, server):
        server.comments = [comment_payload()]
        result = await sync.pull(slug="s1", ref="Auth model")

        assert result.comments_projected == 1
        rows = await index.client.comments()
        assert [c.body for c in rows] == ["This contradicts the JWT decision."]

    async def test_it_is_anchored_to_the_version_it_was_made_on(self, sync, index, server):
        server.comments = [comment_payload(version=1)]
        await sync.pull(slug="s1", ref="Auth model")
        assert [c.n for c in await index.client.comments()] == [1]

    async def test_the_quote_survives_as_the_anchor(self, sync, index, server):
        server.comments = [comment_payload()]
        await sync.pull(slug="s1", ref="Auth model")
        assert (await index.client.comments())[0].quote == "session cookie"

    async def test_a_comment_on_an_unpulled_version_is_skipped_not_lost(self, sync, index, server):
        """It arrives again on the next pull, because the cursor is the
        server's and does not advance past rows this side could not
        place."""
        server.comments = [comment_payload(version=99)]
        result = await sync.pull(slug="s1", ref="Auth model")
        assert result.comments_projected == 0
        assert await index.client.comments() == []

    async def test_projecting_twice_does_not_duplicate(self, sync, index, server):
        server.comments = [comment_payload()]
        await sync.pull(slug="s1", ref="Auth model")
        await sync.pull(slug="s1", ref="Auth model")
        assert len(await index.client.comments()) == 1

    async def test_a_status_change_overwrites_the_local_copy(self, sync, index, server):
        """The server is the authority. A local copy that refused to
        move would show a resolved thread as open forever."""
        server.comments = [comment_payload()]
        await sync.pull(slug="s1", ref="Auth model")
        server.comments = [comment_payload(status="resolved", updated_at="2026-09-26T11:00:00Z")]
        await sync.pull(slug="s1", ref="Auth model")

        rows = await index.client.comments()
        assert len(rows) == 1
        assert rows[0].status is CommentStatus.RESOLVED

    async def test_the_cursor_comes_from_the_server(self, sync, server):
        server.comments = [comment_payload(updated_at="2026-09-26T12:00:00Z")]
        result = await sync.pull(slug="s1", ref="Auth model")
        assert result.cursor == "2026-09-26T12:00:00Z"

    async def test_the_cursor_is_sent_back_on_the_next_pull(self, sync, server):
        server.comments = [comment_payload(updated_at="2026-09-26T12:00:00Z")]
        await sync.pull(slug="s1", ref="Auth model", cursor="2026-09-26T09:00:00Z")
        sync_request = next(
            r for r in server.requests if r.url.path == "/v1/artifacts/comments/sync"
        )
        assert sync_request.url.params["since"] == "2026-09-26T09:00:00Z"

    async def test_an_unknown_status_becomes_open_rather_than_vanishing(self, sync, index, server):
        server.comments = [comment_payload(status="escalated-to-legal")]
        await sync.pull(slug="s1", ref="Auth model")
        assert (await index.client.comments())[0].status is CommentStatus.OPEN

    async def test_the_open_count_is_the_review_queue(self, sync, index, server):
        server.comments = [
            comment_payload(id="c1"),
            comment_payload(id="c2", status="resolved", updated_at="2026-09-26T10:01:00Z"),
        ]
        await sync.pull(slug="s1", ref="Auth model")
        assert await index.client.open_comment_count() == 1


class TestPushing:
    async def test_it_sends_the_local_head_and_its_base(self, sync, server):
        await sync.push(slug="s1", ref="Auth model", base_n=1)
        post = next(
            r for r in server.requests if r.method == "POST" and r.url.path.endswith("/versions")
        )
        body = json.loads(post.content)
        assert body["base_n"] == 1
        assert "session cookie" in body["content"]

    async def test_a_diverged_chain_raises_with_what_is_missing(self, sync, server):
        server.conflict = {
            "head_n": 3,
            "missing": [version_payload(2, "theirs two"), version_payload(3, "theirs three")],
        }
        with pytest.raises(ChainDiverged) as caught:
            await sync.push(slug="s1", ref="Auth model", base_n=1)

        assert caught.value.head_n == 3
        assert [v.n for v in caught.value.missing] == [2, 3]
        assert [v.content for v in caught.value.missing] == ["theirs two", "theirs three"]

    async def test_a_refused_push_changes_nothing_locally(self, sync, index, server):
        server.conflict = {"head_n": 2, "missing": [version_payload(2, "theirs")]}
        with pytest.raises(ChainDiverged):
            await sync.push(slug="s1", ref="Auth model", base_n=1)

        head = await index.version("Auth model", 0)
        assert head is not None and head.n == 1

    async def test_only_confirmed_edges_are_sent(self, sync, index, server):
        """A candidate is this machine's note that something was read.
        Sending it plants an unadjudicated edge in a colleague's graph
        that they never saw the evidence for."""
        await index.create(
            name="Rate limits", content="Per-IP at the edge.", sources=SourceDeclaration()
        )
        index.ledger.clear()
        # Reading it makes it a candidate on the next write.
        await index.version("Rate limits", 0, record=True)
        await index.revise(ref="Auth model", content="Auth is a JWT.", sources=SourceDeclaration())

        await sync.push(slug="s1", ref="Auth model", base_n=1)
        post = next(
            r for r in server.requests if r.method == "POST" and r.url.path.endswith("/versions")
        )
        sent = json.loads(post.content)["sources"]["pins"]
        assert all(pin["state"] == "confirmed" for pin in sent)

    async def test_pushing_a_document_you_may_not_write_raises(self, sync, server):
        server.status_overrides["/v1/artifacts/shared/s1/versions"] = 404
        with pytest.raises(NotAllowed):
            await sync.push(slug="s1", ref="Auth model", base_n=1)


class TestResolvingFromTheLocalScreen:
    async def test_it_writes_through_and_projects_the_result(self, sync, index, server):
        server.comments = [comment_payload()]
        await sync.pull(slug="s1", ref="Auth model")

        await sync.resolve(comment_id="c1", status=CommentStatus.ACCEPTED, resolved_in_n=2)

        patch = next(r for r in server.requests if r.method == "PATCH")
        assert json.loads(patch.content) == {"status": "accepted", "resolved_in_n": 2}
        rows = await index.client.comments()
        assert rows[0].status is CommentStatus.ACCEPTED
        assert rows[0].resolved_in_n == 2

    async def test_accepting_records_which_version_applied_it(self, sync, index, server):
        """Otherwise the thread closes and the link between "somebody
        asked for this" and "here is where it happened" is gone."""
        server.comments = [comment_payload()]
        await sync.pull(slug="s1", ref="Auth model")
        result = await sync.resolve(comment_id="c1", status=CommentStatus.ACCEPTED, resolved_in_n=7)
        assert result is not None and result.resolved_in_n == 7


class TestConfiguration:
    async def test_an_unconfigured_sync_says_so(self, index):
        sync = CollaborationSync(index, server_url="", access_token="")
        assert not sync.is_configured
        with pytest.raises(RuntimeError, match="igni login"):
            await sync.pull(slug="s1", ref="Auth model")
