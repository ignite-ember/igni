"""The passage id search hands out resolves wherever a ref is accepted.

Search tells the agent `cite as: <server document>::<n>::<ordinal>`, so
the agent quotes that id back — to confirm a candidate, dismiss one, or
read it. It resolved to nothing: confirm/dismiss answered "No captured
link" about links that existed, because the candidates were filed under
the artifact id, which the passage id does not contain.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.core.artifacts.index import ArtifactIndex


def _index() -> ArtifactIndex:
    index = ArtifactIndex.__new__(ArtifactIndex)
    index._client = MagicMock()
    known = SimpleNamespace(artifact_id="88a3e6040ef8320d", name="Postmortem")
    index._client.get_artifact = AsyncMock(side_effect=lambda aid: known if aid == known.artifact_id else None)
    index._client.find_by_name = AsyncMock(return_value=None)
    return index


@pytest.mark.asyncio
async def test_a_passage_id_from_search_resolves_to_its_document():
    index = _index()
    hit = index._hit_from_server({"chunk_id": "3cfe8a81-fc2c::2::0", "artifact_id": "88a3e6040ef8320d",
                                  "title": "Postmortem", "version_n": 2, "text": "…"})
    index._passage_owners()[hit.chunk_id.split("::", 1)[0]] = hit.artifact_id

    found = await index.resolve("3cfe8a81-fc2c::2::0")

    assert found is not None and found.artifact_id == "88a3e6040ef8320d"


@pytest.mark.asyncio
async def test_a_passage_id_never_seen_in_search_still_resolves_to_nothing():
    """No guessing: an unknown server id is not mapped to anything."""
    assert await _index().resolve("ffffffff-0000::1::0") is None


@pytest.mark.asyncio
async def test_plain_ids_are_unaffected():
    assert (await _index().resolve("88a3e6040ef8320d")).name == "Postmortem"


def test_a_cited_passage_is_written_against_this_clients_document():
    """A passage id opens with the server's document id, and a pin keyed
    by it was skipped by both the local graph and the server push as an
    unknown source — so no passage citation had ever been recorded."""
    from ember_code.core.artifacts.models import SourceDeclaration, VersionPin

    index = _index()
    index._passage_owners()["3cfe8a81-fc2c"] = "88a3e6040ef8320d"
    declared = SourceDeclaration(pins=[
        VersionPin.parse("3cfe8a81-fc2c::2::0"),
        VersionPin.parse("3cfe8a81-fc2c::2::4"),
    ])

    pins = index._localise_passage_pins(declared).pins

    assert len(pins) == 1, "two passages of one version are one edge"
    assert pins[0].artifact_id == "88a3e6040ef8320d" and pins[0].n == 2
    assert [p.chunk_id for p in pins[0].passages] == ["3cfe8a81-fc2c::2::0", "3cfe8a81-fc2c::2::4"]


def test_a_passage_from_an_unseen_document_is_left_alone():
    from ember_code.core.artifacts.models import SourceDeclaration, VersionPin

    declared = SourceDeclaration(pins=[VersionPin.parse("ffffffff-0000::1::0")])

    assert _index()._localise_passage_pins(declared).pins[0].artifact_id == "ffffffff-0000"
