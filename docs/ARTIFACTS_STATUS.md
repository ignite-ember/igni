# Artifacts: what is built, what is not

**Branch:** `feat/artifacts-lineage` in both `ember-code` and
`ember-server`. Nothing is merged.

**Companion documents.** [ARTIFACTS.md](ARTIFACTS.md) is the design
record — *why* the store has the shape it does. This file is the
status — *what exists today*. When they disagree, this one is wrong
and should be corrected; the design record describes intent, and
intent does not drift.

---

## What this is

A document you author keeps one identity and an append-only chain of
versions. Every write records **which version of which other document
it was built on**. That one fact is what the whole system is for:
when a source changes, the documents standing on it can be found and
fixed, instead of going quietly wrong.

It replaces the knowledge index, which could not do any of that:
writing replaced content in place, so "show me v1" had no answer; a
`source` string recorded *where* text came from but not *which
version*; and nothing connected a document to the ones it was written
from.

```
(:Artifact {head_n})-[:HAS_VERSION]->(:Version {n, head})
(:Version)-[:DERIVED_FROM {pinned_n, state}]->(:Version)
```

A write appends `n+1` and moves the head. The previous version keeps
its content and loses only its `head` flag. Search filters to heads,
so the agent never retrieves text that was corrected three revisions
ago, while `(artifact_id, n)` stays addressable forever.

Staleness is then a comparison — `pinned_n < head_n` — with nothing
written when a source moves and no invalidation flags to repair. And
because the pin survives, reconciliation can diff pinned→head and
hand *that* to the agent, rather than asking it to re-derive blind.

---

## Built and committed

### The store — `ember-code`, commit `48e54e7`

| | |
| --- | --- |
| Versioned store, lineage, staleness | `core/artifacts/neo4j_client.py` |
| Coordinator (resolve, chunk, capture, reconcile) | `core/artifacts/index.py` |
| Retrieval capture | `core/artifacts/capture.py` |
| Pinned→head diff | `core/artifacts/reconcile.py` |
| Self-contained HTML + export projection | `core/artifacts/render.py`, `export.py` |
| Agent tools | `core/tools/artifacts.py` |
| Session policy (sources are required) | `core/session/artifact_ops.py` |

**Sources are required.** `artifact_create` and `artifact_revise`
take a mandatory `sources` argument. When it is missing the refusal
names the exact refs the session read, so the agent confirms a real
corpus rather than inventing a plausible one:

```
Error: this write needs a source declaration.
You have read these artifacts this session:
  - 826de04b1ae70098@1
```

An empty list is allowed **only** with a stated reason. Some
documents genuinely have no antecedent, and a forced citation poisons
the graph worse than an honest gap.

**Capture is generous, confirmation is strict.** Every artifact the
agent retrieves is recorded as a `candidate` edge — missing a
derivation is unrecoverable, so capture guesses. Only `confirmed`
edges raise staleness — a false dependency that raises alarms teaches
people to ignore alarms.

**The rename is done.** `knowledge` is gone from the codebase. Old
`knowledge:` config and `.igni/knowledge.yaml` still load via
aliases, and are written back under the new names.

### Sharing — `ember-server`, commits `c942128`, `22f7dde`, `0e381c0`

Four endpoints under `/v1/artifacts/shared` (publish, list, read,
revoke), a portal page at `/a/:slug`, and five visibilities:

| Visibility | Who can open it |
| --- | --- |
| `private` | Only you |
| `users` | Named recipients, matched on email **or** user id |
| `domain` | Anyone whose account email shares the author's |
| `org` | Your organisation |
| `public` | Anyone with the link, signed in or not |

`domain` is the one that needed an argument. The first cut left it
out, reasoning that the deployment could not verify who belongs to a
domain — which was wrong, and wrong in a specific way: it checked
*GitHub org membership*, genuinely unverifiable for this purpose, and
concluded the *email address on the account* was too. The address is
verified on every sign-up path (OTP delivered to it for password
sign-up, `email_verified` for OIDC, the providers' own verified
primary address for OAuth). One deployment serves one customer whose
people share a domain and may sit in several orgs, so "my colleagues"
is the domain, not the org — and the tests assert exactly that: a
same-domain reader with **no shared org** can open the document.

The domain is captured from the account at publish and never accepted
from the request; a client that could name its own domain could
publish into somebody else's. It is stored rather than joined on each
read, so an author who changes employer does not silently re-target a
document shared with their old colleagues.

**One deployment assumption is written down rather than guarded:**
`oidc_link_by_email` lets an operator accept an unverified address
from their IdP. `domain` inherits that decision and is no stronger
than it. Nothing in the code can detect the difference.

A shared page can never contain more than a local export — both run
the same projection. An edge pointing outside the shared set is
**dropped**, not rendered as a dangling reference, because a dangling
reference leaks the name and the existence of private work.

### Ask the graph — `ember-code`, commit `2c454a6`

