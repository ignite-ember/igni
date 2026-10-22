"""End-to-end test: ArtifactIndex routes through Neo4jArtifactClient.

The one place the index is exercised against a real database rather
than a fake store, so it is where a Cypher statement that only works
in theory shows up.

It was written against ``add`` / ``add_document`` / ``list_entries``
/ ``has_entry`` / ``delete_entry`` / ``delete_by_query`` — the
knowledge-index surface, all of it gone with the move to versioned
documents. The file went on asserting that vanished API for as long
as it did because these tests skip without ``NEO4J_TEST_URI``, so a
green local run said nothing about them. Rewritten against
``create`` / ``revise`` / ``search`` / ``count`` / ``resolve`` /
``versions`` / ``delete``.

Live integration — requires ``NEO4J_TEST_URI``. Skipped otherwise,
which is worth saying out loud.
"""

from __future__ import annotations

import os
import uuid

import pytest
from neo4j import AsyncGraphDatabase

from ember_code.core.artifacts.index import ArtifactIndex
from ember_code.core.artifacts.manager import ArtifactManager
from ember_code.core.artifacts.models import SourceDeclaration
from ember_code.core.artifacts.neo4j_client import Neo4jArtifactClient
from ember_code.core.code_index.embedder import HashEmbedder
from evals.archivist.store import FakeSharedServer

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
async def project_id(driver):
    pid = uuid.uuid4().hex[:32]
    async with driver.session() as s:
        await s.run("MATCH (n {project_hash: $p}) DETACH DELETE n", p=pid)
    yield pid
    async with driver.session() as s:
        await s.run("MATCH (n {project_hash: $p}) DETACH DELETE n", p=pid)


@pytest.fixture
def artifact_index(tmp_path, driver, project_id) -> ArtifactIndex:
    client = Neo4jArtifactClient(driver, project_id)
    embedder = HashEmbedder()
    index = ArtifactIndex(
        project=tmp_path,
        data_dir=tmp_path / "data",
        neo4j_client=client,
        embedder=embedder,
    )
    # Signed in. Artifacts are a cloud service, so an index with no
    # server attached refuses every write.
    index.attach_shared(FakeSharedServer())
    return index


async def test_artifact_index_round_trips_against_a_real_database(artifact_index):
    """Create, search, count, resolve and delete, all against Neo4j."""
    idx = artifact_index
    # start() is a no-op on the neo4j path (the client is open).
    await idx.start()
    none = SourceDeclaration()

    entries = {
        "Auth": "Passwords must be hashed with bcrypt; never store plaintext.",
        "Deploy": "Deploy via the GitHub Actions workflow; never push to main.",
        "Config": "Configuration is loaded from .igni/config.local.yaml on startup.",
    }
    for name, content in entries.items():
        result = await idx.create(name=name, content=content, sources=none)
        assert result.success is True, result.error
        assert result.artifact_id

    assert await idx.count() == 3

    # Every one is addressable by the name it was given.
    for name in entries:
        summary = await idx.resolve(name)
        assert summary is not None
        assert summary.name == name
        assert summary.head_n == 1

    # A name that was never written resolves to nothing rather than
    # to the nearest match.
    assert await idx.resolve("Does not exist") is None

    # Not search. ``search`` is the server's answer now, not this
    # database's — it returns [] for an index whose shared server has
    # no index of its own, and asserting on it here would be testing
    # the fake server rather than the Cypher this file is about.
    # ``test_search_asks_the_server_not_the_local_graph`` is where
    # that belongs.

    # revise appends rather than replacing: v1 stays addressable.
    revised = await idx.revise(ref="Deploy", content="Deploy from the release job.", sources=none)
    assert revised.success is True, revised.error
    assert [v.n for v in await idx.versions("Deploy")] == [2, 1]
    first = await idx.version("Deploy", 1)
    assert first is not None and first.content == entries["Deploy"]

    # delete takes the whole document, versions included.
    assert await idx.delete("Deploy") is True
    assert await idx.resolve("Deploy") is None
    assert await idx.count() == 2


async def test_artifact_index_neo4j_requires_embedder(tmp_path, driver, project_id):
    """Constructing with neo4j_client but no embedder raises on write."""
    client = Neo4jArtifactClient(driver, project_id)
    idx = ArtifactIndex(
        project=tmp_path,
        data_dir=tmp_path / "data",
        neo4j_client=client,
        # intentionally no embedder=
    )
    idx.attach_shared(FakeSharedServer())
    with pytest.raises(RuntimeError, match="requires an embedder"):
        await idx.create(name="X", content="x", sources=SourceDeclaration())
    # Only the write path. ``search`` used to embed the query here and
    # now asks the server, which does its own embedding — so there is
    # nothing for this index's missing embedder to raise about.


async def test_artifacts_manager_passes_neo4j_client_through(tmp_path, driver, project_id):
    """ArtifactManager.create_artifacts forwards the neo4j_client to
    the constructed ArtifactIndex."""
    from ember_code.core.config.settings import Settings

    client = Neo4jArtifactClient(driver, project_id)
    settings = Settings()
    settings.artifacts.enabled = True
    manager = ArtifactManager(settings, project_dir=tmp_path, neo4j_client=client)
    idx = manager.create_artifacts()
    assert idx is not None
    # Read through the public accessor — the field behind it is
    # ``_client``, and this named ``_neo4j_client`` for long enough to
    # outlive the rename.
    assert idx.client is client


async def test_artifact_index_without_neo4j_raises(tmp_path):
    """The chroma fallback is gone — ``neo4j_client`` is now a required kwarg."""
    with pytest.raises(TypeError, match="neo4j_client"):
        ArtifactIndex(project=tmp_path, data_dir=tmp_path / "data")
