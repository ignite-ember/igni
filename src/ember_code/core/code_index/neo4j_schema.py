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
    "CREATE INDEX item_importer_count IF NOT EXISTS FOR (i:Item) ON (i.importer_count)",
    "CREATE INDEX item_method_count IF NOT EXISTS FOR (i:Item) ON (i.method_count)",
    "CREATE INDEX item_is_callable IF NOT EXISTS FOR (i:Item) ON (i.is_callable)",
    "CREATE INDEX item_sink_hits IF NOT EXISTS FOR (i:Item) ON (i.sink_hits)",
    "CREATE INDEX item_test_importers IF NOT EXISTS FOR (i:Item) ON (i.test_importer_count)",
    "CREATE INDEX item_member_count IF NOT EXISTS FOR (i:Item) ON (i.member_count)",
    "CREATE INDEX item_test_refs IF NOT EXISTS FOR (i:Item) ON (i.test_refs)",
    "CREATE INDEX item_empty_handlers IF NOT EXISTS FOR (i:Item) ON (i.empty_handlers)",
    "CREATE INDEX item_broad_handlers IF NOT EXISTS FOR (i:Item) ON (i.broad_handlers)",
    # Full-text (Lucene) over chunk text. This is the query type similarity
    # cannot do: it returns an empty result when the term is absent. Vector
    # search always returns its k nearest neighbours with confident-looking
    # scores — measured, "validates a JWT bearer token" scored 0.72 against a SQL
    # parser that has no auth code at all — so "is this really here?" needs term
    # matching, and without it the only honest answer was to leave the index and
    # grep the working tree.
    "CREATE FULLTEXT INDEX chunk_text IF NOT EXISTS FOR (c:Chunk) ON EACH [c.text]",
    "CREATE INDEX chunk_kind IF NOT EXISTS FOR (c:Chunk) ON (c.chunk_kind)",
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

**Two booleans that matter more than they look.** ``is_callable`` and
``is_type`` are normalised across languages, and you should filter on them rather
than on ``entity_type``. The raw node name is not reliable: in TypeScript most
functions are ``variable_declarator`` (``const f = (x) => {...}``), the same node
as ``const x = 5``. Measured — 3,507 of one repository's 4,563 entities are that
node type, and a query filtering ``entity_type`` by function node names found 12%
of its longest functions where ``WHERE i.is_callable`` finds 88%. The extractor
decides this from the parse tree, so ``const h = useMemo(() => f, [])`` is
correctly *not* callable.

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
| ``importer_count`` | — | always | — |
| ``error_handlers``, ``empty_handlers``, ``broad_handlers`` | — | always | **always** |
| ``member_count``, ``method_count`` | — | always 0 | **always** |
| ``is_callable``, ``is_type`` | — | false | **always** |
| ``sink_hits``, ``sink_kinds`` | — | always | **always** |
| ``test_importer_count``, ``sink_lines`` | — | always | — |

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
  ``fan_in`` (how many distinct items reference this one — use for blast radius:
  who breaks if this changes, at any granularity),
  ``importer_count`` (how many distinct *files* import this file — use for
  "which module does everything lean on". Files only, and deliberately separate
  from ``fan_in``: ``fan_in`` also counts entity-level references, so a module
  whose symbols are used everywhere outranks a package that is merely imported
  everywhere. Measured on pydantic, ``fan_in`` ranks ``_pydantic_core.pyi``
  first at 1,434 and ``pydantic/__init__.py`` seventh, while by importers
  ``pydantic/__init__.py`` is first at 201 — for an architecture question the
  second ranking is the right one),
  ``fan_out`` (how many distinct items it references — real coupling),
  ``test_refs`` (how many distinct test files are among the incoming references,
  so a high ``fan_in`` with ``test_refs`` 0 is "depended on, untested"),
  ``member_count`` (methods plus fields on a class-like entity; 0 for anything
  that is not a class, and 0 on every file),
  ``method_count`` (**methods only** — this is the god-class ranking.
  ``member_count`` counts fields too, and a record with forty fields and two
  methods is not a god class: ranking on it reached 73.7% of the god-class oracle
  where the question asks about behaviour),
  ``test_importer_count`` (test files that *import* this file — "depended upon
  but untested" is ``importer_count`` high with this at 0. Distinct from
  ``test_refs``, which counts any reference from a test and reached only 31% of
  that oracle),
  ``sink_hits`` / ``sink_kinds`` / ``sink_lines`` (counted dangerous sinks:
  deserialisation, dynamic evaluation and shell execution, per language and with
  line numbers. Yes, this duplicates what a grep would find — deliberately. A
  sink is a literal construct, and an index that cannot answer a literal question
  hands the work back to the filesystem. Counted, it is one ``ORDER BY``, and it
  joins against ``fan_in`` to ask which *load-bearing* module reaches a sink,
  which no grep can do).
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
- ``text`` — full-text indexed (index name ``chunk_text``)
- ``chunk_kind`` — ``'summary'`` or ``'code'``. **This is the important one.**
  A summary chunk is a model's prose about the item; a code chunk is the item's
  actual source. Ask the summary what something *does*, ask the code what it
  literally *says*.
