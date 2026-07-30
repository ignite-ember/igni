"""Cypher DDL for the Neo4j code_index + knowledge stores.

Tenancy model — single physical DB, per-``(project, commit)`` processes.

In the per-process design, **process IS the isolation boundary**:
one Neo4j process holds exactly one commit's data. Switching
commits = stopping the old process and starting a new one. The
old process's data lives on disk and can be re-loaded on demand;
data leakage between commits is impossible because the new
process simply doesn't have access to the old process's
in-memory state (and never queries the old process's DB at
all).

This means the data model is the **simplest possible**:

* :Item, :Chunk, :Entry — no ``commit_sha`` property. Each node
  is "owned" by the process it lives in.
* (:Item)-[:REL]->(:Item) — no ``commit_sha`` on the edge. Each
  reference edge is scoped to its process.
* :Commit — one per process, used for admin queries only
  ("what commits are tracked?"). The actual data isolation is
  the process boundary, not the node.
* No ``[:PRESENT_IN]`` edges. The commit-to-data relationship
  is implicit in which process is running.

Compare to the previous design (one physical DB, many
commits via property filters + ``[:PRESENT_IN]`` walks): the
relationship model was correct for that world. With per-process
isolation, the relationship model is belt-and-suspenders — the
process is the boundary, queries are direct ``MATCH`` on Item,
no edge walks, no ``r.commit_sha = $sha`` filters.

Vector index recall is tuned at index build time via
``vector.hnsw.m`` and ``vector.hnsw.ef_construction`` (the query-time
``ef`` knob isn't exposed on 5.26). To compensate, callers
over-fetch and re-rank client-side.
"""

from __future__ import annotations

# ── Single-DB schema ────────────────────────────────────────────────────


# Database name we use for everything. The default ``neo4j`` DB
# is created automatically by every Neo4j deployment, so we
# don't need (and on Community Edition, *can't*) issue
# ``CREATE DATABASE``. Single constant, single source of truth.
DEFAULT_DATABASE: str = "neo4j"


# Prefix for the per-project logical DB identifier. Every project
# is scoped by ``project_hash`` and conceptually lives in
# ``codeindex_<project_hash>``. The "DB" is a scope / label on
# the shared physical DB. On Enterprise Edition, this would map
# 1:1 to a ``:USE codeindex_<proj>`` session; on Community
# Edition, the ``project_hash`` property does the same job.
PROJECT_DB_PREFIX: str = "codeindex_"


def project_db_name(project_id: str) -> str:
    """The logical DB name for one project's scope.

    Used as a label / prefix in logs and the migration
    metadata. On Enterprise Edition, this would also be the
    real database name passed to ``session(database=...)``. On
    Community Edition, all projects share the default DB and
    this name is purely informational.
    """
    return f"{PROJECT_DB_PREFIX}{project_hash_short(project_id)}"


