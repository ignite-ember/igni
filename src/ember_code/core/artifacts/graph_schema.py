"""The artifact graph, described for whoever has to write Cypher against it.

This text is read by a model, not a person, and that shapes it. It is
not a reference — a reference lists everything and lets the reader
decide what matters. This says which property answers which question,
and names the mistakes that produce an empty result that looks like a
real answer.

Two of those mistakes are worth calling out here because they cost
whole queries:

* Forgetting ``v.head``. Every artifact has every version it ever had.
  A query that does not filter returns the same document five times,
  four of them superseded, and the newest is not necessarily first.
* Filtering on ``project_hash``. It is an opaque hash, not a project
  name, so ``WHERE a.project_hash = 'my-repo'`` matches nothing and
  returns an empty result that looks like a real answer.

The rule that scoping is unnecessary rests on a fact worth writing
down, because it is the difference between a safe escape hatch and a
cross-project leak: :meth:`Neo4jRuntime.driver_for_artifacts` keys its
processes on ``(project_hash, "artifacts")`` and spawns one Neo4j
**process per project**. The database a query reaches contains that
project's documents and nothing else, so a bare ``MATCH (a:Artifact)``
is already scoped.

:class:`Neo4jArtifactClient` filters on ``project_hash`` anyway. That
is belt-and-braces over the process boundary, not the boundary itself
— and the asymmetry is worth knowing about: point two projects at one
database, as the test harness does, and every method on the client
stays scoped while a hand-written query here quietly returns the
other project's documents. Nothing in a deployment does that today.
"""

from __future__ import annotations

