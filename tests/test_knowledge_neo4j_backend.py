"""End-to-end test: Neo4jKnowledgeClient round-trips a knowledge entry.

Verifies the knowledge migration: an entry's content + chunks
land in neo4j (:Entry + :Chunk + vector index), search returns
the right chunks, and delete cleans up. Live integration —
requires ``NEO4J_TEST_URI``. Skipped otherwise.
"""

from __future__ import annotations

import os
import uuid

import pytest
from neo4j import AsyncGraphDatabase

from ember_code.core.code_index.embedder import HashEmbedder
from ember_code.core.code_index.neo4j_client import Neo4jKnowledgeClient

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


async def test_knowledge_add_search_list_delete_round_trip(tmp_path, driver, project_id):
    """Add an entry, search by embedding, list entries, delete, verify gone."""
    client = Neo4jKnowledgeClient(driver, project_id)
    await client.apply_schema()
    embedder = HashEmbedder()

    # Three entries with distinctive content. Each chunk is the
    # entry's own content (so the hash embedder gives each a
    # distinct, reproducible vector).
    entries = {
        "auth": "Passwords must be hashed with bcrypt; never store plaintext.",
        "deploy": "Deploy via the GitHub Actions workflow; never push to main directly.",
        "config": "Configuration is loaded from .ember/config.local.yaml on startup.",
    }
    for eid, content in entries.items():
        # One embedding per entry (matches the entry_embedding
        # vector index). One chunk per entry for this test.
        emb = embedder.embed([content])[0]
        await client.add_entry(
            entry_id=eid,
            name=eid.title(),
            source="test",
            content=content,
            embedding=emb,
            chunks=[(content, 0, 0)],
        )

    # count: three entries
    assert await client.count() == 3

    # list_entries: every entry visible
    listed = await client.list_entries()
    listed_ids = {e["entry_id"] for e in listed}
    assert listed_ids == set(entries)

    # has_entry: each id present
    for eid in entries:
        assert await client.has_entry(eid) is True
    assert await client.has_entry("does-not-exist") is False

    # search: query with the same vector as the "deploy" entry
    # should return that entry as the top hit.
    query = entries["deploy"]
    query_vec = embedder.embed([query])[0]
    results = await client.search(query_embedding=query_vec, limit=5)
    assert len(results) >= 1
    assert results[0]["entry_id"] == "deploy"

    # delete: gone after deletion
    assert await client.delete_entry("auth") is True
    assert await client.has_entry("auth") is False
    assert await client.count() == 2


async def test_knowledge_re_upsert_replaces_chunks(tmp_path, driver, project_id):
    """Adding the same entry_id twice replaces its chunk set, not appends."""
    client = Neo4jKnowledgeClient(driver, project_id)
    await client.apply_schema()
    embedder = HashEmbedder()

    content_v1 = "first version of this entry"
    content_v2 = "second version of this entry — different content"

    await client.add_entry(
        entry_id="dup",
        name="dup",
        source="",
        content=content_v1,
        embedding=embedder.embed([content_v1])[0],
        chunks=[(content_v1, 0, 0)],
    )
    # :Chunk count for this entry should be 1.
    async with driver.session() as s:
        n = await s.run(
            "MATCH (:Entry {entry_id: $eid})-[:HAS_CHUNK]->(c:Chunk) RETURN count(c) AS n",
            eid="dup",
        )
        rec = await n.single()
        assert rec["n"] == 1

    # Re-upsert with different content → still 1 chunk, but the
    # vector index now points to the new embedding.
    await client.add_entry(
        entry_id="dup",
        name="dup",
        source="",
        content=content_v2,
        embedding=embedder.embed([content_v2])[0],
        chunks=[(content_v2, 0, 0)],
    )
    async with driver.session() as s:
        n = await s.run(
            "MATCH (:Entry {entry_id: $eid})-[:HAS_CHUNK]->(c:Chunk) RETURN count(c) AS n",
            eid="dup",
        )
        rec = await n.single()
        assert rec["n"] == 1
