"""End-to-end: the artifact store on a real Neo4j.

Covers the three claims the subsystem rests on, each of which is only
observable against a live database:

1. A write **appends**. The previous version keeps its content and
   loses only its head flag.
2. A derivation edge pins the version it read, and the pin does not
   move when the source is revised.
3. Staleness is derivable from the graph alone — ``pinned_n <
   head_n`` — with nothing written at revision time.

Live integration — requires ``NEO4J_TEST_URI``. Skipped otherwise.
"""

from __future__ import annotations

import os
import uuid

import pytest
from neo4j import AsyncGraphDatabase

from ember_code.core.artifacts.models import Citation, EdgeState, VersionPin
from ember_code.core.artifacts.neo4j_client import Neo4jArtifactClient
from ember_code.core.code_index.embedder import HashEmbedder

_REQUIRED = "NEO4J_TEST_URI"


def _has_neo4j() -> bool:
    return bool(os.environ.get(_REQUIRED))


pytestmark = pytest.mark.skipif(not _has_neo4j(), reason=f"{_REQUIRED} not set")


@pytest.fixture
async def driver():
    d = AsyncGraphDatabase.driver(
        os.environ[_REQUIRED],
        auth=(
            os.environ.get("NEO4J_TEST_USER", "neo4j"),
            os.environ.get("NEO4J_TEST_PASSWORD", "test"),
        ),
    )
    try:
        yield d
    finally:
        await d.close()


@pytest.fixture
async def client(driver):
    project_id = uuid.uuid4().hex[:32]
    async with driver.session() as s:
        await s.run("MATCH (n {project_hash: $p}) DETACH DELETE n", p=project_id)
    c = Neo4jArtifactClient(driver, project_id)
    await c.apply_schema()
    yield c
    async with driver.session() as s:
        await s.run("MATCH (n {project_hash: $p}) DETACH DELETE n", p=project_id)


@pytest.fixture
def embed():
    embedder = HashEmbedder()
    return lambda text: embedder.embed([text])[0]


async def write(client, embed, *, artifact_id, name, content, **kw):
    return await client.append_version(
        artifact_id=artifact_id,
        name=name,
        content=content,
        embedding=embed(content),
        **kw,
    )


class TestVersionsAppend:
    async def test_a_second_write_appends_rather_than_overwriting(self, client, embed):
        await write(client, embed, artifact_id="doc", name="Doc", content="first")
        outcome = await write(client, embed, artifact_id="doc", name="Doc", content="second")

        assert outcome.n == 2
        v1 = await client.version("doc", 1)
        v2 = await client.version("doc", 2)
        assert v1 is not None and v1.content == "first"
        assert v2 is not None and v2.content == "second"

    async def test_only_the_newest_version_is_head(self, client, embed):
        await write(client, embed, artifact_id="doc", name="Doc", content="first")
        await write(client, embed, artifact_id="doc", name="Doc", content="second")

        versions = await client.versions("doc")
        assert [(v.n, v.head) for v in versions] == [(2, True), (1, False)]

    async def test_head_n_tracks_the_latest(self, client, embed):
        for i in range(3):
            await write(client, embed, artifact_id="doc", name="Doc", content=f"v{i}")
        summary = await client.get_artifact("doc")
        assert summary is not None
        assert summary.head_n == 3
        assert summary.version_count == 3

    async def test_version_zero_resolves_to_head(self, client, embed):
        await write(client, embed, artifact_id="doc", name="Doc", content="first")
        await write(client, embed, artifact_id="doc", name="Doc", content="second")
        head = await client.version("doc", 0)
        assert head is not None and head.content == "second"

    async def test_content_hash_changes_with_content(self, client, embed):
        await write(client, embed, artifact_id="doc", name="Doc", content="first")
        await write(client, embed, artifact_id="doc", name="Doc", content="second")
        versions = await client.versions("doc")
        assert versions[0].content_hash != versions[1].content_hash


class TestSearchSeesOnlyHeads:
    async def test_a_superseded_version_never_surfaces(self, client, embed):
        """Retrieving replaced text is how an agent repeats a fact
        that was corrected three revisions ago."""
        await write(client, embed, artifact_id="doc", name="Doc", content="the old wrong answer")
        await write(client, embed, artifact_id="doc", name="Doc", content="the new right answer")

        hits = await client.search(query_embedding=embed("the new right answer"), limit=5)
        assert [h.content for h in hits] == ["the new right answer"]

    async def test_a_hit_carries_its_version_number(self, client, embed):
        await write(client, embed, artifact_id="doc", name="Doc", content="body text")
        hits = await client.search(query_embedding=embed("body text"), limit=5)
        assert hits[0].ref == "doc@1"


