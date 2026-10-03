"""The agent can say which paragraph it drew on, and does.

Provenance used to be a claim: "this document was built on that one",
which nobody can check. Worse, the mechanism behind it was a model
comparing two texts *after the fact* and offering a verdict — that
reads like evidence and is a guess, with nothing to audit and no way
to tell a good one from a bad one.

A passage id fixes the checkable part. Search hands one back per
result; quoting it when writing says "I drew on *this paragraph*",
and anybody can render it and judge. It stays checkable because the id
names a version that never changes.

What it does **not** fix: an agent told to cite will sometimes cite
loosely — a passage it saw but did not use. The id makes that
detectable, which the verdict never was. The evidence is only as good
as the agent's discipline; the gain is that somebody can now audit it.

So four things have to hold, and each was broken before this:

1. the server's passage id survives into the hit the agent sees;
2. the agent is *told* to cite it, in the tool it reads;
3. a cited id parses into a pin that carries the passage;
4. the pin reaches the server with the passage attached.
"""

from __future__ import annotations

from typing import Any

import pytest

from ember_code.core.artifacts.models import ArtifactSearchHit, VersionPin
from evals.archivist.store import FakeArtifactStore, FakeSharedServer


def _index(shared: Any | None):
    from ember_code.core.artifacts.index import ArtifactIndex

    index = ArtifactIndex(project="cite-test", neo4j_client=FakeArtifactStore())  # type: ignore[arg-type]
    if shared is not None:
        index.attach_shared(shared)
    return index


def _hit(**overrides) -> dict:
    return {
        'chunk_id': 'doc-7::3::2',
        'slug': 's1',
        'artifact_id': 'a1',
        'title': 'Release checklist',
        'text': 'Page the on-call engineer first.',
        'line_from': 7,
        'line_to': 9,
        'version_n': 3,
        'score': 0.9,
        **overrides,
    }


class TestThePassageIdSurvives:
    async def test_a_hit_keeps_the_id_the_server_sent(self):
        """Dropped once already: the hit kept lines and slug and threw
        away the one thing worth citing."""
        server = FakeSharedServer()
        server.hits = [_hit()]

        hits = await _index(server).search(query='rollback', record=False)

        assert hits[0].chunk_id == 'doc-7::3::2'

    async def test_a_server_that_sends_none_is_not_an_error(self):
        """An older deployment, or a hit from somewhere without
        passages. The document ref still works; the citation is just
        weaker."""
        server = FakeSharedServer()
        server.hits = [_hit(chunk_id=None)]

        hits = await _index(server).search(query='rollback', record=False)

        assert hits[0].chunk_id == ''
        assert hits[0].artifact_id == 'a1'


class TestTheAgentIsToldToCite:
    """Asserted on the tool text, because that text *is* the mechanism.

    Nothing enforces citation — the agent reads a description and
    complies or does not. A description that stopped asking would
    silently return the product to unverifiable claims, with every
    test still green.
    """

    def _tool_source(self) -> str:
        import inspect

        from ember_code.core.tools.artifacts import ArtifactTools

        return inspect.getsource(ArtifactTools)

    def test_search_prints_the_id_to_cite(self):
        assert 'cite as:' in self._tool_source()

    def test_create_asks_for_the_passage_not_just_the_document(self):
        body = self._tool_source()

        assert 'Prefer the passage id' in body
        assert 'Passage ids from ``artifact_search``' in body

    def test_it_says_what_a_bare_document_id_costs(self):
        """An agent choosing the cheaper option should know what it is
        giving up, or it will always choose the cheaper option."""
        body = self._tool_source()

        assert 'nothing to check' in body


class TestACitationParsesIntoAPin:
    def test_a_passage_id_becomes_a_pin_that_carries_it(self):
        pin = VersionPin.parse('doc-7::3::2')

        assert pin is not None
        assert pin.artifact_id == 'doc-7'
        assert [p.chunk_id for p in pin.passages] == ['doc-7::3::2']

    def test_the_version_comes_from_the_id_not_the_head(self):
        """A passage belongs to the version it was read from. Pinning
        it to whatever the head happens to be at write time would
        claim the agent read text it never saw."""
        assert VersionPin.parse('doc-7::3::2').n == 3

    def test_a_document_ref_still_parses_and_cites_nothing(self):
        pin = VersionPin.parse('design-doc@4')

        assert pin is not None and pin.n == 4
        assert pin.passages == []

    def test_a_bare_name_still_pins_to_the_head(self):
        pin = VersionPin.parse('design-doc')

        assert pin is not None and pin.n == 0

    @pytest.mark.parametrize('bad', ['doc::notanumber::2', 'https://example.test/x', '', '   '])
    def test_rubbish_does_not_become_a_pin(self, bad):
        assert VersionPin.parse(bad) is None


class TestThePinReachesTheServer:
    """Against the real payload builder.

    Asserting on `FakeSharedServer` here would assert the fake: it
    records whatever keyword arguments it is handed, so the
    `CollaborationSync` code that actually serialises a pin would
    never run.
    """

    async def _sent(self, pins) -> dict:
        import json

        import httpx

        from ember_code.core.artifacts.collaboration import CollaborationSync

        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content or b'{}'))
            return httpx.Response(200, json={'n': 1, 'head_n': 1})

        sync = CollaborationSync(
            None,
            server_url='https://igni.example',
            access_token='t',
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        await sync.push_content(slug='s1', content='x', base_n=0, pins=pins)
        return seen

    async def test_passages_are_sent_with_the_pin(self):
        """The last link. A pin whose passages are dropped in transit
        arrives as the weaker claim, and nothing downstream can tell
        the difference."""
        from ember_code.core.artifacts.models import CitedPassage

        pin = VersionPin(
            artifact_id='a1',
            n=3,
            passages=[CitedPassage(chunk_id='doc-7::3::2', quote='Page the on-call engineer first.')],
        )

        body = await self._sent([pin])

        assert body['sources']['pins'][0]['passages'] == [
            {'chunk_id': 'doc-7::3::2', 'quote': 'Page the on-call engineer first.'}
        ]

    async def test_a_pin_without_passages_sends_an_empty_list(self):
        """Not a missing key. A key that is sometimes absent is a shape
        every reader has to special-case."""
        body = await self._sent([VersionPin(artifact_id='a1', n=3)])

        assert body['sources']['pins'][0]['passages'] == []


class TestWhatTheHitLooksLikeToTheAgent:
    async def test_the_search_output_shows_the_id_on_its_own_line(self):
        """The agent has to copy it exactly, and an id buried in a
        heading gets paraphrased."""
        class _Mgr:
            async def search(self, query, limit=5, record=True):
                from ember_code.core.artifacts.models import ArtifactSearchResponse

                return ArtifactSearchResponse(
                    query=query,
                    results=[ArtifactSearchHit(**{k: v for k, v in _hit().items() if k in ArtifactSearchHit.model_fields})],
                    total=1,
                )

        from ember_code.core.tools.artifacts import ArtifactTools

        tools = ArtifactTools.__new__(ArtifactTools)
        tools._mgr = _Mgr()  # type: ignore[attr-defined]
        out = await ArtifactTools.artifact_search(tools, 'rollback')

        assert 'cite as: doc-7::3::2' in out
