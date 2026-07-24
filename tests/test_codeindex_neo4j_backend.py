"""End-to-end test: CodeIndex routes file references through Neo4j.

Verifies the #3 wiring: when a ``Neo4jClient`` is injected at
construction, ``CodeIndex.file_reference_service()`` returns a
service backed by Neo4j; ``apply_delta`` writes through it; the
same service reads them back.

Live integration — requires ``NEO4J_TEST_URI``. Skipped otherwise.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest
from neo4j import AsyncGraphDatabase

from ember_code.core.code_index import CodeIndex
from ember_code.core.code_index.db.file_reference import FileReferenceService
from ember_code.core.code_index.neo4j_client import Neo4jClient

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


async def test_codeindex_routes_references_through_neo4j(tmp_path, driver, project_id):
    """Build a CodeIndex with a neo4j_client, apply a delta, read it back."""
    client = Neo4jClient(driver, project_id, commit_sha="a" * 40)
    await client.apply_schema()
    index = CodeIndex(project=tmp_path, data_dir=tmp_path / "data", neo4j_client=client)

    # The service must be the neo4j-backed one.
    svc = index.file_reference_service()
    assert isinstance(svc, FileReferenceService)
    assert svc._is_neo4j is True  # type: ignore[attr-defined]

    # Write a reference through the service, read it back.
    await svc.create(from_uuid="x", to_uuid="y", relation="calls", meta={"line": 3})
    fetched = await svc.get("x", "y", "calls")
    assert fetched is not None
    assert fetched.meta == {"line": 3}
    assert fetched.relation == "calls"

    edges = await svc.get_by_uuids(["x"])
    assert len(edges) == 1
    assert edges[0].meta == {"line": 3}


async def test_codeindex_without_neo4j_falls_back_to_sqlite(tmp_path):
    """The legacy SQLite path still works when no neo4j_client is injected."""
    index = CodeIndex(project=tmp_path, data_dir=tmp_path / "data")
    svc = index.file_reference_service()
    assert isinstance(svc, FileReferenceService)
    assert svc._is_neo4j is False  # type: ignore[attr-defined]

    await svc.create("a", "b", "calls", {})
    got = await svc.get("a", "b", "calls")
    assert got is not None
    assert got.relation == "calls"


async def test_apply_delta_persists_references_to_neo4j(tmp_path, driver, project_id):
    """``apply_delta`` with a neo4j-backed CodeIndex writes through Neo4j."""
    client = Neo4jClient(driver, project_id, commit_sha="b" * 40)
    await client.apply_schema()
    index = CodeIndex(project=tmp_path, data_dir=tmp_path / "data", neo4j_client=client)

    # Write a JSONL delta that adds one file item with a reference.
    jsonl = tmp_path / "delta.jsonl"
    jsonl.write_text(
        json.dumps(
            {
                "op": "commit",
                "sha": "c" * 40,
                "parent_sha": None,
                "branches": ["main"],
                "indexed_at": "2026-07-24T00:00:00+00:00",
            }
        )
        + "\n"
        + json.dumps(
            {
                "op": "upsert_item",
                "id": "f1",
                "type": "file",
                "name": "f1.py",
                "path": "f1.py",
                "content": "x",
            }
        )
        + "\n"
        + json.dumps(
            {
                "op": "upsert_reference",
                "from_id": "f1",
                "to_id": "f2",
                "relation": "imports",
                "meta": {"line": 1},
            }
        )
        + "\n"
    )

    await index.apply_delta(jsonl)

    # The reference must be readable through the same service.
    svc = index.file_reference_service()
    edges = await svc.get_by_uuids(["f1"])
    assert any(e.to_uuid == "f2" and e.relation == "imports" for e in edges)
