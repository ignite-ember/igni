# Design: versioned artifacts with derivation lineage

> **STATUS — PROPOSED.** Nothing below is built. This is the design record for
> replacing the knowledge index with a versioned artifact store that tracks
> which version of which artifact each artifact was built on, detects when a
> source has moved underneath its dependents, and renders/shares the result.

Spans three repos: the store and renderer live in `ember-code`, the shared-doc
resource lives in `ember-server`, the group-entry copy lives in the portal.

---

## The gap

The knowledge index today is two node types and one edge
(`core/code_index/neo4j_client.py:1161`):

```
(:Entry {entry_id, project_hash, name, source, content, kind, embedding, meta})
  -[:HAS_CHUNK]-> (:Chunk {chunk_id, parent_id, text, line_from, line_to})
```

Entries never point at each other. `add_entry` is idempotent on `entry_id`, so
re-writing an entry replaces content, embedding and chunks — the previous
state is gone. `ArtifactProvenance` (`core/knowledge/models.py:505`) carries a
single `added_at` timestamp. `source` is a free-text string.

Three consequences:

1. You cannot address v1 of a document, so you cannot move between versions.
2. You cannot say "this was built on v1 of that", so when the source changes
   nothing knows which dependents are now wrong.
3. Nothing records what the agent was reading when it wrote a new artifact.

### Prior art: vector-bridge

