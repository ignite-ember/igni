"""Unit tests for the Neo4j schema module.

These tests don't require a running Neo4j — they cover the
DB-name helpers and the integrity of the Cypher DDL constants.
Live-database behaviour is exercised by ``test_neo4j_integration.py``
(behind ``@pytest.mark.integration``).
"""

from __future__ import annotations

from ember_code.core.code_index.neo4j_schema import (
    COMMIT_SCHEMA_STATEMENTS,
    DEFAULT_DATABASE,
    project_hash_short,
)


def test_project_hash_short_truncates_to_twelve() -> None:
    assert project_hash_short("abcdef1234567890") == "abcdef123456"
    assert project_hash_short("abc") == "abc"


def test_default_database_is_neo4j() -> None:
    """Single-DB design uses the default ``neo4j`` DB."""
    assert DEFAULT_DATABASE == "neo4j"


def test_commit_schema_statements_are_all_idempotent() -> None:
    """Every DDL statement uses ``IF NOT EXISTS`` (CREATE) or ``IF EXISTS`` (DROP) so re-apply is safe."""
    for stmt in COMMIT_SCHEMA_STATEMENTS:
        stmt_upper = stmt.upper()
        # DROP statements use IF EXISTS to be idempotent
        if stmt_upper.startswith("DROP"):
            assert "IF EXISTS" in stmt_upper, f"non-idempotent DROP: {stmt}"
        else:
            assert "IF NOT EXISTS" in stmt, f"non-idempotent: {stmt}"


def test_commit_schema_includes_vector_indexes() -> None:
    """Both Chunk and Entry need a vector index — search and knowledge."""
    joined = " ".join(COMMIT_SCHEMA_STATEMENTS)
    assert "CREATE VECTOR INDEX chunk_embedding" in joined
    assert "CREATE VECTOR INDEX entry_embedding" in joined
    assert "vector.dimensions`: 384" in joined
    assert "cosine" in joined


def test_commit_schema_indexes_edge_kind() -> None:
    """Edges use a single REL label with an indexed `kind` property."""
    joined = " ".join(COMMIT_SCHEMA_STATEMENTS)
    assert "FOR ()-[r:REL]-() ON (r.kind)" in joined
