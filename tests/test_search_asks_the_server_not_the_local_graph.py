"""Document search is the server's answer, not this machine's.

It used to be local: the client embedded the query with its own copy
of `all-MiniLM-L6-v2` and queried the Neo4j beside it. That made a
search result a property of *this laptop* — it returned what had been
pulled here, so a document somebody shared with you was searchable
only once you had opened it, and two colleagues asking the same
question got different answers from the same documents.

Worse, it was indistinguishable from an authoritative empty result.
Nothing in the reply said "this is only what I happen to have".

Documents live on the server, so search does too. The server also
decides *what may be ranked* — the access rule is next to the
documents, where it already was for every other read.

What is still local, deliberately: the write path chunks and embeds
into the graph for drift and candidate evidence, which reason about
this session's own reading. Those are questions about what the agent
did here, and the answer is correctly local.
"""

from __future__ import annotations

from typing import Any

import pytest

from evals.archivist.store import FakeArtifactStore, FakeSharedServer


def _index(shared: Any | None):
    from ember_code.core.artifacts.index import ArtifactIndex

    index = ArtifactIndex(
        project="search-test",
        neo4j_client=FakeArtifactStore(),  # type: ignore[arg-type]
    )
    if shared is not None:
        index.attach_shared(shared)
    return index


def _hit(**overrides) -> dict:
    return {
        "slug": "s1",
        "artifact_id": "a1",
        "title": "Rollback steps",
        "text": "Page the on-call engineer first.",
        "line_from": 7,
        "line_to": 9,
        "version_n": 3,
        "score": 0.88,
        **overrides,
    }


class TestItAsksTheServer:
    async def test_the_query_goes_to_the_server(self):
        server = FakeSharedServer()
        server.hits = [_hit()]

        await _index(server).search(query="how do we roll back", limit=4, record=False)

        assert server.searched == {"query": "how do we roll back", "limit": 4}

    async def test_a_hit_keeps_the_identifier_this_client_keys_on(self):
        """The server returns both; without `artifact_id` a caller has
        to walk its slug map backwards, and that map is empty until
        something has been published."""
        server = FakeSharedServer()
        server.hits = [_hit()]

        hits = await _index(server).search(query="rollback", record=False)

        assert [h.artifact_id for h in hits] == ["a1"]
        assert hits[0].name == "Rollback steps"
        assert hits[0].n == 3

    async def test_a_hit_carries_where_the_passage_is(self):
        """Search returns a *passage* now. Where it sits is most of
        what makes one useful — without it a reader opens the document
        and searches again."""
        server = FakeSharedServer()
        server.hits = [_hit()]

        hits = await _index(server).search(query="rollback", record=False)

        assert hits[0].metadata["line_from"] == "7"
        assert hits[0].metadata["line_to"] == "9"
        assert hits[0].metadata["slug"] == "s1"

    async def test_the_limit_is_passed_through(self):
        server = FakeSharedServer()
        server.hits = [_hit(artifact_id=f"a{i}") for i in range(10)]

        hits = await _index(server).search(query="rollback", limit=3, record=False)

        assert len(hits) == 3


class TestWithoutAServer:
    async def test_a_session_with_no_account_searches_nothing(self):
        """Not the local graph. Answering from whatever this machine
        has pulled is indistinguishable from an authoritative empty
        result, which is the failure that justified moving this."""
        server = FakeSharedServer()
        server.is_configured = False  # type: ignore[misc]

        assert await _index(server).search(query="rollback", record=False) == []

    async def test_no_shared_client_at_all_is_the_same(self):
        assert await _index(None).search(query="rollback", record=False) == []

    async def test_a_deployment_without_search_returns_empty(self):
        """`None` from the client means the server has no graph or no
        embedder — a supported deployment, not an error."""
        server = FakeSharedServer()
        server.hits = None

        assert await _index(server).search(query="rollback", record=False) == []


class TestTheRetrievalLedger:
    """A search the agent ran is a read, and a read is what a later
    write may have been built on. Moving search to the server must not
    quietly stop capturing that."""

    async def test_a_recorded_search_pins_what_it_returned(self):
        server = FakeSharedServer()
        server.hits = [_hit(artifact_id="a1", version_n=3)]
        index = _index(server)

        await index.search(query="rollback", record=True)

        pinned = {(p.artifact_id, p.n) for p in index.ledger.candidates()}
        assert ("a1", 3) in pinned, pinned

    async def test_record_false_captures_nothing(self):
        """The panel listing documents is not the agent learning from
        them."""
        server = FakeSharedServer()
        server.hits = [_hit()]
        index = _index(server)

        await index.search(query="rollback", record=False)

        assert not index.ledger.candidates()


class TestNothingEmbedsLocallyToSearch:
    def test_the_search_path_does_not_touch_the_embedder(self):
        """Asserted on the source: the whole point is that this machine
        no longer needs the model to ask a question. A call that
        reappeared here would work, and would quietly restore the
        laptop-dependent answer."""
        import inspect

        from ember_code.core.artifacts.index import ArtifactIndex

        body = inspect.getsource(ArtifactIndex.search)

        assert "_require_embedder" not in body, body
        assert "embed(" not in body, body

    @pytest.mark.parametrize("forbidden", ["query_embedding", "_client.search"])
    def test_it_does_not_query_the_local_graph(self, forbidden):
        import inspect

        from ember_code.core.artifacts.index import ArtifactIndex

        assert forbidden not in inspect.getsource(ArtifactIndex.search)