`artifact_search` finds documents *about* a topic. It cannot answer
questions *about the documents* — what was built on what, what would
break, what nobody reviewed — because those are structural.

A question now goes to the session's own model with the graph schema
in front of it, comes back as Cypher, runs read-only against the
local Neo4j sidecar, and returns rows plus a drawable projection.
**All of it on this machine.** There is no server-side counterpart
and there is not meant to be: the graph does not exist anywhere but
here.

- The query is **shown and editable** — that is the control, not a
  debugging affordance. A query that is nearly right is far more
  common than one that is wrong.
- **One retry, carrying the reason.** The second attempt gets the
  guard's or the database's actual complaint; a blind retry
  reproduces the same mistake.
- **`UNANSWERABLE` is a valid answer.** A model with no way to refuse
  invents a query returning nothing, which reads as "there are none"
  rather than "I did not understand".
- The graph is projected from whatever columns the rows carry, so the
  picture always matches the table. `RETURN count(*)` draws nothing
  rather than one mystery dot.

Available to the agent as `artifact_ask` and to the panel as
`artifact_ask` / `artifact_run_cypher`.

### Verified end to end

Against real Neo4j 5.26, the scenario this was built for:

```
create source → write without citing → refused, offering ['…@1']
→ cite → revise the source → "Login flow is pinned to Auth model@1,
now at @2 (1 version behind)" → diff pinned→head → fix
→ still stale: 0 → v1 and v2 both readable
→ rendered self-contained (3.3 KB)
```

### Tests

| Suite | Result |
| --- | --- |
| `ember-code` | 4570 passed |
| `ember-server` | 2483 passed |
| portal | 653 passed |
| web client | 842 passed |

One pre-existing `ember-code` failure remains
(`test_the_commit_convention_is_followed`) — it is about commits
already on `main`, not this work.

Live Neo4j tests need `docker start neo4j-test` plus
`NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_TEST_USER=neo4j
NEO4J_TEST_PASSWORD=integration-test`. They skip silently otherwise,
which is worth knowing: a green run does not mean they ran.

---

## Decided, not built: comments and rights

Decisions taken; no code written.

**An "edit" right is a suggestion, not an edit.** The version chain
lives on the author's machine and the server holds a rendered
snapshot, so a server-side edit has no way to enter the chain — the
local store cannot ingest a change made to a rendering. An editor
selects text and proposes replacement wording; the owner accepts or
rejects it locally, and accepting appends a version and republishes.
One author, lineage intact.

**Anyone who can view can comment.** The difference between viewer
and editor is that an editor's comment may carry proposed text. A
viewer who spots an error and has no way to say so is the failure
this avoids.

**The server pushes comments over a live connection.** Chosen over
polling. This is the largest piece of new infrastructure in the plan:
a per-author connection on the server, and reconnect/backoff on the
client. Worth flagging as the main risk in this cut — the author's
laptop sleeps, and a push channel has to survive that without either
losing comments or duplicating them.

### Shape of the work

1. **Server** — a comments table (anchor to a version + selected
   text, body, optional suggested text, status, thread), an
   `audience` that carries a role rather than a bare identity, and
   endpoints to post, list and resolve. The anchor records *which
   version* it was made against; a comment on v2 must be legible when
   the document is at v3.
2. **Push** — per-author channel, reconnect, replay-on-reconnect so a
   sleeping laptop does not miss anything.
3. **Portal** — text selection → comment box, comment thread beside
   the document, suggested-text UI for editors.
4. **igni, locally** — a review screen listing open comments across
   your published documents, with the agent drafting resolutions;
   accepting writes a new version and republishes.

---

## Known gaps in what is built

Small, specific, and worth having written down.

**Three config flags are declared and never read.** In
`core/config/schemas/llm_behavior.py`:

- `capture_reads` — capture is currently always on; the flag does
  nothing.
- `candidate_ttl_days` — `prune_candidates()` exists on the client
  and the index, but nothing calls it on a schedule.
- `warn_on_stale` — nothing checks for stale sources at session
  start.

A declared flag that does nothing is worse than a missing one: it
reads as a setting somebody can rely on.

~~**Six BE methods have no UI.**~~ **Closed.** Confirm and dismiss
sit on each captured source in the lineage list; the stale count in
the header opens a review screen where each row can fetch its
pinned→head diff on demand; export and share sit beside Remove on a
document. The share dialog spells out who each option reaches in
full, because "Domain" is not a description and this is one click
that can put a document in front of a company.

**The graph view is deliberately simple.** Deterministic layout, tens
of nodes, no force simulation — the same query draws the same picture
twice, and a simulation would be a dependency in an offline app. If
results grow past ~50 nodes it will need revisiting.

---

## Suggested order

1. ~~Wire the six panel controls.~~ **Done.**
2. Comments + rights, server first, push second.
3. The local review screen.
4. The three config flags — cheap, and each is a promise currently
   not kept.
