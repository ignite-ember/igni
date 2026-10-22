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

import asyncio
import contextlib
import json
from pathlib import Path

import httpx
import pytest

from ember_code.core.artifacts.collaboration import (
    ChainDiverged,
    CollaborationSync,
    NotAllowed,
    WaitingStream,
)
from ember_code.core.artifacts.index import ArtifactIndex
from ember_code.core.artifacts.metadata_codec import ArtifactMetadataCodec
from ember_code.core.artifacts.models import CommentStatus, SourceDeclaration
from ember_code.core.code_index.embedder import HashEmbedder
from evals.archivist.store import FakeArtifactStore, FakeSharedServer

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
        #: The one draft this document has, once somebody writes it.
        self.draft: dict | None = None

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
        if path.endswith("/draft") and request.method == "GET":
            if self.draft is None:
                return httpx.Response(
                    200, json={"exists": False, "content": "", "base_n": 0, "head_n": self.head_n}
                )
            return httpx.Response(200, json={**self.draft, "head_n": self.head_n})
        if path.endswith("/draft") and request.method == "PUT":
            body = json.loads(request.content or b"{}")
            self.draft = {
                "exists": True,
                "content": body["content"],
                "base_n": body["base_n"],
                "updated_by": "sam@acme.test",
            }
            return httpx.Response(204)
        if path.endswith("/draft/publish"):
            if self.conflict is not None:
                return httpx.Response(409, json=self.conflict)
            if self.draft is None:
                return httpx.Response(409, json={"detail": "There is nothing to publish."})
            self.head_n += 1
            self.versions.append(version_payload(self.head_n, self.draft["content"]))
            self.draft = None
            return httpx.Response(200, json={"n": self.head_n})
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
    index.attach_shared(FakeSharedServer())
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

    async def test_a_pulled_version_goes_through_the_write_path(self, sync, index, server):
        """A projection that inserted behind the write path would leave
        a version with no passages — invisible to drift and to every
        lineage question that reasons about paragraphs.

        This used to assert it through ``index.search``, which no
        longer measures it: search is the server's now, and a pulled
        version is by definition one the server already has. Asserting
        it that way would have been asserting the fake server.
        """
        server.head_n = 2
        server.versions = [version_payload(2, "Tokens expire after fifteen minutes.")]
        await sync.pull(slug="s1", ref="Auth model")

        summary = await index.client.find_by_name("Auth model")
        assert summary is not None
        passages = await index.client.passages(artifact_id=summary.artifact_id, n=2)
        assert any("fifteen minutes" in p.text for p in passages), passages

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