COMMIT_SCHEMA_STATEMENTS: tuple[str, ...] = (
    # ── Node constraints ────────────────────────────────────────
    # Community Edition: `IS NODE KEY` (composite) is Enterprise-only.
    # We use `IS UNIQUE` on the natural single-property key
    # (item_id / chunk_id / entry_id) and rely on the application
    # layer for (project_hash, key) uniqueness — both are `MERGE`-
    # style idempotent operations where the constraint is the
    # index lookup, not a hard fail.
    # NOTE: `IS UNIQUE` on item_id/chunk_id/entry_id alone conflicts
    # across projects (same UUID in different projects hits the constraint).
    # We use plain B-tree indexes instead; uniqueness per
    # (project_hash, id) is enforced at the MERGE layer.
    # Drop any pre-existing constraints first (index and constraint
    # share the name space; we migrated from constraints to indexes).
    # Use new index names to avoid "equivalent already exists" conflicts.
    "DROP CONSTRAINT item_pk IF EXISTS",
    "DROP CONSTRAINT chunk_pk IF EXISTS",
    "DROP CONSTRAINT entry_pk IF EXISTS",
    "DROP INDEX item_idx IF EXISTS",
    "DROP INDEX chunk_idx IF EXISTS",
    "DROP INDEX entry_idx IF EXISTS",
    "CREATE INDEX item_idx IF NOT EXISTS FOR (i:Item) ON (i.item_id)",
    "CREATE INDEX chunk_idx IF NOT EXISTS FOR (c:Chunk) ON (c.chunk_id)",
    "CREATE INDEX entry_idx IF NOT EXISTS FOR (e:Entry) ON (e.entry_id)",
    # ── Vector indexes ──────────────────────────────────────────
    "CREATE VECTOR INDEX chunk_embedding IF NOT EXISTS "
    "FOR (c:Chunk) ON (c.embedding) "
    "OPTIONS {indexConfig: {"
    "`vector.dimensions`: 384, "
    "`vector.similarity_function`: 'cosine', "
    "`vector.hnsw.m`: 32, "
    "`vector.hnsw.ef_construction`: 400"
    "}}",
    "CREATE VECTOR INDEX entry_embedding IF NOT EXISTS "
    "FOR (e:Entry) ON (e.embedding) "
    "OPTIONS {indexConfig: {"
    "`vector.dimensions`: 384, "
    "`vector.similarity_function`: 'cosine', "
    "`vector.hnsw.m`: 32, "
    "`vector.hnsw.ef_construction`: 400"
    "}}",
    # ── Item + chunk + edge property indexes ─────────────────
    "CREATE INDEX item_project IF NOT EXISTS FOR (i:Item) ON (i.project_hash)",
    "CREATE INDEX item_path IF NOT EXISTS FOR (i:Item) ON (i.path)",
    "CREATE INDEX item_type IF NOT EXISTS FOR (i:Item) ON (i.type)",
    "CREATE INDEX item_kind IF NOT EXISTS FOR (i:Item) ON (i.kind)",
    "CREATE INDEX item_parent IF NOT EXISTS FOR (i:Item) ON (i.parent_id)",
    "CREATE INDEX item_entity_type IF NOT EXISTS FOR (i:Item) ON (i.entity_type)",
    "CREATE INDEX item_archived IF NOT EXISTS FOR (i:Item) ON (i.archived)",
    "CREATE INDEX rel_kind IF NOT EXISTS FOR ()-[r:REL]-() ON (r.kind)",
    "CREATE INDEX entry_project IF NOT EXISTS FOR (e:Entry) ON (e.project_hash)",
    "CREATE INDEX entry_kind IF NOT EXISTS FOR (e:Entry) ON (e.kind)",
    "CREATE INDEX entry_source IF NOT EXISTS FOR (e:Entry) ON (e.source)",
)


# ── Meta-DB schema (per-project admin: head, branch pins, commits) ──
#
# The meta client owns project-level bookkeeping that must survive
# individual commit processes: the current HEAD sha, the set of
# branch-pinned commits (exempt from retention), and a :Commit node
# per tracked commit (for retention sweeps). These live under
# ``project_hash``-scoped nodes so a single physical DB can hold
# many projects' admin data without leakage.
META_SCHEMA_STATEMENTS: tuple[str, ...] = (
    "CREATE INDEX meta_project_key IF NOT EXISTS FOR (m:Meta) ON (m.project_hash, m.key)",
    "CREATE INDEX commit_project_sha IF NOT EXISTS FOR (c:Commit) ON (c.project_hash, c.sha)",
    "CREATE INDEX commit_last_used IF NOT EXISTS FOR (c:Commit) ON (c.last_used_at)",
)


# ── Helpers ──────────────────────────────────────────────────────────


def project_hash_short(project_id: str) -> str:
    """Truncate an already-hashed project id to 12 chars for log readability.

    ``resolve_project_id`` returns a 16-char SHA-256 prefix; this
    helper takes the first 12 so log lines that include the hash
    stay readable.
    """
    return project_id[:12]


# ── Graph schema description (for AI Cypher authors) ───────────────────


