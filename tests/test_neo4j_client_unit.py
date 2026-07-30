"""Unit tests for Neo4jClient that don't need a live database.

Focused on the pure-logic seams:

* The chroma-style ``where`` → Cypher WHERE fragment translator.
* DB name resolution for the active client.
* Driver guard (constructor + driver lookup).
* The class-level constant bag (:class:`Neo4jChunkSearch`).

Live-database behaviour (vector search round-trip, edge upsert,
item lifecycle) is exercised by ``test_neo4j_integration.py``
behind ``@pytest.mark.integration``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ember_code.core.code_index.neo4j_client import Neo4jChunkSearch, Neo4jClient


def _client() -> Neo4jClient:
    """Build a Neo4jClient with a mock driver — no network I/O happens."""
    return Neo4jClient(driver=MagicMock(), project_id="abc123def456")


def test_database_name_format() -> None:
    """Single-DB design — every client uses the default ``neo4j`` DB.

    Per-commit isolation is via ``[:PRESENT_IN]`` edges to
    ``:Commit`` nodes, not per-DB naming. Branch switching is a
    Cypher traversal, not a ``:USE`` switch.
    """
    from ember_code.core.code_index.neo4j_schema import DEFAULT_DATABASE

    c = _client()
    assert c.database_name == DEFAULT_DATABASE
    assert c.database_name == "neo4j"


def test_project_and_commit_accessors() -> None:
    c = _client()
    assert c.project_id == "abc123def456"


# ── Where-clause renderer ────────────────────────────────────────────


def test_render_where_handles_bare_value_as_eq() -> None:
    """A bare value in the where dict is a `=` (chroma's ``$eq`` default)."""
    clause, params = Neo4jClient._render_where({"name": "verify_password"})
    assert "i.name = $w0_eq" in clause
    assert params["w0_eq"] == "verify_password"


def test_render_where_handles_explicit_eq() -> None:
    clause, params = Neo4jClient._render_where({"type": {"$eq": "entity"}})
    assert "i.type = $w0_eq" in clause
    assert params["w0_eq"] == "entity"


def test_render_where_handles_in() -> None:
    clause, params = Neo4jClient._render_where({"type": {"$in": ["entity", "file"]}})
    assert "i.type IN $w0_in" in clause
    assert params["w0_in"] == ["entity", "file"]


def test_render_where_handles_nin() -> None:
    clause, params = Neo4jClient._render_where({"type": {"$nin": ["folder"]}})
    assert "NOT i.type IN $w0_nin" in clause
    assert params["w0_nin"] == ["folder"]


def test_render_where_handles_ne() -> None:
    clause, params = Neo4jClient._render_where({"security": {"$ne": "critical"}})
    assert "<> $w0_ne" in clause
    assert params["w0_ne"] == "critical"


def test_render_where_handles_gt_gte_lt_lte() -> None:
    for op, fragment in [
        ("$gt", ">"),
        ("$gte", ">="),
        ("$lt", "<"),
        ("$lte", "<="),
    ]:
        clause, params = Neo4jClient._render_where({"token_count": {op: 100}})
        assert f"i.token_count {fragment} $w0_{op[1:]}" in clause
        assert params[f"w0_{op[1:]}"] == 100


def test_render_where_handles_contains_with_list_membership() -> None:
    """``$contains`` on a multi-value list field uses ``ANY(... = x)``."""
    clause, params = Neo4jClient._render_where({"vulnerabilities": {"$contains": "sql-injection"}})
    assert "ANY(x IN coalesce(i.vulnerabilities, [])" in clause
    assert params["w0_c"] == "sql-injection"


def test_render_where_handles_multiple_fields() -> None:
    clause, params = Neo4jClient._render_where({"security": "critical", "type": "entity"})
    # Both fields rendered; AND-joined.
    assert "i.security = $w0_eq" in clause
    assert "i.type = $w1_eq" in clause
    assert " AND " in clause
    assert params["w0_eq"] == "critical"
    assert params["w1_eq"] == "entity"


def test_render_where_rejects_unknown_operator() -> None:
    # Use a field that IS on the :Item allowlist so the operator check
    # (not the field allowlist) is the one that fires. The renderer
    # rejects both unknown fields and unknown operators; the messages
    # are distinct so callers can tell them apart.
    with pytest.raises(ValueError, match="unsupported where operator"):
        Neo4jClient._render_where({"type": {"$regex": ".*"}})


def test_render_where_empty_dict_returns_empty_clause() -> None:
    """No filter → caller gets ``""`` and an empty params dict.

    The vector-search caller checks for the empty string and
    short-circuits to the un-filtered query path."""
    clause, params = Neo4jClient._render_where({})
    assert clause == ""
    assert params == {}


# ── Constant bag ─────────────────────────────────────────────────────


def test_chunk_search_constants_match_chroma_era() -> None:
    """The constants survived the chroma → Neo4j migration."""
    assert Neo4jChunkSearch.PREVIEW_MAX_CHARS == 1000
    assert Neo4jChunkSearch.PARENT_ID_CAP == 10_000
