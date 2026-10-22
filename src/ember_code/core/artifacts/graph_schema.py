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