ARTIFACT_GRAPH_SCHEMA: str = """\
# Artifact graph schema (Neo4j)

An **artifact** is a document somebody authored: a design note, an
understanding of a subsystem, a decision record. It has one stable
identity and an append-only chain of immutable versions.

## Nodes

### :Artifact — the stable identity
- `artifact_id` (str) — stable across every revision
- `name` (str) — the human title; what people cite it by
- `kind` (str) — free-form label: `note`, `design`, `decision`, `file`, …
- `head_n` (int) — the current version number
- `created_at`, `updated_at` (datetime)
- `author` (str), `origin` (str — set only on imported copies)
- `project_hash` (str) — **do not filter on this.** It is an opaque
  hash, and the per-project process already scopes every query.

### :Version — one immutable revision
- `version_id` (str) — always `artifact_id + '::' + n`
- `artifact_id` (str), `n` (int) — the revision number, from 1
- `head` (bool) — **true for exactly one version per artifact.**
  Filter on it unless the question is explicitly about history.
- `content` (str) — the full text of that revision
- `content_hash` (str), `created_at` (datetime)
- `author` (str), `session_id` (str)
- `embedding` (384 floats) — indexed as `version_embedding`

### :Chunk — a slice of one version, for display
- `chunk_id`, `parent_id` (the version_id), `text`, `line_from`, `line_to`

### :Source — external material a version cited
- `source_id`, `url`, `kind` (`web` | `pdf` | `wiki` | `video` | `paper`), `title`

## Relationships

    (:Artifact)-[:HAS_VERSION]->(:Version)
    (:Version)-[:HAS_CHUNK]->(:Chunk)
    (:Version)-[:CITES]->(:Source)
    (:Version)-[:DERIVED_FROM {pinned_n, state, at}]->(:Version)

`DERIVED_FROM` is the one that carries the meaning. It points from a
version to **the exact version of the source it was written from**:

- `pinned_n` (int) — the source version number that was read
- `state` (str) — `confirmed` (the author cited it), `candidate`
  (captured automatically from what was retrieved, nobody has
  reviewed it), or `dismissed` (reviewed and rejected)

**Only `confirmed` edges mean anything about correctness.** A
`candidate` is a guess.

## The staleness question

"Which documents were built on something that has since changed" is
a comparison, not a stored flag:

    MATCH (dep:Version)-[d:DERIVED_FROM]->(src:Version)
    MATCH (sa:Artifact)-[:HAS_VERSION]->(src)
    MATCH (da:Artifact)-[:HAS_VERSION]->(dep)
    WHERE d.state = 'confirmed' AND dep.head
      AND d.pinned_n < sa.head_n
    RETURN da.name AS dependent, sa.name AS source,
           d.pinned_n AS pinned_to, sa.head_n AS now_at
    ORDER BY (sa.head_n - d.pinned_n) DESC
    LIMIT 50

## Worked examples

**Everything, most recently touched first**

    MATCH (a:Artifact)-[:HAS_VERSION]->(v:Version {head: true})
    RETURN a.name AS name, a.kind AS kind, a.head_n AS version,
           toString(a.updated_at) AS updated
    ORDER BY a.updated_at DESC LIMIT 50

**What a document was built on**

    MATCH (a:Artifact {name: 'Auth model'})-[:HAS_VERSION]->(v:Version {head: true})
    MATCH (v)-[d:DERIVED_FROM]->(sv:Version)<-[:HAS_VERSION]-(src:Artifact)
    WHERE d.state = 'confirmed'
    RETURN src.name AS source, d.pinned_n AS pinned_to, src.head_n AS now_at
    LIMIT 50

**What would be affected if a document changed** — directly

    MATCH (src:Artifact {name: 'Auth model'})-[:HAS_VERSION]->(sv:Version)
    MATCH (dv:Version)-[d:DERIVED_FROM]->(sv)
    MATCH (dep:Artifact)-[:HAS_VERSION]->(dv)
    WHERE dv.head AND d.state = 'confirmed'
    RETURN DISTINCT dep.name AS affected, dep.head_n AS version
    LIMIT 50

**Everything downstream of a document** — what a change could reach

One hop finds what cites the document; the pages built on *those* are
affected too, and no passage anywhere says so. A variable-length
match follows the chain. Start from every version of the source, not
just its head: a dependent pinned to an older version is still built
on it.

    MATCH (src:Artifact {name: 'Auth model'})-[:HAS_VERSION]->(:Version)
          <-[:DERIVED_FROM*1..10]-(v:Version)<-[:HAS_VERSION]-(dep:Artifact)
    WHERE v.head
    RETURN DISTINCT dep.name AS affected
    LIMIT 100

Reverse the arrow for **everything a document ultimately rests on**:

    MATCH (a:Artifact {name: 'Release plan'})-[:HAS_VERSION]->(v:Version {head: true})
          -[:DERIVED_FROM*1..10]->(:Version)<-[:HAS_VERSION]-(src:Artifact)
    RETURN DISTINCT src.name AS rests_on
    LIMIT 100

Results are printed 40 rows at a time. When the answer may be larger,
count first — `RETURN count(DISTINCT dep) AS n` — so a truncated list
is never reported as the whole answer. An empty traversal from a
document that has dependents is a wrong query, not an answer: check
the name and the arrow direction.

**Documents written from nothing** — no sources at all

    MATCH (a:Artifact)-[:HAS_VERSION]->(v:Version {head: true})
    WHERE NOT (v)-[:DERIVED_FROM]->()
    RETURN a.name AS unsourced
    LIMIT 100

**Documents nothing was built on — the leaves**

    MATCH (a:Artifact)-[:HAS_VERSION]->(v:Version {head: true})
    WHERE NOT EXISTS { (:Version)-[:DERIVED_FROM]->(v) }
    RETURN a.name AS name, a.kind AS kind LIMIT 50

**Full text of a specific old revision**

    MATCH (a:Artifact {name: 'Auth model'})-[:HAS_VERSION]->(v:Version {n: 1})
    RETURN v.content AS content LIMIT 1

**Unreviewed captured sources, oldest first**

    MATCH (v:Version)-[d:DERIVED_FROM]->(sv:Version)<-[:HAS_VERSION]-(src:Artifact)
    MATCH (a:Artifact)-[:HAS_VERSION]->(v)
    WHERE d.state = 'candidate' AND v.head
    RETURN a.name AS document, src.name AS possible_source, d.at AS captured
    ORDER BY d.at LIMIT 50

**Semantic search**, when the question is about meaning rather than
structure. Pass the text as `$semantic_query`; it is embedded and
bound as `$query_vector` before the query runs:

    CALL db.index.vector.queryNodes('version_embedding', 20, $query_vector)
    YIELD node, score
    WHERE node.head
    MATCH (a:Artifact)-[:HAS_VERSION]->(node)
    RETURN a.name AS name, node.content AS content, score
    ORDER BY score DESC LIMIT 10

## Rules

1. **Read-only.** `MATCH`, `OPTIONAL MATCH`, `WITH`, `WHERE`,
   `RETURN`, `ORDER BY`, `SKIP`, `LIMIT`, `UNWIND`, `UNION`. No
   `CREATE`, `MERGE`, `SET`, `DELETE`, `REMOVE`, no schema or admin
   commands. A write is rejected before it reaches the database.
2. **Always `LIMIT`.** The result is rendered in a panel, not piped
   into a file.
3. **Filter `head` unless the question is about history.** Otherwise
   one document appears once per revision it has ever had.
4. **Return named columns**, not whole nodes: `RETURN a.name AS name`
   rather than `RETURN a`. The panel renders columns.
5. **Never filter on `project_hash`.** One Neo4j process serves one
   project, so the database you are querying holds only this
   project's documents. The property is an opaque hash, and a filter
   on it returns nothing.
6. Return `artifact_id` alongside a name whenever the row is about a
   document — the panel turns it into a link to that document.
"""