class TestDerivationPinsAVersion:
    async def test_a_bare_pin_resolves_to_head_at_write_time(self, client, embed):
        await write(client, embed, artifact_id="src", name="Source", content="s1")
        await write(client, embed, artifact_id="src", name="Source", content="s2")
        await write(
            client,
            embed,
            artifact_id="dep",
            name="Dependent",
            content="d1",
            pins=[VersionPin(artifact_id="src", n=0, state=EdgeState.CONFIRMED)],
        )

        lineage = await client.lineage("dep", 1)
        assert [(e.to_artifact_id, e.pinned_n) for e in lineage.sources] == [("src", 2)]

    async def test_the_pin_does_not_move_when_the_source_is_revised(self, client, embed):
        await write(client, embed, artifact_id="src", name="Source", content="s1")
        await write(
            client,
            embed,
            artifact_id="dep",
            name="Dependent",
            content="d1",
            pins=[VersionPin(artifact_id="src", n=1, state=EdgeState.CONFIRMED)],
        )
        await write(client, embed, artifact_id="src", name="Source", content="s2")

        lineage = await client.lineage("dep", 1)
        edge = lineage.sources[0]
        assert edge.pinned_n == 1
        assert edge.source_head_n == 2

    async def test_an_unknown_source_is_reported_not_silently_dropped(self, client, embed):
        outcome = await write(
            client,
            embed,
            artifact_id="dep",
            name="Dependent",
            content="d1",
            pins=[VersionPin(artifact_id="ghost", n=1, state=EdgeState.CONFIRMED)],
        )
        assert outcome.unresolved_pins == ["ghost"]
        assert outcome.edges_written == 0

    async def test_the_content_still_lands_when_a_source_is_unknown(self, client, embed):
        """Losing the document because one reference was stale would
        be a worse failure than the bad reference."""
        await write(
            client,
            embed,
            artifact_id="dep",
            name="Dependent",
            content="d1",
            pins=[VersionPin(artifact_id="ghost", n=1, state=EdgeState.CONFIRMED)],
        )
        stored = await client.version("dep", 1)
        assert stored is not None and stored.content == "d1"

    async def test_dependents_are_visible_from_the_source(self, client, embed):
        await write(client, embed, artifact_id="src", name="Source", content="s1")
        await write(
            client,
            embed,
            artifact_id="dep",
            name="Dependent",
            content="d1",
            pins=[VersionPin(artifact_id="src", n=1, state=EdgeState.CONFIRMED)],
        )
        lineage = await client.lineage("src", 1)
        assert [e.from_artifact_id for e in lineage.dependents] == ["dep"]

    async def test_citations_round_trip(self, client, embed):
        await write(
            client,
            embed,
            artifact_id="dep",
            name="Dependent",
            content="d1",
            citations=[Citation(url="https://x.test/spec", kind="web")],
        )
        lineage = await client.lineage("dep", 1)
        assert [c.url for c in lineage.citations] == ["https://x.test/spec"]


class TestStalenessIsDerived:
    async def test_a_moved_source_makes_its_dependent_stale(self, client, embed):
        await write(client, embed, artifact_id="src", name="Source", content="s1")
        await write(
            client,
            embed,
            artifact_id="dep",
            name="Dependent",
            content="d1",
            pins=[VersionPin(artifact_id="src", n=1, state=EdgeState.CONFIRMED)],
        )
        assert await client.stale_dependents() == []

        await write(client, embed, artifact_id="src", name="Source", content="s2")

        stale = await client.stale_dependents()
        assert len(stale) == 1
        assert stale[0].dependent_artifact_id == "dep"
        assert (stale[0].pinned_n, stale[0].source_head_n) == (1, 2)
        assert stale[0].versions_behind == 1

    async def test_candidate_edges_do_not_raise_staleness(self, client, embed):
        """An unreviewed capture is a guess; alarming on guesses
        teaches people to ignore alarms."""
        await write(client, embed, artifact_id="src", name="Source", content="s1")
        outcome = await write(client, embed, artifact_id="dep", name="Dependent", content="d1")
        await client.record_candidates(
            version_id=outcome.version_id, pins=[VersionPin(artifact_id="src", n=1)]
        )
        await write(client, embed, artifact_id="src", name="Source", content="s2")

        assert await client.stale_dependents() == []

    async def test_confirming_a_candidate_makes_it_count(self, client, embed):
        await write(client, embed, artifact_id="src", name="Source", content="s1")
        outcome = await write(client, embed, artifact_id="dep", name="Dependent", content="d1")
        await client.record_candidates(
            version_id=outcome.version_id, pins=[VersionPin(artifact_id="src", n=1)]
        )
        await write(client, embed, artifact_id="src", name="Source", content="s2")

        await client.set_edge_state(
            artifact_id="dep", n=1, source_artifact_id="src", state=EdgeState.CONFIRMED
        )
        stale = await client.stale_dependents()
        assert [s.dependent_artifact_id for s in stale] == ["dep"]

    async def test_a_superseded_dependent_is_not_reported(self, client, embed):
        """Old versions being out of date is a fact about history."""
        await write(client, embed, artifact_id="src", name="Source", content="s1")
        await write(
            client,
            embed,
            artifact_id="dep",
            name="Dependent",
            content="d1",
            pins=[VersionPin(artifact_id="src", n=1, state=EdgeState.CONFIRMED)],
        )
        await write(client, embed, artifact_id="src", name="Source", content="s2")
        # The dependent is revised and re-pinned to the new head.
        await write(
            client,
            embed,
            artifact_id="dep",
            name="Dependent",
            content="d2",
            pins=[VersionPin(artifact_id="src", n=2, state=EdgeState.CONFIRMED)],
        )
        assert await client.stale_dependents() == []

    async def test_repin_resolves_without_a_content_change(self, client, embed):
        await write(client, embed, artifact_id="src", name="Source", content="s1")
        await write(
            client,
            embed,
            artifact_id="dep",
            name="Dependent",
            content="d1",
            pins=[VersionPin(artifact_id="src", n=1, state=EdgeState.CONFIRMED)],
        )
        await write(client, embed, artifact_id="src", name="Source", content="s2")

        moved = await client.repin(artifact_id="dep", n=1, source_artifact_id="src")
        assert moved == 1
        assert await client.stale_dependents() == []
        still_there = await client.version("dep", 1)
        assert still_there is not None and still_there.content == "d1"


