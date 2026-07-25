"""End-to-end test: KnowledgeIndex routes through Neo4jKnowledgeClient.

Verifies the seam added in commit bd7a804: when a neo4j_client
is passed at construction, KnowledgeIndex.add / search / count /
list_entries / delete_entry / has_entry / delete_by_query all
hit neo4j. Without a client, the legacy chroma path runs.

Live integration — requires ``NEO4J_TEST_URI``. Skipped otherwise.
"""

from __future__ import annotations

import os
import uuid

import pytest
from neo4j import AsyncGraphDatabase

from ember_code.core.code_index.embedder import HashEmbedder
from ember_code.core.code_index.neo4j_client import Neo4jKnowledgeClient
from ember_code.core.knowledge.index import KnowledgeIndex
from ember_code.core.knowledge.manager import KnowledgeManager

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
def knowledge_index(tmp_path, driver, project_id) -> KnowledgeIndex:
    client = Neo4jKnowledgeClient(driver, project_id)
    embedder = HashEmbedder()
    return KnowledgeIndex(
        project=tmp_path,
        data_dir=tmp_path / "data",
        neo4j_client=client,
        embedder=embedder,
    )


async def test_knowledge_index_add_search_count_round_trip(knowledge_index):
    """Add three entries through the index, search, count, list, verify."""
    idx = knowledge_index
    # start() is a no-op on the neo4j path (the client is open).
    await idx.start()

    entries = {
        "auth": "Passwords must be hashed with bcrypt; never store plaintext.",
        "deploy": "Deploy via the GitHub Actions workflow; never push to main.",
        "config": "Configuration is loaded from .ember/config.local.yaml on startup.",
    }
    for eid, content in entries.items():
        result = await idx.add_document(
            chunks=[content],
            full_content=content,
            name=eid.title(),
            source="test",
        )
        assert result.success is True
        assert result.entry_id is not None
        entries[eid] = result.entry_id  # store the actual neo4j-assigned id

    # The add() shorthand → add_document() also works.
    inline_eid = await idx.add(
        content="A short inline entry.",
        name="Inline",
    )
    assert inline_eid  # non-empty

    # count: 4 entries
    assert await idx.count() == 4

    # list_entries: every entry visible
    listed = await idx.list_entries()
    listed_ids = {e.id for e in listed}
    assert listed_ids == set(entries.values()) | {inline_eid}

    # has_entry: each id present, a bogus one isn't
    for k in list(entries.values()) + [inline_eid]:
        assert await idx.has_entry(k) is True
    assert await idx.has_entry("does-not-exist") is False

    # search: query with the deploy text (HashEmbedder matches by
    # exact-text hash, so querying with the stored content is the
    # only way to get a deterministic hit on the offline embedder;
    # a live model would give approximate matches).
    deploy_content = next(
        content
        for name, content in (
            ("auth", "Passwords must be hashed with bcrypt; never store plaintext."),
            ("deploy", "Deploy via the GitHub Actions workflow; never push to main."),
            ("config", "Configuration is loaded from .ember/config.local.yaml on startup."),
        )
        if name == "deploy"
    )
    results = await idx.search(query=deploy_content, limit=5)
    assert len(results) >= 1
    assert results[0].entry_id == entries["deploy"]
    assert results[0].score is not None

    # delete_by_query: drops the deploy entry. The HashEmbedder
    # is offline-only and gives a high baseline similarity between
    # unrelated entries (~0.5), so this query matches all four
    # entries — ``delete_by_query`` deletes every match. We check
    # the deploy entry is gone (and that count drops by at least
    # one, which it does because the deploy is in the result set).
    deleted = await idx.delete_by_query(query=deploy_content, limit=5)
    assert deleted.deleted >= 1
    assert await idx.has_entry(entries["deploy"]) is False
    # The exact count after delete is encoder-dependent; the
    # important assertion is that the deploy entry is gone.
    assert await idx.count() < 4

    # delete_entry: pick any entry that's still present, remove it
    # explicitly. Use ``add`` to insert a fresh entry we control so
    # we know its id and don't depend on the encoder's match
    # behavior.
    import uuid

    target = "delete-me-" + uuid.uuid4().hex[:8]
    result = await idx.add_document(
        chunks=[target],
        full_content=target,
        name=target,
    )
    assert result.success
    assert await idx.delete_entry(result.entry_id) is True
    assert await idx.has_entry(result.entry_id) is False


async def test_knowledge_index_without_neo4j_raises(tmp_path):
    """The chroma fallback is gone — ``neo4j_client`` is now a required kwarg."""
    with pytest.raises(TypeError, match="neo4j_client"):
        KnowledgeIndex(project=tmp_path, data_dir=tmp_path / "data")


async def test_knowledge_index_neo4j_requires_embedder(tmp_path, driver, project_id):
    """Constructing with neo4j_client but no embedder raises on use."""
    client = Neo4jKnowledgeClient(driver, project_id)
    idx = KnowledgeIndex(
        project=tmp_path,
        data_dir=tmp_path / "data",
        neo4j_client=client,
        # intentionally no embedder=
    )
    with pytest.raises(RuntimeError, match="requires an embedder"):
        await idx.add_document(chunks=["x"], full_content="x")
    with pytest.raises(RuntimeError, match="requires an embedder"):
        await idx.search(query="x")


async def test_knowledge_manager_passes_neo4j_client_through(tmp_path, driver, project_id):
    """KnowledgeManager.create_knowledge forwards the neo4j_client to
    the constructed KnowledgeIndex."""
    from ember_code.core.config.settings import Settings

    client = Neo4jKnowledgeClient(driver, project_id)
    settings = Settings()
    settings.knowledge.enabled = True
    manager = KnowledgeManager(settings, project_dir=tmp_path, neo4j_client=client)
    idx = manager.create_knowledge()
    assert idx is not None
    # The index's private field carries the client through.
    assert idx._neo4j_client is client