# Kept separate from the schema so the planner prompt can put it last,
# where an instruction is most likely to be followed.
CYPHER_AUTHOR_RULES: str = """\
Answer with a single Cypher query and nothing else. No prose, no
explanation, no markdown fence. If the question cannot be answered
from this graph, answer with the single word: UNANSWERABLE
"""


# ── The DDL behind the description above ─────────────────────────────
#
# This lived in ``core/code_index/neo4j_schema.py`` — CodeIndex's
# schema module defining Artifacts' schema. It was put there because
# the per-project artifact store used to apply
# ``COMMIT_SCHEMA_STATEMENTS``: the code index's DDL, against a
# database with no :Item and no :Commit, building four dead indexes
# and dropping three constraints that were never there, on every
# attach. Giving Artifacts its own statements fixed that and left
# them in the wrong file.
#
# They are here now because both capabilities are being extracted,
# and a capability whose schema is defined inside another one cannot
# leave on its own.
#
# The artifact store is **per-project** (one process per project,
# shared across commits) — unlike the code index, which is
# per-(project, commit).
#
# Shape:
#
#   (:Artifact {artifact_id, head_n})
#     -[:HAS_VERSION]-> (:Version {n, content, head, embedding})
#       -[:HAS_CHUNK]-> (:Chunk)
#       -[:DERIVED_FROM {pinned_n, state}]-> (:Version)
#       -[:CITES]-> (:Source)
#
# ``head`` is denormalised onto :Version so the vector search can
# drop superseded versions without a join back to :Artifact —
# retrieval is the hot path and runs on every agent turn.
ARTIFACT_SCHEMA_STATEMENTS: tuple[str, ...] = (
    # ── Identity lookups ────────────────────────────────────────
    # Plain indexes, not constraints, for the same reason the code
    # index uses them: ``IS UNIQUE`` on a bare id collides across
    # projects sharing the physical DB. Uniqueness per
    # (project_hash, id) is enforced at the MERGE layer.
    "CREATE INDEX artifact_idx IF NOT EXISTS FOR (a:Artifact) ON (a.artifact_id)",
    "CREATE INDEX artifact_project IF NOT EXISTS FOR (a:Artifact) ON (a.project_hash)",
    "CREATE INDEX artifact_kind IF NOT EXISTS FOR (a:Artifact) ON (a.kind)",
    "CREATE INDEX version_idx IF NOT EXISTS FOR (v:Version) ON (v.version_id)",
    # The version lookup is always "this artifact, that n" — a
    # composite index so ``versions()`` and ``version(id, n)`` are
    # index seeks rather than a label scan filtered down.
    "CREATE INDEX version_artifact_n IF NOT EXISTS FOR (v:Version) ON (v.artifact_id, v.n)",
    # Every retrieval query filters on this; without the index the
    # head filter is a post-hoc scan over all versions ever written.
    "CREATE INDEX version_head IF NOT EXISTS FOR (v:Version) ON (v.head)",
    "CREATE INDEX version_project IF NOT EXISTS FOR (v:Version) ON (v.project_hash)",
    "CREATE INDEX version_hash IF NOT EXISTS FOR (v:Version) ON (v.content_hash)",
    "CREATE INDEX artifact_chunk_idx IF NOT EXISTS FOR (c:Chunk) ON (c.chunk_id)",
    "CREATE INDEX artifact_chunk_parent IF NOT EXISTS FOR (c:Chunk) ON (c.parent_id)",
    "CREATE INDEX source_idx IF NOT EXISTS FOR (s:Source) ON (s.source_id)",
    "CREATE INDEX source_project IF NOT EXISTS FOR (s:Source) ON (s.project_hash)",
    # ── Comments ────────────────────────────────────────────────
    # Comments are captured on the server, where the reader is, and
    # projected down here on sync. They live in the graph rather than
    # being fetched over the API each time so the whole history is
    # local: "which documents drew comments after their source moved"
    # is then one query alongside everything else, and it still
    # answers on a plane.
    "CREATE INDEX comment_idx IF NOT EXISTS FOR (c:Comment) ON (c.comment_id)",
    "CREATE INDEX comment_project IF NOT EXISTS FOR (c:Comment) ON (c.project_hash)",
    # The review queue is "what is still open", and it is the query
    # the local screen runs on every refresh.
    "CREATE INDEX comment_status IF NOT EXISTS FOR (c:Comment) ON (c.status)",
    "CREATE INDEX comment_thread IF NOT EXISTS FOR (c:Comment) ON (c.thread_id)",
    # ── Edge properties ─────────────────────────────────────────
    # The staleness query filters candidates out by state and
    # compares pinned_n, so both are read on every sweep.
    "CREATE INDEX derived_state IF NOT EXISTS FOR ()-[d:DERIVED_FROM]-() ON (d.state)",
    "CREATE INDEX derived_pinned IF NOT EXISTS FOR ()-[d:DERIVED_FROM]-() ON (d.pinned_n)",
    # Candidate pruning is "everything unconfirmed older than X".
    "CREATE INDEX derived_at IF NOT EXISTS FOR ()-[d:DERIVED_FROM]-() ON (d.at)",
    # ── Vector index ────────────────────────────────────────────
    # On :Version, not :Artifact — the embedding describes one
    # revision's text. Same 384 dims / cosine / HNSW tuning as the
    # code index, so the same over-fetch-and-rerank applies.
    # Folders are navigation only: a separate label from :Artifact so
    # they cannot turn up in search, lineage, drift or the vector
    # index, all of which key on that label.
    "CREATE INDEX folder_idx IF NOT EXISTS FOR (f:Folder) ON (f.folder_id)",
    "CREATE INDEX folder_project IF NOT EXISTS FOR (f:Folder) ON (f.project_hash)",
    "CREATE VECTOR INDEX version_embedding IF NOT EXISTS "
    "FOR (v:Version) ON (v.embedding) "
    "OPTIONS {indexConfig: {"
    "`vector.dimensions`: 384, "
    "`vector.similarity_function`: 'cosine', "
    "`vector.hnsw.m`: 32, "
    "`vector.hnsw.ef_construction`: 400"
    "}}",
    # On :Chunk as well as :Version, because "which paragraph of this
    # document came from which paragraph of its source" is a question
    # about passages, and a whole-document vector cannot answer it.
    # Same dims / metric / tuning, so the two are directly comparable.
    "CREATE VECTOR INDEX chunk_embedding IF NOT EXISTS "
    "FOR (c:Chunk) ON (c.embedding) "
    "OPTIONS {indexConfig: {"
    "`vector.dimensions`: 384, "
    "`vector.similarity_function`: 'cosine', "
    "`vector.hnsw.m`: 32, "
    "`vector.hnsw.ef_construction`: 400"
    "}}",
    # ── Full-text over names ────────────────────────────────────
    # Artifacts are looked up by name far more often than code is —
    # the agent cites "the auth design doc", not a hash.
    "CREATE FULLTEXT INDEX artifact_name_ft IF NOT EXISTS FOR (a:Artifact) ON EACH [a.name]",
)
