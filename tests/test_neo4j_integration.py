"""Integration tests for the simplified Neo4j data model.

These tests require a live Neo4j instance and are skipped by
default. Set ``NEO4J_TEST_URI`` (and optionally
``NEO4J_TEST_USER`` / ``NEO4J_TEST_PASSWORD``) to enable.

The data model in v0.10+ is **per-(project, commit) process**:
each process holds one commit's data; switching commits = kill
old process, start new. The data model is the simplest possible:

* :Item, :Chunk, :Entry — no ``commit_sha`` property
* `[:REL]` edges — no ``commit_sha``
* No `[:PRESENT_IN]` edges (process IS the scope)
* :Commit — one per process (admin queries only)

Single-DB design — all data lives in the default ``neo4j`` DB
on Community Edition.

Run locally::

    docker run -d --name neo4j-test -p 7687:7687 \\
        -e NEO4J_AUTH=neo4j/test neo4j:5.26
    export NEO4J_TEST_URI=bolt://127.0.0.1:7687
    export NEO4J_TEST_USER=neo4j
    export NEO4J_TEST_PASSWORD=test
    uv run pytest tests/test_neo4j_integration.py -v
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from neo4j import AsyncGraphDatabase

from ember_code.core.code_index.db.commit_metadata import CommitMetadataService
from ember_code.core.code_index.db.file_reference import FileReferenceService
from ember_code.core.code_index.enums import FileSystemType, Relation
from ember_code.core.code_index.neo4j_client import Neo4jClient, Neo4jMetaClient
from ember_code.core.code_index.schema.commit_metadata import (
    CommitMetadataBulkCreate,
    CommitMetadataBulkItem,
    CommitMetadataCreate,
)
from ember_code.core.code_index.schema.items import CodeIndexItem

pytestmark = pytest.mark.integration

_REQUIRED_ENV = "NEO4J_TEST_URI"

# NOTE: the "skip when NEO4J_TEST_URI is unset" gate lives in
# ``tests/conftest.py`` — pytest only honours
# ``pytest_collection_modifyitems`` from conftest/plugin scope, so a
# copy here would be silently ignored.


# ── Fixtures ────────────────────────────────────────────────────────────


def _auth() -> tuple[str, str]:
    return (
        os.environ.get("NEO4J_TEST_USER", "neo4j"),
        os.environ.get("NEO4J_TEST_PASSWORD", "test"),
    )


@pytest_asyncio.fixture
async def neo4j_driver():
    driver = AsyncGraphDatabase.driver(os.environ[_REQUIRED_ENV], auth=_auth())
    try:
        yield driver
    finally:
        await driver.close()


@pytest.fixture
def project_id() -> str:
    return uuid.uuid4().hex[:32]


@pytest.fixture
def commit_sha() -> str:
    return uuid.uuid4().hex[:12]


@pytest.fixture
def meta_client(neo4j_driver, project_id):
    if neo4j_driver is None:
        pytest.skip(f"{_REQUIRED_ENV} not set")
    return Neo4jMetaClient(neo4j_driver, project_id)


@pytest.fixture
def commit_client(neo4j_driver, project_id, commit_sha):
    if neo4j_driver is None:
        pytest.skip(f"{_REQUIRED_ENV} not set")
    return Neo4jClient(neo4j_driver, project_id, commit_sha)


async def _setup_schema(meta_client, commit_client):
    await meta_client.apply_schema()
    await commit_client.apply_schema()


async def _drop_project(driver, project_id: str):
    """Clean up every :Item, :Entry, :Meta, :Commit for this project."""
    async with driver.session() as session:
        await session.run(
            "MATCH (n {project_hash: $proj}) DETACH DELETE n",
            proj=project_id,
        )


@pytest_asyncio.fixture(autouse=True)
async def _clean_db(neo4j_driver):
    """Wipe the DB before each test — tests share a single Neo4j instance."""
    if neo4j_driver is None:
        yield
        return
    async with neo4j_driver.session() as session:
        await session.run("MATCH (n) DETACH DELETE n")
    yield


# ── Schema ──────────────────────────────────────────────────────────────


async def test_schema_apply_is_idempotent(neo4j_driver, project_id, meta_client) -> None:
    """Schema apply is idempotent — every DDL uses IF NOT EXISTS."""
    if neo4j_driver is None:
        pytest.skip(f"{_REQUIRED_ENV} not set")
    await meta_client.apply_schema()
    # Second apply must not error.
    await meta_client.apply_schema()
    await _drop_project(neo4j_driver, project_id)


# ── FileReferenceService ────────────────────────────────────────────────


async def test_file_reference_create_and_get(neo4j_driver, project_id, commit_client) -> None:
    if neo4j_driver is None:
        pytest.skip(f"{_REQUIRED_ENV} not set")
    meta = Neo4jMetaClient(neo4j_driver, project_id)
    await _setup_schema(meta, commit_client)
    svc = FileReferenceService(commit_client)
    ref = await svc.create(from_uuid="a", to_uuid="b", relation=Relation.CALLS, meta={"line": 5})
    assert ref.relation == "calls"
    fetched = await svc.get(from_uuid="a", to_uuid="b", relation="calls")
    assert fetched is not None
    assert fetched.meta == {"line": 5}
    await _drop_project(neo4j_driver, project_id)


async def test_file_reference_get_by_uuids(neo4j_driver, project_id, commit_client) -> None:
    if neo4j_driver is None:
        pytest.skip(f"{_REQUIRED_ENV} not set")
    meta = Neo4jMetaClient(neo4j_driver, project_id)
    await _setup_schema(meta, commit_client)
    svc = FileReferenceService(commit_client)
    await svc.create("a", "b", Relation.CALLS, {})
    await svc.create("a", "c", Relation.IMPORTS, {})
    await svc.create("b", "a", Relation.CALLED_BY, {})
    edges = await svc.get_by_uuids(["a"])
    assert len(edges) == 3
    await _drop_project(neo4j_driver, project_id)


async def test_file_reference_delete_pair(neo4j_driver, project_id, commit_client) -> None:
    if neo4j_driver is None:
        pytest.skip(f"{_REQUIRED_ENV} not set")
    meta = Neo4jMetaClient(neo4j_driver, project_id)
    await _setup_schema(meta, commit_client)
    svc = FileReferenceService(commit_client)
    await svc.create("a", "b", Relation.CALLS, {})
    await svc.create("a", "b", Relation.IMPORTS, {})
    await svc.delete("a", "b", relation="calls")
    assert await svc.get("a", "b", "calls") is None
    assert await svc.get("a", "b", "imports") is not None
    await _drop_project(neo4j_driver, project_id)


# ── CommitMetadataService ─────────────────────────────────────────────


async def test_commit_metadata_create_or_update_and_fetch(
    neo4j_driver, project_id, commit_client
) -> None:
    if neo4j_driver is None:
        pytest.skip(f"{_REQUIRED_ENV} not set")
    meta = Neo4jMetaClient(neo4j_driver, project_id)
    await _setup_schema(meta, commit_client)
    svc = CommitMetadataService(commit_client)
    await svc.create_or_update(
        CommitMetadataCreate(
            item_id="u1",
            commit_sha=commit_client.commit_sha,
            key="line_range",
            value={"from": 1, "to": 9},
        )
    )
    entries = await svc.get_by_items_and_commit(
        ["u1"], commit_sha=commit_client.commit_sha, key="line_range"
    )
    assert "u1" in entries
    assert entries["u1"].value == {"from": 1, "to": 9}
    await _drop_project(neo4j_driver, project_id)


async def test_commit_metadata_bulk_upsert(neo4j_driver, project_id, commit_client) -> None:
    if neo4j_driver is None:
        pytest.skip(f"{_REQUIRED_ENV} not set")
    meta = Neo4jMetaClient(neo4j_driver, project_id)
    await _setup_schema(meta, commit_client)
    svc = CommitMetadataService(commit_client)
    await svc.bulk_create_or_update(
        CommitMetadataBulkCreate(
            commit_sha=commit_client.commit_sha,
            key="quality",
            items=[
                CommitMetadataBulkItem(item_id="a", value={"score": 0.8}),
                CommitMetadataBulkItem(item_id="b", value={"score": 0.9}),
            ],
        )
    )
    entries = await svc.get_by_items_and_commit(
        ["a", "b"], commit_sha=commit_client.commit_sha, key="quality"
    )
    assert entries["a"].value == {"score": 0.8}
    assert entries["b"].value == {"score": 0.9}
    await _drop_project(neo4j_driver, project_id)


# ── Read-side queries ─────────────────────────────────────────────────


async def test_get_item(neo4j_driver, project_id, commit_client) -> None:
    if neo4j_driver is None:
        pytest.skip(f"{_REQUIRED_ENV} not set")
    meta = Neo4jMetaClient(neo4j_driver, project_id)
    await _setup_schema(meta, commit_client)
    item = CodeIndexItem(
        item_id="get-test",
        type=FileSystemType.FILE,
        name="get_test",
        path="get_test.py",
        content="x",
    )
    await commit_client.upsert_item(item, [])
    fetched = await commit_client.get_item("get-test")
    assert fetched is not None
    assert fetched.name == "get_test"
    assert fetched.content == "x"
    await _drop_project(neo4j_driver, project_id)


async def test_filter_items(neo4j_driver, project_id, commit_client) -> None:
    if neo4j_driver is None:
        pytest.skip(f"{_REQUIRED_ENV} not set")
    meta = Neo4jMetaClient(neo4j_driver, project_id)
    await _setup_schema(meta, commit_client)
    for item_id in ("a", "b", "c"):
        await commit_client.upsert_item(
            CodeIndexItem(
                item_id=item_id,
                type=FileSystemType.FILE,
                name=item_id,
                path=f"{item_id}.py",
                content="x",
            ),
            [],
        )
    results = await commit_client.filter_items(where={"type": "file"}, limit=10)
    assert len(results) == 3
    # Filter by ids
    results = await commit_client.filter_items(ids=["a", "c"], limit=10)
    assert sorted(r.item_id for r in results) == ["a", "c"]
    await _drop_project(neo4j_driver, project_id)


# ── Vector search round-trip ────────────────────────────────────────


async def test_vector_search_returns_indexed_chunk(neo4j_driver, project_id, commit_client) -> None:
    if neo4j_driver is None:
        pytest.skip(f"{_REQUIRED_ENV} not set")
    meta = Neo4jMetaClient(neo4j_driver, project_id)
    await _setup_schema(meta, commit_client)
    item = CodeIndexItem(
        item_id="vec-test",
        type=FileSystemType.FILE,
        name="vec.py",
        path="vec.py",
        content="def verify_password(p): return bcrypt.hashpw(p, salt)",
    )
    await commit_client.upsert_item(item, [("text", [0.1] * 384)])
    results = await commit_client.vector_search(embedding=[0.1] * 384, where=None, limit=10)
    assert len(results) == 1
    assert results[0].item_id == "vec-test"
    await _drop_project(neo4j_driver, project_id)


# ── Meta client (admin) ─────────────────────────────────────────────


async def test_meta_head_round_trip(neo4j_driver, project_id, meta_client) -> None:
    if neo4j_driver is None:
        pytest.skip(f"{_REQUIRED_ENV} not set")
    await meta_client.apply_schema()
    await meta_client.set_head("abc123def456")
    assert await meta_client.get_head() == "abc123def456"
    await _drop_project(neo4j_driver, project_id)


async def test_meta_branch_pins_round_trip(neo4j_driver, project_id, meta_client) -> None:
    if neo4j_driver is None:
        pytest.skip(f"{_REQUIRED_ENV} not set")
    await meta_client.apply_schema()
    await meta_client.set_branch_pins(["main", "develop"])
    assert await meta_client.get_branch_pins() == ["main", "develop"]
    await _drop_project(neo4j_driver, project_id)


async def test_touch_commit_creates_commit_node(neo4j_driver, project_id, meta_client) -> None:
    if neo4j_driver is None:
        pytest.skip(f"{_REQUIRED_ENV} not set")
    await meta_client.apply_schema()
    await meta_client.touch_commit("aaa111aaa111", now_iso="2026-07-19T00:00:00+00:00")
    async with neo4j_driver.session() as session:
        result = await session.run(
            "MATCH (c:Commit {project_hash: $proj, sha: $sha}) "
            "RETURN c.last_used_at AS ts, c.created_at AS created",
            proj=project_id,
            sha="aaa111aaa111",
        )
        records = (await result.to_eager_result()).records
    assert len(records) == 1
    assert records[0]["ts"] == "2026-07-19T00:00:00+00:00"
    assert records[0]["created"] is not None
    await _drop_project(neo4j_driver, project_id)


async def test_list_tracked_commits(neo4j_driver, project_id, meta_client) -> None:
    if neo4j_driver is None:
        pytest.skip(f"{_REQUIRED_ENV} not set")
    await meta_client.apply_schema()
    for sha in ("a1a1a1a1a1a1", "b2b2b2b2b2b2", "c3c3c3c3c3c3"):
        await meta_client.touch_commit(sha, now_iso="2026-07-19T00:00:00+00:00")
    tracked = await meta_client.list_tracked_commits()
    assert set(tracked) == {"a1a1a1a1a1a1", "b2b2b2b2b2b2", "c3c3c3c3c3c3"}
    await _drop_project(neo4j_driver, project_id)


# ── Drop commit (process exit) ─────────────────────────────────────


async def test_drop_commit_removes_all_data(neo4j_driver, project_id, commit_client) -> None:
    if neo4j_driver is None:
        pytest.skip(f"{_REQUIRED_ENV} not set")
    meta = Neo4jMetaClient(neo4j_driver, project_id)
    await _setup_schema(meta, commit_client)
    item = CodeIndexItem(
        item_id="x",
        type=FileSystemType.FILE,
        name="x.py",
        path="x.py",
        content="x",
    )
    await commit_client.upsert_item(item, [("t", [0.0] * 384)])
    svc = FileReferenceService(commit_client)
    await svc.create("x", "y", Relation.CALLS, {})
    # Drop the commit's data — every node and edge for this
    # (project, commit) pair is removed.
    await Neo4jClient.drop_database(neo4j_driver, project_id, commit_client.commit_sha)
    # Verify everything is gone.
    async with neo4j_driver.session() as session:
        result = await session.run(
            "MATCH (n {project_hash: $proj}) RETURN count(n) AS n",
            proj=project_id,
        )
        records = (await result.to_eager_result()).records
    assert records[0]["n"] == 0
    await _drop_project(neo4j_driver, project_id)


# ── Cutover idempotency (DB-free) ──────────────────────────────────


async def test_cutover_is_idempotent(neo4j_driver, project_id, tmp_path) -> None:
    """``maybe_cutover`` runs once and is a no-op on subsequent calls."""
    if neo4j_driver is None:
        pytest.skip(f"{_REQUIRED_ENV} not set")
    from ember_code.core.code_index.cutover import maybe_cutover
    from ember_code.core.code_index.paths import code_index_dir

    project = tmp_path / "proj"
    project.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    chroma = code_index_dir(project, data_dir=data_dir) / "deadbeef.chroma"
    chroma.mkdir(parents=True)
    (chroma / "chroma.sqlite3").write_text("fake")
    manifest = code_index_dir(project, data_dir=data_dir) / "manifest.json"
    manifest.write_text("{}")

    first = await maybe_cutover(project=project, data_dir=data_dir)
    assert first is True
    assert not chroma.exists()
    assert not manifest.exists()

    second = await maybe_cutover(project=project, data_dir=data_dir)
    assert second is False
    await _drop_project(neo4j_driver, project_id)


# ── Raw Cypher escape hatch ──────────────────────────────────────


async def test_execute_query_returns_dict_records(neo4j_driver, project_id, commit_client) -> None:
    """``execute_query`` runs arbitrary Cypher and returns row dicts.

    The escape hatch for AI / callers that want to write
    custom Cypher against the data model.
    """
    if neo4j_driver is None:
        pytest.skip(f"{_REQUIRED_ENV} not set")
    meta = Neo4jMetaClient(neo4j_driver, project_id)
    await _setup_schema(meta, commit_client)
    await commit_client.upsert_item(
        CodeIndexItem(
            item_id="query-test",
            type=FileSystemType.FILE,
            name="query_test",
            path="query_test.py",
            content="x",
        ),
        [],
    )
    result = await commit_client.execute_query(
        "MATCH (i:Item) WHERE i.item_id = $eid RETURN i.name AS name",
        eid="query-test",
    )
    assert len(result) == 1
    assert result[0]["name"] == "query_test"
    await _drop_project(neo4j_driver, project_id)


# ── Schema description contract ─────────────────────────────────


def test_graph_schema_description_is_exposed(neo4j_driver) -> None:
    """The schema description is the contract for custom Cypher.

    AI reads this before writing queries — it must mention every
    node type and the scoping rules. Test it parses without
    crashing and contains the key surface.
    """
    if neo4j_driver is None:
        pytest.skip(f"{_REQUIRED_ENV} not set")
    from ember_code.core.code_index.neo4j_schema import (
        GRAPH_SCHEMA_DESCRIPTION,
        graph_schema,
    )

    for label in (":Item", ":Chunk", ":Entry", ":Meta", ":Commit"):
        assert label in GRAPH_SCHEMA_DESCRIPTION
    for edge in ("[:REL {kind, meta_json}]", "[:HAS_CHUNK]"):
        assert edge in GRAPH_SCHEMA_DESCRIPTION
    # Process IS the scope — the schema description should say so.
    assert "process" in GRAPH_SCHEMA_DESCRIPTION.lower()
    assert "scope" in GRAPH_SCHEMA_DESCRIPTION.lower()
    # The graph_schema() helper returns the same string.
    assert graph_schema() == GRAPH_SCHEMA_DESCRIPTION
