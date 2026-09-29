"""A version goes to the shared copy before it goes in the graph.

A document is a shared thing, so the copy everybody reads is the copy
that decides. The ordering is the whole point: writing the graph first
and pushing afterwards produces exactly the state this exists to
prevent — a version that exists on one machine and nowhere else, which
looks from the inside like a version that was saved.

So a server that refuses **fails the write**. Falling back to a local
save would be the same split store arriving quietly instead of loudly,
and the quiet version is the one nobody finds until a colleague asks
where the document went.

Two things are checked here that are easy to get wrong and impossible
to see afterwards:

*Publishing happens once.* The publish endpoint upserts on
``(owner, artifact_id)`` and overwrites ``visibility`` as it goes, so
a client that published on every write would return a document its
author had shared with colleagues to private the next time they typed
in it. Nobody would be told.

*No server is not a refusal.* Artifacts still run for somebody with no
account, and a missing client must leave the old behaviour alone
rather than making every write fail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from ember_code.core.artifacts.collaboration import ChainDiverged
from ember_code.core.artifacts.index import ArtifactIndex
from ember_code.core.artifacts.models import ArtifactSummary, SourceDeclaration
from ember_code.core.code_index.embedder import HashEmbedder


@dataclass
class Appended:
    """What the graph was asked to store."""

    n: int = 1
    artifact_id: str = "a1"
    version_id: str = "v1"
    edges_written: int = 0
    unresolved_pins: list[str] = field(default_factory=list)


class FakeGraph:
    """Enough of the Neo4j client to see whether it was written to."""

    def __init__(self, summary: ArtifactSummary | None = None) -> None:
        self.summary = summary
        self.writes: list[dict[str, Any]] = []

    async def get_artifact(self, artifact_id: str) -> ArtifactSummary | None:
        return self.summary

    async def find_by_name(self, name: str) -> ArtifactSummary | None:
        return self.summary

    async def append_version(self, **kwargs: Any) -> Appended:
        self.writes.append(kwargs)
        return Appended(n=(self.summary.head_n + 1) if self.summary else 1)

    async def record_candidates(self, **_: Any) -> int:
        return 0


class FakeServer:
    """A shared copy that records what it was asked to do."""

    is_configured = True

    def __init__(self, *, slug: str = "s1", fail: Exception | None = None) -> None:
        self._slug = slug
        self._fail = fail
        self.published: list[dict[str, Any]] = []
        self.pushed: list[dict[str, Any]] = []
        #: Set once published, the way the real client's cache is.
        self.known: dict[str, str] = {}

    async def ensure_shared(self, **kwargs: Any) -> str:
        artifact_id = kwargs["artifact_id"]
        if artifact_id in self.known:
            return self.known[artifact_id]
        self.published.append(kwargs)
        self.known[artifact_id] = self._slug
        return self._slug

    async def push_content(self, **kwargs: Any) -> int:
        if self._fail is not None:
            raise self._fail
        self.pushed.append(kwargs)
        return int(kwargs["base_n"]) + 1


def index_for(graph: FakeGraph, server: Any | None) -> ArtifactIndex:
    index = ArtifactIndex(project="/tmp/p", neo4j_client=graph, embedder=HashEmbedder())
    if server is not None:
        index.attach_shared(server)
    return index


async def write(index: ArtifactIndex, name: str = "Rate limiting", content: str = "Limits.") -> Any:
    return await index.create(name=name, content=content, sources=SourceDeclaration())


class TestTheServerIsWrittenFirst:
    @pytest.mark.asyncio
    async def test_a_new_document_is_published_and_pushed(self):
        graph, server = FakeGraph(), FakeServer()

        result = await write(index_for(graph, server))

        assert result.success
        assert len(server.published) == 1
        assert len(server.pushed) == 1

    @pytest.mark.asyncio
    async def test_the_text_that_was_written_is_the_text_that_was_sent(self):
        graph, server = FakeGraph(), FakeServer()

        await write(index_for(graph, server), content="The exact words.")

        assert server.pushed[0]["content"] == "The exact words."

    @pytest.mark.asyncio
    async def test_it_carries_the_rendered_page_so_a_recipient_has_something_to_open(self):
        graph, server = FakeGraph(), FakeServer()

        await write(index_for(graph, server), name="Rate limiting")

        assert "Rate limiting" in server.published[0]["html"]

    @pytest.mark.asyncio
    async def test_the_graph_is_written_too(self):
        # Mirror, not replacement. Search and lineage answer from the
        # graph, and a document that reached only the server is a
        # document nothing here can find.
        graph, server = FakeGraph(), FakeServer()

        await write(index_for(graph, server))

        assert len(graph.writes) == 1


class TestPublishingHappensOnce:
    @pytest.mark.asyncio
    async def test_a_second_version_does_not_publish_again(self):
        # Publishing overwrites visibility. Doing it per write would
        # un-share a shared document every time its author edited it.
        graph = FakeGraph(ArtifactSummary(artifact_id="a1", name="Rate limiting", head_n=1))
        server = FakeServer()
        index = index_for(graph, server)
        server.known["a1"] = "s1"

        await index.revise(ref="a1", content="More limits.", sources=SourceDeclaration())

        assert server.published == []
        assert len(server.pushed) == 1

    @pytest.mark.asyncio
    async def test_the_later_version_still_refreshes_the_page(self):
        # The snapshot has to keep up without the publish call: push
        # carries the html and leaves the sharing alone.
        graph = FakeGraph(ArtifactSummary(artifact_id="a1", name="Rate limiting", head_n=1))
        server = FakeServer()
        index = index_for(graph, server)
        server.known["a1"] = "s1"

        await index.revise(ref="a1", content="More limits.", sources=SourceDeclaration())

        assert "More limits." in server.pushed[0]["html"]


class TestARefusedWriteIsNotStored:
    @pytest.mark.asyncio
    async def test_a_server_that_cannot_be_reached_fails_the_write(self):
        graph = FakeGraph()
        server = FakeServer(fail=RuntimeError("connection refused"))

        result = await write(index_for(graph, server))

        assert not result.success

    @pytest.mark.asyncio
    async def test_and_nothing_is_left_in_the_graph(self):
        # The whole reason the server goes first. A local-only version
        # is indistinguishable from a saved one.
        graph = FakeGraph()
        server = FakeServer(fail=RuntimeError("connection refused"))

        await write(index_for(graph, server))

        assert graph.writes == []

    @pytest.mark.asyncio
    async def test_the_refusal_says_what_went_wrong(self):
        graph = FakeGraph()
        server = FakeServer(fail=RuntimeError("connection refused"))

        result = await write(index_for(graph, server))

        assert "connection refused" in (result.error or "")

    @pytest.mark.asyncio
    async def test_a_document_somebody_else_moved_on_is_refused_by_name(self):
        # Not merged. Two people editing one document is a decision,
        # and a version nobody wrote is the worst answer to it.
        graph = FakeGraph(ArtifactSummary(artifact_id="a1", name="Rate limiting", head_n=1))
        server = FakeServer(fail=ChainDiverged(head_n=4, missing=[]))
        index = index_for(graph, server)
        server.known["a1"] = "s1"

        result = await index.revise(ref="a1", content="x", sources=SourceDeclaration())

        assert not result.success
        assert "v4" in (result.error or "")
        assert graph.writes == []


class TestWithNoAccount:
    @pytest.mark.asyncio
    async def test_a_write_still_works_with_no_server_attached(self):
        graph = FakeGraph()

        result = await write(index_for(graph, None))

        assert result.success
        assert len(graph.writes) == 1

    @pytest.mark.asyncio
    async def test_a_signed_out_client_is_not_a_refusal(self):
        # ``is_configured`` false is "no credentials yet", which is a
        # supported way to run and must not fail every write.
        class SignedOut:
            is_configured = False

        graph = FakeGraph()

        result = await write(index_for(graph, SignedOut()))

        assert result.success
        assert len(graph.writes) == 1