class TestTheLiveChannel:
    """The nudge, and what it makes the client do.

    The channel carries every kind of thing that concerns somebody
    now, not only comments — hence ``WaitingStream`` — but what a
    nudge *means* is unchanged: read again. Comments are pulled into
    the graph because they are part of a document's history; the
    notifications themselves are read from the server, where they
    are the only copy.
    """

    """The stream only decides *how soon*, never *whether*.

    Every assertion here is about latency or about not falling over.
    The correctness property — that missing a notification costs
    nothing — is defended by the cursor tests above, because it is a
    property of the pull rather than of the stream.
    """

    @staticmethod
    def sse(*lines: str) -> bytes:
        return ("\n".join(lines) + "\n\n").encode()

    def streaming_server(self, frames: bytes, comments: list[dict] | None = None):
        """A server whose stream emits ``frames`` then ends."""
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path.endswith("/notifications/stream"):
                return httpx.Response(200, content=frames)
            if request.url.path == "/v1/artifacts/comments/sync":
                rows = comments or []
                return httpx.Response(
                    200,
                    json={
                        "comments": rows,
                        "cursor": rows[-1]["updated_at"] if rows else None,
                    },
                )
            return httpx.Response(404, json={"detail": "Not found"})

        return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen

    async def test_a_sync_event_triggers_a_pull(self, index):
        client, seen = self.streaming_server(
            self.sse("id: 2026-09-26T12:00:00+00:00", "event: sync", 'data: {"cursor": ""}'),
            comments=[comment_payload()],
        )
        sync = CollaborationSync(index, server_url=SERVER, access_token=TOKEN, client=client)
        await WaitingStream(sync).follow_once()

        assert any(r.url.path == "/v1/artifacts/comments/sync" for r in seen)
        assert [c.body for c in await index.client.comments()] == [
            "This contradicts the JWT decision."
        ]

    async def test_a_keepalive_triggers_nothing(self, index):
        """An idle stream must not cost a request per heartbeat."""

        client, seen = self.streaming_server(b": keepalive\n\n: keepalive\n\n")
        sync = CollaborationSync(index, server_url=SERVER, access_token=TOKEN, client=client)
        await WaitingStream(sync).follow_once()

        assert not any(r.url.path == "/v1/artifacts/comments/sync" for r in seen)

    async def test_the_cursor_is_remembered_for_the_reconnect(self, index):

        client, _ = self.streaming_server(
            self.sse("id: 2026-09-26T12:00:00+00:00", "event: sync", 'data: {"cursor": ""}'),
            comments=[comment_payload(updated_at="2026-09-26T13:00:00+00:00")],
        )
        sync = CollaborationSync(index, server_url=SERVER, access_token=TOKEN, client=client)
        stream = WaitingStream(sync)
        await stream.follow_once()

        assert stream.cursor == "2026-09-26T13:00:00+00:00"

    async def test_it_sends_the_cursor_as_last_event_id(self, index):
        """The same mechanism a browser's EventSource uses, arriving at
        the same server parameter as ``since``."""

        client, seen = self.streaming_server(b": keepalive\n\n")
        sync = CollaborationSync(index, server_url=SERVER, access_token=TOKEN, client=client)
        stream = WaitingStream(sync)
        stream._cursor = "2026-09-26T09:00:00+00:00"
        await stream.follow_once()

        opened = next(r for r in seen if r.url.path.endswith("/notifications/stream"))
        assert opened.headers["Last-Event-ID"] == "2026-09-26T09:00:00+00:00"

    async def test_on_change_is_called_with_what_landed(self, index):

        client, _ = self.streaming_server(
            self.sse("event: sync", 'data: {"cursor": ""}'),
            comments=[comment_payload()],
        )
        sync = CollaborationSync(index, server_url=SERVER, access_token=TOKEN, client=client)
        seen: list[int] = []
        await WaitingStream(sync).follow_once(on_change=seen.append)
        assert seen == [1]

    async def test_a_nudge_with_no_comments_in_it_still_reaches_the_screen(self, index):
        """The bug that made every non-comment notification invisible.

        ``on_change`` used to be called only ``if projected`` — a
        leftover from when this followed the comment channel, where a
        nudge that projected nothing genuinely had nothing to say.
        The channel now carries somebody asking for access, somebody
        being given a document, somebody publishing a version. All
        three project zero comments, so all three were swallowed one
        line before reaching the screen: the row was written on the
        server, the nudge crossed the network, and the panel sat
        there showing yesterday's news until it was closed and
        reopened.
        """
        client, _ = self.streaming_server(
            self.sse("event: waiting", "data: 1"),
            comments=[],
        )
        sync = CollaborationSync(index, server_url=SERVER, access_token=TOKEN, client=client)
        seen: list[int] = []

        await WaitingStream(sync).follow_once(on_change=seen.append)

        assert seen == [0]

    async def test_a_failing_stream_does_not_raise(self, index):
        """A laptop waking to a dead deployment retries; it does not
        crash the session that started the stream."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"detail": "down"})

        sync = CollaborationSync(
            index,
            server_url=SERVER,
            access_token=TOKEN,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        stream = WaitingStream(sync)
        stream.stop()
        await stream.run()  # must simply return

    async def test_stop_ends_the_loop(self, index):

        client, _ = self.streaming_server(b": keepalive\n\n")
        sync = CollaborationSync(index, server_url=SERVER, access_token=TOKEN, client=client)
        stream = WaitingStream(sync)
        task = asyncio.create_task(stream.run())
        await asyncio.sleep(0.05)
        stream.stop()
        await asyncio.wait_for(task, timeout=5)


class TestEditingThroughTheDraft:
    """A shared document keeps its draft where everybody can see it.

    The local file is for documents nobody has shared — see
    ``test_an_edit_waits_in_the_draft.py``. Here the question is the
    other one: that a document *with* a shared copy puts its draft on
    the server rather than on the machine that happened to type it,
    because a draft only one person can read is an unsaved editor
    with extra steps.
    """

    @pytest.fixture
    def desk(self, index, sync):
        from ember_code.core.artifacts.draft_desk import DraftDesk

        return DraftDesk(index, sync=sync, author="maya@acme.test")

    @pytest.fixture(autouse=True)
    def shared(self, index, server, monkeypatch, sync):
        """This document has a shared copy called ``s1``."""
        server.head_n = 1
        server.versions = [version_payload(1, "Auth is a session cookie.")]

        async def slug_for(_ref: str) -> str:
            return "s1"

        monkeypatch.setattr(sync, "slug_for", slug_for)

    async def test_the_draft_goes_to_the_server(self, desk, server):
        await desk.save(CODEC.artifact_id("Auth model"), "Being rewritten.")

        assert server.draft is not None
        assert server.draft["content"] == "Being rewritten."

    async def test_no_version_is_made(self, desk, server):
        await desk.save(CODEC.artifact_id("Auth model"), "Being rewritten.")

        assert server.head_n == 1

    async def test_it_is_written_from_the_current_head(self, desk, server):
        # Not from whatever the editor remembered when it opened: a
        # base that is already behind is a conflict at publish time
        # over a version nobody disagreed with.
        server.head_n = 4
        await desk.save(CODEC.artifact_id("Auth model"), "From wherever we are.")

        assert server.draft["base_n"] == 4

    async def test_reading_it_back_says_who_touched_it(self, desk):
        # The thing a local draft cannot answer, and the reason a
        # shared one is worth the round trip.
        await desk.save(CODEC.artifact_id("Auth model"), "Mine for now.")

        draft = await desk.read(CODEC.artifact_id("Auth model"))
        assert draft.updated_by == "sam@acme.test"

    async def test_publishing_makes_the_version_there(self, desk, server):
        await desk.save(CODEC.artifact_id("Auth model"), "Auth is a signed JWT.")

        n = await desk.publish(CODEC.artifact_id("Auth model"))

        assert n == 2
        assert server.head_n == 2

    async def test_the_local_chain_mirrors_it(self, desk, index):
        # Published means published everywhere. A version that exists
        # on the server and not here would come back on the next pull
        # and read as somebody else's work.
        await desk.save(CODEC.artifact_id("Auth model"), "Auth is a signed JWT.")
        await desk.publish(CODEC.artifact_id("Auth model"))

        head = await index.version("Auth model", 0)
        assert head is not None
        assert "signed JWT" in head.content

    async def test_the_mirror_is_not_pushed_back(self, desk, server):
        await desk.save(CODEC.artifact_id("Auth model"), "Auth is a signed JWT.")
        await desk.publish(CODEC.artifact_id("Auth model"))

        pushes = [
            r for r in server.requests if r.method == "POST" and r.url.path.endswith("/versions")
        ]
        assert pushes == [], "the server already has it; pushing would carry a stale base"

    async def test_publishing_clears_it(self, desk, server):
        await desk.save(CODEC.artifact_id("Auth model"), "Done.")
        await desk.publish(CODEC.artifact_id("Auth model"))

        assert server.draft is None

    async def test_there_is_nothing_to_publish_without_a_draft(self, desk):
        from ember_code.core.artifacts.draft_desk import DraftRefused

        with pytest.raises(DraftRefused):
            await desk.publish(CODEC.artifact_id("Auth model"))

    async def test_somebody_publishing_first_is_said_plainly(self, desk, server):
        from ember_code.core.artifacts.draft_desk import PublishedUnderneath

        await desk.save(CODEC.artifact_id("Auth model"), "Written from v1.")
        server.conflict = {"detail": "Somebody published v2.", "head_n": 2}

        with pytest.raises(PublishedUnderneath):
            await desk.publish(CODEC.artifact_id("Auth model"))

    async def test_a_refused_publish_keeps_the_draft(self, desk, server):
        # The moment the draft matters most: it is the only copy of
        # what was written from the older version.
        from ember_code.core.artifacts.draft_desk import PublishedUnderneath

        await desk.save(CODEC.artifact_id("Auth model"), "Written from v1.")
        server.conflict = {"detail": "Somebody published v2.", "head_n": 2}

        with contextlib.suppress(PublishedUnderneath):
            await desk.publish(CODEC.artifact_id("Auth model"))

        assert server.draft is not None
        assert server.draft["content"] == "Written from v1."

    async def test_nothing_is_written_locally_until_it_is_published(self, desk, index):
        await desk.save(CODEC.artifact_id("Auth model"), "Still being written.")

        head = await index.version("Auth model", 0)
        assert head is not None
        assert "session cookie" in head.content


class TestAConnectionThatDiedWhileNobodyWasLooking:
    """A pooled keep-alive outliving the server on the other end.

    Deploy the cloud, restart its container, let an idle socket be
    reaped — in all three cases the pool still holds a connection it
    believes is usable. The write goes into a closed socket and the
    read fails. Nothing was processed, so the honest answer is to
    send it again; the previous behaviour was to fail the call and
    keep failing it, leaving every screen in the app broken until the
    local backend was restarted too.
    """

    async def _sync_that_dies_once(self, index, error: Exception):
        attempts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(request.url.path)
            if len(attempts) == 1:
                raise error
            return httpx.Response(200, json={"notifications": [], "waiting": 0})

        sync = CollaborationSync(
            index,
            server_url=SERVER,
            access_token=TOKEN,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        return sync, attempts

    @pytest.mark.parametrize(
        "error",
        [
            httpx.RemoteProtocolError("Server disconnected without sending a response."),
            httpx.ReadError(""),
            httpx.WriteError(""),
        ],
        ids=["disconnected", "read", "write"],
    )
    async def test_the_second_attempt_is_the_one_that_answers(self, index, error):
        sync, attempts = await self._sync_that_dies_once(index, error)

        answer = await sync.waiting()

        assert answer == {"notifications": [], "waiting": 0}
        assert len(attempts) == 2

    async def test_it_gives_up_rather_than_retrying_forever(self, index):
        attempts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(request.url.path)
            raise httpx.ReadError("")

        sync = CollaborationSync(
            index,
            server_url=SERVER,
            access_token=TOKEN,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        with pytest.raises(httpx.ReadError):
            await sync.waiting()
        assert len(attempts) == 2

    async def test_a_server_that_is_simply_down_is_not_retried(self, index):
        # Refused is not stale. The server is not there, and asking a
        # microsecond later answers the same thing more slowly.
        attempts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(request.url.path)
            raise httpx.ConnectError("Connection refused")

        sync = CollaborationSync(
            index,
            server_url=SERVER,
            access_token=TOKEN,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        with pytest.raises(httpx.ConnectError):
            await sync.waiting()
        assert len(attempts) == 1