- ``line_from``, ``line_to`` — absolute file lines, on code chunks only, so a
  hit is a place you can open rather than a file you then have to search
- ``chunk_index``, ``name``, ``type``, ``kind``, ``path``, ``file_extension``,
  ``repository_id``

**Three ways to search, and they fail differently.**

1. *By meaning* — ``CALL db.index.vector.queryNodes('chunk_embedding', 40,
   $query_vector)``. Pass ``semantic_query`` and the tool embeds it for you.
   Finds code you cannot name. **It can never return nothing**: it returns its k
   nearest neighbours whatever you ask, with scores that look the same either
   way — measured, "validates a JWT bearer token and checks its expiry" scored
   0.72 against a SQL parser containing no auth code at all, where real hits in
   the same graph scored 0.82. So a vector hit is a lead, never a confirmation.
2. *By term* — ``CALL db.index.fulltext.queryNodes('chunk_text', 'pickle OR
   eval')``. Lucene syntax. This one **does** return nothing when the term is
   absent, which makes it the only way to answer "is this really here?" Add
   ``WHERE c.chunk_kind = 'code'`` and you are searching the source, not a
   description of it.
3. *By structure or number* — a plain ``MATCH`` over :Item and :REL with the
   counted facts. Exact, and the only thing that can rank.

The intended shape of an investigation is 1 or 3 to find candidates, then 2 over
``chunk_kind = 'code'`` to confirm, reading ``line_from`` to say where. Do not
confirm a claim about the code against a summary: asking prose "what swallows
errors" scored 25% where ripgrep scored 57%, because a summary that reads
"handles failures gracefully" is exactly how a swallowed exception hides.

**Copy these.** Term search is the one most easily forgotten, and the one that
replaces leaving the index to grep the working tree:

```
// EXHAUSTIVE: which files contain a dangerous sink at all? Drop score entirely
// — keeping it defeats DISTINCT, because every chunk scores differently, and you
// get 25 near-duplicate chunks from four files instead of the files.
CALL db.index.fulltext.queryNodes('chunk_text', 'pickle OR eval OR exec OR subprocess')
YIELD node
WHERE node.chunk_kind = 'code'
RETURN DISTINCT split(node.path, '::')[0] AS file LIMIT 200
```

```
// RANKED: where is the strongest match, and on which line? Score is fine here
// because you want the top few places, not the set of files.
CALL db.index.fulltext.queryNodes('chunk_text', 'pickle.loads')
YIELD node, score
WHERE node.chunk_kind = 'code'
RETURN node.path AS path, node.line_from AS line, round(score,2) AS score
ORDER BY score DESC LIMIT 10
```

```
// Does this repository handle X at all? An empty result here means NO — the
// only search that can tell you that.
CALL db.index.fulltext.queryNodes('chunk_text', 'websocket')
YIELD node WHERE node.chunk_kind = 'code' RETURN count(node) AS hits
```

```
// Find by description, then confirm the literal, in two steps.
CALL db.index.vector.queryNodes('chunk_embedding', 25, $query_vector)
YIELD node, score
RETURN node.path AS path, node.line_from AS line, round(score,2) AS score
ORDER BY score DESC LIMIT 10
```

```
// Longest callables. Filter on is_callable, NOT on entity_type - see above.
MATCH (i:Item)
WHERE i.type = 'entity' AND i.is_callable AND i.line_to IS NOT NULL
RETURN i.path AS path, i.line_to - i.line_from AS lines
ORDER BY lines DESC LIMIT 40
```

```
// Types carrying the most behaviour. method_count, not member_count.
MATCH (i:Item) WHERE i.type = 'entity' AND i.method_count > 0
RETURN i.path AS path, i.method_count ORDER BY i.method_count DESC LIMIT 30
```

```
// Dangerous sinks, ranked, with the kind and the line. No grep needed.
MATCH (i:Item) WHERE i.sink_hits > 0 AND i.kind = 'code'
RETURN i.path AS path, i.sink_hits, i.sink_kinds, i.sink_lines
ORDER BY i.sink_hits DESC LIMIT 40
```

```
// Depended upon but untested.
MATCH (i:Item)
WHERE i.type = 'file' AND i.importer_count >= 3
  AND coalesce(i.test_importer_count, 0) = 0
RETURN i.path AS path, i.importer_count ORDER BY i.importer_count DESC LIMIT 30
```

```
// What breaks if this changes: everything importing it, one hop.
MATCH (dep:Item)-[r:REL {kind: 'imports'}]->(hub:Item)
WHERE hub.path ENDS WITH 'the/hub.py'
RETURN DISTINCT dep.path AS path LIMIT 200
```

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
