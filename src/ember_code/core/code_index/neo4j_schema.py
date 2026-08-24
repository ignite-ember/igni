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
    # Counted structural and failure-handling facts. Indexed because they exist
    # to be *ranked* — "which load-bearing file swallows the most failures" is an
    # ORDER BY over these — and an unindexed property degrades to a full label
    # scan that grows with the repository.
    "CREATE INDEX item_fan_in IF NOT EXISTS FOR (i:Item) ON (i.fan_in)",
    "CREATE INDEX item_fan_out IF NOT EXISTS FOR (i:Item) ON (i.fan_out)",
    "CREATE INDEX item_member_count IF NOT EXISTS FOR (i:Item) ON (i.member_count)",
    "CREATE INDEX item_test_refs IF NOT EXISTS FOR (i:Item) ON (i.test_refs)",
    "CREATE INDEX item_empty_handlers IF NOT EXISTS FOR (i:Item) ON (i.empty_handlers)",
    "CREATE INDEX item_broad_handlers IF NOT EXISTS FOR (i:Item) ON (i.broad_handlers)",
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
- ``project_hash`` (str) — 16-char SHA-256 prefix of the project. **Do not
  filter on it.** It is an opaque hash, not the repository's name, and the
  per-(project, commit) process already scopes every query — a
  ``WHERE i.project_hash STARTS WITH 'myrepo'`` matches nothing and returns an
  empty result that looks like a real answer. Measured: agents wasted queries on
  exactly this in 3 of 12 evaluation runs.
- ``name``, ``type``, ``kind``, ``entity_type``, ``parent_id``,
  ``file_extension``, ``repository_id``, ``path``, ``archived``,
  ``timestamp``, ``token_count``, ``line_from``, ``line_to``
- ``content`` (str) — full content (for files) or summary (for entities)

**Which item type carries which property.** ``type`` is ``folder``, ``file`` or
``entity``, and most analysis properties exist at only one or two of those
levels. Filtering an entity on a folder-only property returns nothing, which
reads exactly like "there is nothing wrong here" — measured, agents spent 13% of
their queries (296 of 2,255) on properties that are always null at the level they
asked about, ``coupling`` alone 164 times.

Percentages are how many items of that type carry the property, counted over the
86,332 items (771 folders, 5,093 files, 80,468 entities) indexed so far. The
counted facts read ``always``: they are emitted unconditionally for every item of
that type, because they are counted rather than generated and so cannot go
missing.

**A file is either code or a document, and only code is analysed.** ``kind`` is
``'code'`` (4,014 files) or ``'docs'`` (1,079 — every ``.md`` and ``.rst`` in the
corpus). Measured: every analysis property is set on **100%** of code files and
**0%** of docs files, with no failures in between. So the file column below is
"100% of code files", and ``quality IS NULL`` on a file means "this is a
document", not "this file looks clean". Add ``kind = 'code'`` to any query that
filters or ranks on an analysis property.

Two consequences worth knowing. A docs item's ``content`` is the **raw document
text**, not a summary, so ``content CONTAINS`` on a docs file matches the real
document while on a code file it matches a written description of the code. And
entities under a docs file are its *sections*, not code — a heading, with the
section body as content.

| property | folder | file (``kind='code'``) | entity |
| - | - | - | - |
| ``quality``, ``complexity``, ``security``, ``testability`` | 100% | 100% | **93%** |
| ``domain`` | 100% | 100% | **93%** |
| ``documentation``, ``issues`` | — | — | **93%** |
| ``performance`` | — | 100% | **93%** |
| ``concerns`` | 98% | — | **80%** |
| ``architecture``, ``technical_debt``, ``priority``, ``needs_refactoring`` | 100% | 100% | — |
| ``maintainability`` | — | 100% | — |
| ``cohesion``, ``coupling``, ``stability``, ``layers`` | 100% | — | — |
| ``frameworks`` | — | 89% | — |
| ``file_issues`` | — | 71% | — |
| ``vulnerabilities`` | — | 10% | — |
| ``fan_in``, ``fan_out``, ``test_refs`` | — | always | **always** |
| ``error_handlers``, ``empty_handlers``, ``broad_handlers`` | — | always | **always** |
| ``member_count`` | — | always 0 | **always** |

So: ``coupling`` and ``cohesion`` are folder-only, ``maintainability`` is
file-only, and ``technical_debt`` and ``needs_refactoring`` stop at the file. An
entity-level question about coupling, hotspots or god classes belongs on the
counted facts — they are numbers, they exist on every file and entity, and they
are what those properties were approximating.

Two cautions where they *are* set. They are absolute rather than relative to the
repository, and they cluster hard — measured over the 75,069 analysed entities:

| property | dominant value | share |
| - | - | - |
| ``security`` | ``secure`` | 93% |
| ``complexity`` | ``low`` | 91% |
| ``testability`` | ``easy`` | 83% |
| ``issues`` | ``minor`` | 78% |
| ``documentation`` | ``minimal`` | 72% |
| ``quality`` | ``good`` | 67% |
| ``performance`` | ``optimized`` | 58% |

``WHERE complexity = 'low'`` therefore selects 91% of the repository and
``WHERE security <> 'secure'`` selects 7% of it regardless of how the code
actually looks. Rank on a counted fact, then use these to explain what came
back — they are worth reading on a specific item and worth little as a filter.

**The counted facts.** Everything above is a model's judgement. Everything below
is counted from the parse tree and the reference graph and is stable across runs
— this is what to rank and filter on.

- **Counted structure** — exact numbers, no model involved, present on files and
  entities, meant for ``ORDER BY``. All three reference counts are of *distinct
  items*, not of edges: an entity calling one helper forty times is coupled to
  one thing.
  ``fan_in`` (how many distinct items reference this one — the load-bearing
  ranking), ``fan_out`` (how many distinct items it references — real coupling),
  ``test_refs`` (how many distinct test files are among the incoming references,
  so a high ``fan_in`` with ``test_refs`` 0 is "depended on, untested"),
  ``member_count`` (methods plus fields on a class-like entity; 0 for anything
  that is not a class, and 0 on every file — the god-class ranking).
- **Counted failure handling** — exact numbers from the parse tree, not model
  judgement, so they can be ranked and joined:
  ``error_handlers`` (how many catch/except/rescue blocks),
  ``empty_handlers`` (how many do nothing at all),
  ``broad_handlers`` (how many catch everything — bare ``except``,
  ``catch (...)``, ``Exception``/``Throwable``),
  ``swallow_lines`` (list[int], the lines the empty ones start on).
  Zero for Go and Rust, which have no catch construct — that is a fact about the
  language, not about the code. Prefer these over searching ``content`` for
  words like "except": ``content`` is a written summary of what the code does,
  not the code.
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