class TestCandidateLifecycle:
    async def test_a_candidate_never_downgrades_a_confirmed_edge(self, client, embed):
        await write(client, embed, artifact_id="src", name="Source", content="s1")
        outcome = await write(
            client,
            embed,
            artifact_id="dep",
            name="Dependent",
            content="d1",
            pins=[VersionPin(artifact_id="src", n=1, state=EdgeState.CONFIRMED)],
        )
        await client.record_candidates(
            version_id=outcome.version_id, pins=[VersionPin(artifact_id="src", n=1)]
        )
        lineage = await client.lineage("dep", 1)
        assert lineage.sources[0].state is EdgeState.CONFIRMED

    async def test_dismissing_keeps_the_edge_out_of_staleness(self, client, embed):
        await write(client, embed, artifact_id="src", name="Source", content="s1")
        await write(
            client,
            embed,
            artifact_id="dep",
            name="Dependent",
            content="d1",
            pins=[VersionPin(artifact_id="src", n=1, state=EdgeState.CONFIRMED)],
        )
        await client.set_edge_state(
            artifact_id="dep", n=1, source_artifact_id="src", state=EdgeState.DISMISSED
        )
        await write(client, embed, artifact_id="src", name="Source", content="s2")
        assert await client.stale_dependents() == []

    async def test_pruning_drops_old_candidates_only(self, client, embed):
        await write(client, embed, artifact_id="src", name="Source", content="s1")
        await write(client, embed, artifact_id="keep", name="Keep", content="k1")
        outcome = await write(
            client,
            embed,
            artifact_id="dep",
            name="Dependent",
            content="d1",
            pins=[VersionPin(artifact_id="keep", n=1, state=EdgeState.CONFIRMED)],
        )
        await client.record_candidates(
            version_id=outcome.version_id, pins=[VersionPin(artifact_id="src", n=1)]
        )
        pruned = await client.prune_candidates(before_iso="2999-01-01T00:00:00+00:00")
        assert pruned == 1
        lineage = await client.lineage("dep", 1)
        assert [e.to_artifact_id for e in lineage.sources] == ["keep"]

    async def test_candidate_count_reflects_head_versions(self, client, embed):
        await write(client, embed, artifact_id="src", name="Source", content="s1")
        outcome = await write(client, embed, artifact_id="dep", name="Dependent", content="d1")
        await client.record_candidates(
            version_id=outcome.version_id, pins=[VersionPin(artifact_id="src", n=1)]
        )
        assert await client.candidate_count() == 1


class TestLookupAndDelete:
    async def test_find_by_name_is_case_insensitive(self, client, embed):
        await write(client, embed, artifact_id="doc", name="Auth Design", content="body")
        found = await client.find_by_name("auth design")
        assert found is not None and found.artifact_id == "doc"

    async def test_counts_cover_artifacts_and_versions(self, client, embed):
        await write(client, embed, artifact_id="a", name="A", content="1")
        await write(client, embed, artifact_id="a", name="A", content="2")
        await write(client, embed, artifact_id="b", name="B", content="1")
        assert await client.count() == 2
        assert await client.version_count() == 3

    async def test_delete_removes_every_version(self, client, embed):
        await write(client, embed, artifact_id="doc", name="Doc", content="1")
        await write(client, embed, artifact_id="doc", name="Doc", content="2")
        await client.delete_artifact("doc")

        assert await client.has_artifact("doc") is False
        assert await client.versions("doc") == []
        assert await client.count() == 0

    async def test_deleting_a_source_removes_its_dependents_edges(self, client, embed):
        """A dangling pin would claim a provenance that no longer
        exists, which is worse than a missing one."""
        await write(client, embed, artifact_id="src", name="Source", content="s1")
        await write(
            client,
            embed,
            artifact_id="dep",
            name="Dependent",
            content="d1",
            pins=[VersionPin(artifact_id="src", n=1, state=EdgeState.CONFIRMED)],
        )
        await client.delete_artifact("src")
        lineage = await client.lineage("dep", 1)
        assert lineage.sources == []
