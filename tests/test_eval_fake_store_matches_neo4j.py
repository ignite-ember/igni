"""The archivist eval's fake store must agree with the real one.

The eval grades the agent on judgements it makes *in response to what
the store tells it*: which documents are stale, how far behind a pin
is, whether a source was confirmed or merely captured. If the fake
reported any of that differently from Neo4j, every case would be
grading the agent against feedback the product never produces, and a
green suite would mean nothing.

So the same scenario runs through both, and the answers are compared.
This is the test that earns the fake its place — without it, the fake
is an assertion that two implementations agree, and assertions drift.

Live integration: needs ``NEO4J_TEST_URI``. Skipped otherwise, which
is worth saying out loud — a green run of the suite as a whole does
not mean this ran.
"""

from __future__ import annotations

import os
import uuid

import pytest
from neo4j import AsyncGraphDatabase

from ember_code.core.artifacts.index import ArtifactIndex
from ember_code.core.artifacts.metadata_codec import ArtifactMetadataCodec
from ember_code.core.artifacts.models import EdgeState, SourceDeclaration, VersionPin
from ember_code.core.artifacts.neo4j_client import Neo4jArtifactClient
from ember_code.core.code_index.embedder import HashEmbedder
from evals.archivist.setup import _seed
from evals.archivist.store import FakeArtifactStore

_REQUIRED = "NEO4J_TEST_URI"

pytestmark = pytest.mark.skipif(not os.environ.get(_REQUIRED), reason=f"{_REQUIRED} not set")

CODEC = ArtifactMetadataCodec()


@pytest.fixture
async def real_index(tmp_path):
    driver = AsyncGraphDatabase.driver(
        os.environ[_REQUIRED],
        auth=(
            os.environ.get("NEO4J_TEST_USER", "neo4j"),
            os.environ.get("NEO4J_TEST_PASSWORD", "test"),
        ),
    )
    project_id = uuid.uuid4().hex[:32]
    client = Neo4jArtifactClient(driver, project_id)
    await client.apply_schema()
    index = ArtifactIndex(project=tmp_path, neo4j_client=client, embedder=HashEmbedder())
    await _seed(index)
    try:
        yield index
    finally:
        async with driver.session() as session:
            await session.run("MATCH (n {project_hash: $p}) DETACH DELETE n", p=project_id)
        await driver.close()


@pytest.fixture
async def fake_index(tmp_path):
    index = ArtifactIndex(
        project=tmp_path,
        neo4j_client=FakeArtifactStore(),  # type: ignore[arg-type]
        embedder=HashEmbedder(),
    )
    await _seed(index)
    return index


def _stale_shape(rows) -> list[tuple]:
    return sorted(
        (r.dependent_name, r.source_name, r.pinned_n, r.source_head_n, r.versions_behind)
        for r in rows
    )


def _lineage_shape(lineage) -> list[tuple]:
    return sorted(
        (e.to_name, e.pinned_n, e.source_head_n, e.state.value, e.is_stale) for e in lineage.sources
    )


class TestTheFixtureIsTheSameOnBoth:
    async def test_the_same_documents_exist(self, real_index, fake_index):
        assert await real_index.count() == await fake_index.count()
        assert await real_index.version_count() == await fake_index.version_count()

    async def test_the_same_heads(self, real_index, fake_index):
        real = sorted((a.name, a.head_n) for a in await real_index.list_artifacts())
        fake = sorted((a.name, a.head_n) for a in await fake_index.list_artifacts())
        assert real == fake

    async def test_one_unadjudicated_capture_on_both(self, real_index, fake_index):
        assert await real_index.candidate_count() == 1
        assert await fake_index.candidate_count() == 1


class TestTheyAgreeOnStaleness:
    """The answer the eval leans on hardest.

    Two rows, and the distinction between them is the whole point of
    the reconcile cases: one needs an edit, one needs a re-pin.
    """

    async def test_the_same_rows_are_stale(self, real_index, fake_index):
        real = _stale_shape(await real_index.stale())
        fake = _stale_shape(await fake_index.stale())
        assert real == fake

    async def test_both_report_two_rows_with_the_expected_gap(self, fake_index):
        rows = _stale_shape(await fake_index.stale())
        assert rows == [
            ("Login flow", "Auth model", 1, 3, 2),
            ("Release checklist", "Auth model", 2, 3, 1),
        ]


class TestTheyAgreeOnLineage:
    async def test_login_flow_sources_match(self, real_index, fake_index):
        real = await real_index.lineage("Login flow")
        fake = await fake_index.lineage("Login flow")
        assert _lineage_shape(real) == _lineage_shape(fake)

    async def test_the_candidate_is_a_candidate_on_both(self, real_index, fake_index):
        for index in (real_index, fake_index):
            lineage = await index.lineage("Login flow")
            states = {e.to_name: e.state for e in lineage.sources}
            assert states["Rate limiting notes"] is EdgeState.CANDIDATE
            assert states["Auth model"] is EdgeState.CONFIRMED


class TestTheyAgreeOnVersions:
    async def test_superseded_content_is_addressable_on_both(self, real_index, fake_index):
        real = await real_index.version("Auth model", 1)
        fake = await fake_index.version("Auth model", 1)
        assert real is not None and fake is not None
        assert real.content == fake.content
        assert "session cookie" in real.content

    async def test_head_is_the_same_version_on_both(self, real_index, fake_index):
        real = await real_index.version("Auth model", 0)
        fake = await fake_index.version("Auth model", 0)
        assert real is not None and fake is not None
        assert (real.n, real.head) == (fake.n, fake.head) == (3, True)


class TestTheyAgreeAfterAWrite:
    """Agreement on the seeded state is not enough — the eval's cases
    mutate the store, and the fake has to stay right afterwards."""

    async def test_a_repin_clears_the_row_on_both(self, real_index, fake_index):
        for index in (real_index, fake_index):
            stale = await index.stale()
            plan = await index.reconcile_plan(
                next(r for r in stale if r.dependent_name == "Release checklist")
            )
            assert plan is not None
            await index.accept_reconcile(plan=plan)

        real = _stale_shape(await real_index.stale())
        fake = _stale_shape(await fake_index.stale())
        assert real == fake
        assert [r[0] for r in fake] == ["Login flow"]

    async def test_a_revision_appends_on_both(self, real_index, fake_index):
        auth = CODEC.artifact_id("Auth model")
        for index in (real_index, fake_index):
            await index.revise(
                ref=auth,
                content="A fourth version.",
                sources=SourceDeclaration(
                    pins=[
                        VersionPin(
                            artifact_id=CODEC.artifact_id("Error taxonomy"),
                            n=1,
                            state=EdgeState.CONFIRMED,
                        )
                    ]
                ),
            )
        real = await real_index.resolve("Auth model")
        fake = await fake_index.resolve("Auth model")
        assert real is not None and fake is not None
        assert real.head_n == fake.head_n == 4

    async def test_dismissing_survives_on_both(self, real_index, fake_index):
        for index in (real_index, fake_index):
            await index.dismiss_source(ref="Login flow", source_ref="Rate limiting notes")
            lineage = await index.lineage("Login flow")
            states = {e.to_name: e.state for e in lineage.sources}
            assert states["Rate limiting notes"] is EdgeState.DISMISSED
