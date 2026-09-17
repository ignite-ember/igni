"""Unit tests for the Neo4j schema module.

These tests don't require a running Neo4j — they cover the
DB-name helpers and the integrity of the Cypher DDL constants.
Live-database behaviour is exercised by ``test_neo4j_integration.py``
(behind ``@pytest.mark.integration``).
"""

from __future__ import annotations

import re

from ember_code.core.code_index.neo4j_schema import (
    COMMIT_SCHEMA_STATEMENTS,
    DEFAULT_DATABASE,
    GRAPH_SCHEMA_DESCRIPTION,
    META_SCHEMA_STATEMENTS,
    created_indexes,
)


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


def test_no_statement_drops_an_index_the_ddl_then_creates() -> None:
    """The three ``DROP INDEX item_idx/chunk_idx/entry_idx`` lines could only
    ever drop the index the next statement rebuilt.

    ``apply_schema`` runs once per Neo4j process start, so the three hot-path
    lookups were torn down and re-populated on every start — while two
    docstrings promised an apply that "picks up where it left off". The
    ``DROP CONSTRAINT`` trio is fine: it is the one-way constraints→indexes
    migration and names things the DDL never creates.
    """
    created = {name for name, *_ in created_indexes()}
    dropped = {
        match.group(1)
        for stmt in (*COMMIT_SCHEMA_STATEMENTS, *META_SCHEMA_STATEMENTS)
        for match in [re.match(r"DROP INDEX (\w+)", stmt)]
        if match
    }
    assert not (created & dropped), (
        f"{sorted(created & dropped)} are dropped and re-created on every apply_schema()"
    )


class TestTheDocumentedIndexListMatchesTheDDL:
    """The "## Indexes" list is what an agent reads to pick a filter.

    Hand-written, it drifted to naming five indexes nothing created (two of
    them only ever DROPped) while omitting eighteen that existed, including
    every counted fact the same document says to ORDER BY.
    """

    def test_every_created_index_is_documented(self) -> None:
        missing = [
            name for name, *_ in created_indexes() if f"``{name}``" not in GRAPH_SCHEMA_DESCRIPTION
        ]
        assert not missing, missing

    def test_the_list_names_no_index_the_ddl_does_not_create(self) -> None:
        section = GRAPH_SCHEMA_DESCRIPTION.split("## Indexes")[1].split("## How AI")[0]
        listed = set(re.findall(r"^- ``(\w+)``", section, re.MULTILINE))
        assert listed - {name for name, *_ in created_indexes()} == set()

    def test_the_counted_facts_the_doc_ranks_on_are_listed(self) -> None:
        for name in ("item_fan_in", "item_method_count", "item_sink_hits", "chunk_text"):
            assert f"``{name}``" in GRAPH_SCHEMA_DESCRIPTION