GRAPH_SCHEMA_DESCRIPTION: str = """\
# CodeIndex graph schema (Neo4j)

## Per-(project, commit) processes

**Process IS the isolation boundary.** Each ``(project, commit)``
pair runs in its own Neo4j process with its own data dir. A
query against a process can only see that process's data;
there's no cross-process leakage possible by construction.

This means the data model is the **simplest possible** — no
commit-scoping properties on items or edges, no
``[:PRESENT_IN]`` walks.

## Nodes

### :Item
- ``item_id`` (str, NODE KEY) — stable across commits
- ``project_hash`` (str) — 16-char SHA-256 prefix of the project
- ``name``, ``type``, ``kind``, ``entity_type``, ``parent_id``,
  ``file_extension``, ``repository_id``, ``path``, ``archived``,
  ``timestamp``, ``token_count``, ``line_from``, ``line_to``,
  ``needs_refactoring``
- ``content`` (str) — full content (for files) or summary (for entities)
- ``quality``, ``complexity``, ``security``, ``testing``,
  ``testability``, ``documentation``, ``performance``, ``issues``,
  ``maintainability``, ``architecture``, ``technical_debt``,
  ``cohesion``, ``coupling``, ``stability``, ``priority``
- ``vulnerabilities``, ``frameworks``, ``domain``, ``concerns``,
  ``layers``, ``patterns``, ``keywords``, ``file_issues`` (lists)
- ``meta`` (map) — per-item metadata (line ranges, etc.)

### :Chunk
- ``chunk_id`` (str, NODE KEY)
- ``parent_id`` (str) → :Item
- ``embedding`` (list[float], 384-dim) — vector-indexed
- ``text``, ``chunk_index``, ``name``, ``type``, ``kind``,
  ``path``, ``file_extension``, ``repository_id``

### :Entry (knowledge)
- ``entry_id`` (str, NODE KEY)
- ``project_hash`` (str)
- ``name``, ``source``, ``kind``, ``content``, ``created_at``
- arbitrary metadata fields (whatever the cloud provides)

### :Commit
- ``(project_hash, sha)`` (composite NODE KEY)
- ``created_at``, ``last_used_at``
- One per process. Admin queries only.

### :Meta
- ``(project_hash, key)`` (composite NODE KEY)
- ``value``

## Edges

### ``(:Item)-[:REL {kind, meta_json}]->(:Item)``
Reference edge (calls, imports, etc.). **Scoped to the active
process** — no ``commit_sha`` on the edge because each process
holds exactly one commit's edges. Carry-over copies parent
edges to the child process during ``set_head``.

### ``(:Entry)-[:HAS_CHUNK]->(:Chunk)``
Knowledge entry → its embedding chunks. Per-project, not
per-commit.

## Scoping rules

**Project scope:** always pass ``project_hash`` on every query
to scope to one project. (Multi-tenant queries without this
filter would leak across projects.)

**Commit scope:** handled by the process. You don't need to
filter by ``commit_sha`` in queries — the process only has
one commit's data. If you do query by ``commit_sha`` on a
``:Commit`` node, you're doing admin work, not data work.

## Indexes

- ``item_pk`` (NODE KEY on :Item.item_id) — the hot path lookup
- ``chunk_pk`` (NODE KEY on :Chunk.chunk_id)
- ``commit_pk`` (composite on :Commit) — admin queries
- ``meta_pk`` (composite on :Meta) — admin queries
- ``item_project`` (B-tree on :Item.project_hash) — cross-project
- ``commit_project_sha`` (B-tree on :Commit) — admin
- ``commit_last_used`` (B-tree on :Commit.last_used_at) — retention
- ``item_path``, ``item_type``, ``item_kind``, ``item_parent``,
  ``item_entity_type``, ``item_archived`` — typed filters
- ``rel_kind`` (B-tree on :REL.kind) — relation kind filter
- ``chunk_embedding`` / ``entry_embedding`` — vector indexes
  (384-dim cosine, HNSW)
- ``entry_project``, ``entry_kind``, ``entry_source``
- ``meta_project``

## How AI should write custom Cypher

The :class:`Neo4jClient` exposes
:meth:`Neo4jClient.execute_query` which takes a Cypher string
and a ``**params`` dict, returns a list of dict records. Use
this for any query that doesn't fit the typed methods.

Example::

    result = await client.execute_query(
        "MATCH (i:Item {project_hash: $proj}) "
        "WHERE i.security = 'critical' "
        "RETURN i",
        proj=client.project_id,
    )
    for record in result:
        print(record['i'])

Always pass ``project_hash`` to scope to one project. Use the
indexes for performance (filter on indexed properties first).
Do NOT try to scope by ``commit_sha`` — the process IS the
scope, and there's no commit-scoping property on items.
"""


def graph_schema() -> str:
    """Return the graph schema description as a string for AI prompts.

    Same content as :data:`GRAPH_SCHEMA_DESCRIPTION` but
    exposed as a callable so the agent tools can fetch it on
    demand. The description is the contract for custom Cypher
    — keep it in sync with the DDL above.
    """
    return GRAPH_SCHEMA_DESCRIPTION