`vector-bridge` solved half of this. Its Weaviate `Documents` collection
(`app/db/vectordb/models/documents.py`) had bidirectional derivation:
`source_documents` ↔ `derived_documents` ("the documents used to generate this
file" / "the documents generated from this file"), plus
`document_references` ↔ `referenced_by` for hand-made links, and a full ACL.

What it lacked is exactly what we need now:

- **Edges pointed at documents, not versions.** "Built on that doc" — never
  "built on v1 of that doc."
- **No addressable versions.** The `History` table
  (`app/db/sql/models/history.py`) is a field-level audit log — `change_type`,
  `field_name`, `old_value`, `new_value`, `changed_by`. Replayable, not
  addressable.
- **No staleness propagation.** Nothing detected a moved source.
- **Dual-write.** Weaviate has no undirected traversal, so both reference
  directions were written by hand on create and update
  (`files_manager.py:125`, `:244`) and unwound on delete (`:785`). That is a
  standing consistency bug we do not have to inherit — Neo4j traverses one
  edge both ways.

---

## Model

One decision carries the whole design: **the derivation edge pins a version.**

```
(:Artifact {artifact_id, project_hash, name, kind, head_n, created_by, created_at})
   -[:HAS_VERSION]-> (:Version {version_id, artifact_id, n, content, content_hash,
                                created_at, session_id, head, embedding})
(:Version)-[:DERIVED_FROM {pinned_n, at, state, session_id}]->(:Version)
(:Version)-[:HAS_CHUNK]->(:Chunk {chunk_id, parent_id, text, line_from, line_to})
(:Version)-[:CITES {url}]->(:Source {source_id, url, kind, fetched_at})
```

- `:Artifact` is the stable identity a human names and links to. `:Version` is
  immutable once written.
- A write appends `n+1` and moves `head_n`; the old version keeps its content.
  **Overwriting stays the mental model** — history is a side effect, not a
  thing the user manages.
- `head` is denormalised onto `:Version` so the vector index can filter without
  a join. Exactly one version per artifact carries `head: true`.
- `:Source` replaces today's free-text `source` string for external material
  (the URL/PDF a `WebsiteReader`/`PDFReader` produced the artifact from).

### Search hits head only

The vector index moves from `Entry.embedding` to `Version.embedding`, and
every retrieval query adds `WHERE v.head`. Old versions stay addressable by
`(artifact_id, n)` but never surface in search — otherwise the agent retrieves
superseded text and the whole point is lost.

Chunks hang off the version, not the artifact, so `ChunkResultRollup`
(`core/knowledge/rollup.py`) rolls chunk hits up to the head version rather
than to a parent document. Its shape is unchanged; the parent lookup changes
target.

### Edge lifecycle: capture then confirm

`DERIVED_FROM.state` is `candidate` or `confirmed`.

- **Capture.** Every artifact version the agent retrieved during the session is
  recorded as a `candidate` edge from the version it then wrote, pinned to the
  `n` that was actually read. The retrieval path already knows the ids it
  returned — no model cooperation required, so nothing is silently lost.
- **Confirm.** An edge becomes `confirmed` when the agent cites it explicitly
  in the write, or when you confirm it in the artifact view.
- **Only `confirmed` edges drive staleness.** Candidates are visible in the
  lineage view and reviewable, but they do not raise alarms — a version skimmed
  once is not a dependency, and treating it as one makes the staleness signal
  worthless.
- Candidates older than the retention window and never confirmed are pruned.

### Staleness is a query, not a job

```cypher
MATCH (dep:Version)-[d:DERIVED_FROM]->(src:Version)<-[:HAS_VERSION]-(a:Artifact)
WHERE d.state = 'confirmed' AND dep.head AND d.pinned_n < a.head_n
RETURN dep.artifact_id, a.artifact_id AS source, d.pinned_n, a.head_n
ORDER BY a.head_n - d.pinned_n DESC
LIMIT 100
```

No background worker, no invalidation flags to keep in sync, no write
amplification when a popular source moves. The answer is computed when asked.

### Reconciliation

Because the edge kept `pinned_n` *and* the artifact has `head_n`, we can diff
the source from the pinned version to head and hand **that diff** to the agent
to patch each stale dependent — rather than asking it to re-derive from
scratch. This is the step vector-bridge could not do at all.

The loop proposes, it does not auto-write: each stale dependent gets a
suggested new version shown as a diff, and accepting it appends `n+1` and
re-pins the edge to the new source head.

### Project-local only

Edges live inside one project's Neo4j DB, which is how the knowledge index is
already scoped — `Neo4jRuntime.driver_for_artifacts(project_hash)` spawns one
process per project, shared across commits, distinct from the per-commit code
index DB (`driver_for(project_hash, commit_sha)`).

An artifact that arrives from elsewhere — a teammate's shared doc, an import —
lands as a **copy** carrying `origin` (url + artifact id + version) as a
property. It is not a live link and will never go stale against its source.
That is the accepted cost of not introducing global identity.

---

## Rename

`knowledge` → `artifacts`. "Knowledge" describes ingested reference material;
this is a thing you author, version and ship.

Touches: `core/knowledge/` (12 modules), `core/tools/knowledge.py`,
`core/config/schemas/knowledge.py`, `Neo4jArtifactClient`, the `:Entry` label,
`.igni/artifacts.yaml`, the `artifacts.enabled` settings key,
`clients/web/src/components/panels/ArtifactPanel.tsx`, and the portal copy in
`portal/src/lib/entryKinds.ts` (ember-server).

The node-label change is a one-way graph migration, so it lands in the **same
change** as the version schema — one migration, not two. `.igni/artifacts.yaml`
is read on load with a fallback so existing projects do not lose entries;
the file is rewritten as `.igni/artifacts.yaml` on first sync.

Naming collision to resolve before the migration: *artifact* already names
Claude Code's HTML artifacts and the `igni-gpu-artifacts` store. If the
rendered/shared page is the same concept, fine. If not, pick a different noun
now.

### YAML sync carries head only

`ArtifactYamlFile` (`core/knowledge/models.py:443`) is the synced on-disk
form. It keeps **head content only** — version history is local graph state.
Otherwise every artifact edit produces a large repo diff, and the file stops
being reviewable. Consequence, stated plainly: a fresh clone has artifacts but
no lineage and no history.

---

## Visual

Rendered from the graph, served by the BE into the existing `clients/web`
surface as an artifact view alongside `ArtifactPanel.tsx` / `CodeIndexPanel.tsx`:

- the artifact content at a chosen version, with a version switcher (`HAS_VERSION {n}`)
- a lineage timeline — what this was built on, at which version, and whether
  that source has moved since
- a dependents list — what sits on top of this, and which of those are stale
- candidate edges shown distinctly, each with a confirm/dismiss action

**Export is a flatten of the same page** to one self-contained HTML file —
inline CSS/JS, no external fetches, embedded assets. PDF is print-to-PDF of
that file; there is no second renderer.

---

## Sharing

Export and share are the same projection, which is also the privacy boundary.

### What leaves

Projected out of the graph, never a raw dump:

| Kept | Dropped |
| --- | --- |
| Head (or pinned) version content | Chunks and embeddings |
| Artifact name, kind, version number | `session_id` on versions and edges |
| `CITES` → external sources | Candidate `DERIVED_FROM` edges |
| `DERIVED_FROM` **only between artifacts in the shared set** | Any edge pointing outside the shared set |
| `created_at`, author display name | `project_hash`, local paths, `origin` internals |

An edge to an artifact you did not share is dropped entirely — not rendered as
a dangling reference, which would leak the existence and name of private work.

### Server side

`ember-server` has no document resource today: the portal's entry kinds are
agents, skills, commands, workflows, rules, output-styles, hooks, tools, MCP
servers, plugins, scripts, settings (`portal/src/lib/entryKinds.ts`). This adds
one:

- a shared-document resource holding the exported HTML plus metadata
  (title, author, org, exported-at, source artifact id + version)
- an ACL: private, named users, whole org
- a read-only public route serving the stored HTML

A shared doc is a **snapshot**, not a live mirror. Re-sharing a newer version
replaces the snapshot at the same URL and records the version it came from.

### The domain caveat

"Anyone at my domain" does not exist today. Org membership comes from the
GitHub App collaborator check against a linked `github_login`, and email-domain
matching is explicitly **not** enforced. Domain-scoped access therefore needs
new membership machinery — it is not a filter over something that already
works, and it is the single riskiest item in the sharing cut.

---

## Work list

**1 — Schema and store** (`ember-code`)
`neo4j_schema.py` gets its own artifact schema statements; the knowledge DB
currently re-applies `COMMIT_SCHEMA_STATEMENTS`, which is wrong and gets fixed
here. `Neo4jArtifactClient` → `Neo4jArtifactClient`: `add_entry` becomes
`append_version`, `search` gains `WHERE v.head`, `list_entries` returns heads,
plus `versions(artifact_id)`, `version(artifact_id, n)`, `stale_dependents()`.
One-way migration from `:Entry` (each existing entry becomes an artifact with a
single `n=1` head version).

**2 — Capture and confirm** (`ember-code`)
Retrieval records candidate edges against the session; the write path attaches
them to the new version with `pinned_n`. Citation in a write promotes to
`confirmed`. Candidate pruning on the retention path.

**3 — Reconcile** (`ember-code`)
`stale_dependents()` surfaced as a tool and a panel action; pinned→head diff of
the source; proposed patch per dependent; accept appends a version and re-pins.

**4 — Rename + config** (`ember-code`, `ember-server`)
Module/label/settings-key rename, `.igni/artifacts.yaml` with a read fallback,
portal copy. Lands with step 1's migration.

**5 — View and export** (`ember-code/clients/web`)
Artifact view, version switcher, lineage timeline, dependents list, candidate
review. Self-contained HTML export.

**6 — Share** (`ember-server`, `portal`)
Export projection, document resource + ACL, public read route, portal UI. The
domain-membership question gets answered here or the cut ships with
org/named-user ACLs only.

---

## Open items

- The artifact/artifact-name collision, above.
- Whether a confirmed edge should survive its dependent being re-written from
  scratch, or be re-captured each version. Current design: re-captured per
  version, since the edge lives on `:Version`, not `:Artifact`.
- Retention: versions are immutable and accumulate. No pruning policy is
  specified; `core/knowledge` has a retention path that will need one.
- Cross-project derivation is deliberately out of scope and expensive to add
  later — it needs global identity, which is why it is called out here rather
  than left implicit.
