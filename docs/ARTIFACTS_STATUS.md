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
| Agent tools | `core/tools/artifacts.py` — **archivist only** |
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
| `public` | Anyone with the link, signed in or not |

There is deliberately no `org`. Every account gets an organisation
created for it at sign-up (`get_or_create_personal_org`), so for most
people that scope contained only themselves — a control that looked
like it shared with a team, sat directly beneath one that shared with
the whole company, and failed silently when picked. The domain is the
grouping that means something here, and unlike org membership it is
verified on every sign-up path.

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
here. Co-authoring does not change that — the server holds version
*content* and comments as ordinary rows, never edges, and cannot
answer a structural question about either.

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

### Archivist evals — 14/15

Run against MiniMax-M2.7 through the capture proxy, temperature 0:

```
.venv/bin/python tests/evals/archivist/run_live.py --model MiniMax-M2.7-proxied
```

Two consecutive full runs scored 14/15, with a different single case
red each time (`does_not_touch_confirmed_sources`, then
`reviews_an_unconfirmed_capture`). Thirteen cases are stable; the
residual flip is one case per run and is not yet attributed.

The route there is worth more than the number, because two of the
three things fixed were not agent problems at all.

**A revision used to drop its lineage.** Derivation edges hang off a
*version*, so a revision starts with none — and a declaration that
forgot to re-state a source silently dropped it. Revising a stale
document therefore made the staleness *disappear*, because the
dependency was erased rather than reconciled, leaving a document
reporting itself healthy while standing on a version nobody checked
it against. Measured on the eval fixture: Login flow v1 had two
confirmed sources, v2 had none, and the stale row went with them.

A revision now inherits the previous version's confirmed edges at the
version they were pinned at, so inheriting resolves nothing on its
own — an inherited stale edge is still stale. A source the
declaration names wins, which is how a pin moves forward. Candidates
are not inherited (evidence about the version that was read, not a
claim about its replacement), and `artifact_dismiss_source` still
removes an edge — removal takes a decision rather than an omission.
Locked in by `tests/test_artifact_revision_keeps_its_lineage.py`.

**Cases were contaminating each other.** `SuiteRunner` never passed
`session_id`, so every case ran in the agent's default session and
Agno reloaded the previous cases' tool calls into
`response.messages` — precisely what `CaseRunner`'s own docstring
warns about. Symptom: `never_writes_through_cypher` failed inside the
suite and passed 2 of 3 times run alone, at temperature 0, and traces
carried tools belonging to other cases' scenarios. Each case now gets
`eval-{agent}-{case}-{run}`. **This affected all 31 suites**, so
other agents' historical scores are suspect in the same way — the
`data-architect`'s 4/12–8/12 swing is the obvious thing to re-measure.

**The agent definition gained three things it actually needed:** that
reading a diff is not a resolution and every stale row ends in
reconcile-or-repin; that `artifact_revise` is not the route to a
stale document however you learned it was stale; and that a refused
write is the answer, not an obstacle — being asked to delete through
Cypher is answered by naming the tool that would do it, not by doing
it.

**Two cases were wrong, not the agent.**
`edits_the_document_that_is_actually_wrong` required
`artifact_reconcile` and so failed an agent that read both documents
in full, rewrote the text correctly and pinned to v3 via
`artifact_revise` — an equally honest route now that revisions keep
their lineage. It grades the outcome instead.
`writes_a_structural_query` asked for an answer while grading the
Cypher, so `artifact_ask` satisfied the prompt and the *planner's*
query got graded instead of the archivist's; it now asks for the
query and the answer.

**Method note.** A judge guideline saying "fail if it re-confirms a
confirmed edge" did not catch two agents doing exactly that. Moving
those to `unexpected_tool_calls` caught both on the first run. State
a failure as "this tool must not be called" where you can, and keep
the judge for what only reads as wrong in prose.

---

## Co-authoring, comments and rights

Built, except the UI. This section replaced a set of decisions that
said the opposite, and the reversal is worth recording rather than
quietly overwriting.

**What was decided first.** One author owns the chain; the server
holds a rendered snapshot; an "edit" right is a *suggestion* the
owner accepts locally, because a server-side edit has no way to enter
a version chain that lives on somebody's laptop.

**What changed it.** A request to actually co-work on documents, with
two-way sync. The old reasoning was sound given its premise and the
premise no longer holds: if two people are to append to one chain,
neither can see the other's head, so something has to decide what
order the appends happened in. The only participant both can reach is
the server.

### The server orders; it does not merge

An append names the version it was written from. If that is still the
head it lands as `head + 1`. If it is not, it is refused with **the
versions the caller is missing**, so they can rebase in one round
trip — the same bargain a rejected `git push --force-with-lease`
offers. No merge is invented: two people editing the same paragraph
is a decision, and a document nobody wrote is the worst way to settle
it. A unique constraint on `(document, n)` makes a genuine race safe;
one retry distinguishes a race from a stale writer.

Derivation edges stay client-side facts. `sources` travels through
the server as opaque JSON for each collaborator to rebuild locally.
This server has no artifact graph and cannot answer a staleness
question about one.

### Reading and writing are separate grants

Visibility widens who can *read*. `writers` names who may append, and
is always an explicit list of people. A document shared with a whole
company does not become writable by it — the link is the thing that
travels, and nobody re-reads permissions before forwarding one.

Anyone who can read can comment. The difference a writer gets is that
their comment may carry replacement text, which is what makes
accepting it an ordinary append with an author and a reason.

### Comments are nodes in the local graph

`(:Comment)-[:ON]->(:Version)`. Captured on the server, where the
reader is, and projected down on sync — so the whole history is
local. "Which documents drew comments after their source moved" is
one query alongside staleness and lineage, and it answers with the
network down.

Anchored to a version, not a document: a remark about v2 means
nothing beside a paragraph rewritten in v3. The quoted passage is the
anchor that survives; the offsets are a highlighting hint that rots
on the first edit.

### The live channel is an optimisation, not a delivery mechanism

SSE, at `GET /v1/artifacts/comments/stream`. **It carries a cursor
and never a comment.** The client answers a nudge by running the sync
pull it already has.

That is what makes a bad network harmless. Correctness lives in the
cursor: the pull is ordered on `updated_at`, projection is a MERGE on
the comment id, and a row naming a version the client has not fetched
is skipped *without consuming the cursor*. A laptop that sleeps
through fifty notifications wakes, pulls once, and is exactly as
correct as one that received every one. The stream decides only how
soon.

Each event's `id` is the cursor, so `EventSource`'s `Last-Event-ID`
resumption is the same mechanism as the `since` parameter arriving
through a different door — the sleeping-laptop case that was flagged
as this cut's main risk stops being special.

SSE rather than socket.io because writes already go over POST, so
only one direction is needed; socket.io would add a protocol layer, a
client library and sticky sessions for bidirectionality nothing uses.
Long polling stays available as a fallback if a customer's proxy eats
SSE, and buys nothing otherwise.

**One operational limit, stated rather than guarded.** The notifier
is in-process. A deployment running several workers will have
listeners on a process that does not see a given write; those
listeners miss the nudge and fall back to their reconnect interval.
Worth knowing before adding a second worker, not worth a message
broker until there is one. Nothing is lost either way — see the
cursor, above.

### Still to build

The UI, and only the UI: text selection → comment box in the portal,
and the local review screen where the agent drafts resolutions and
accepting appends a version.


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

## Who can touch the store

One agent: the **archivist** (`bundled_agents/archivist.md`), reading
as well as writing. No other agent has an artifact tool.

Two reasons. *Provenance*: a write must declare what it was built on,
and an agent midway through a refactor writes something true and
cites nothing — not carelessly, but because citing is not its task.
Reading belongs with writing because they share one model.

*Attributable traces*: a capability spread across every agent makes
every agent's manifest claim it, and a trace calling the tool could
belong to any of them. The fine-tuning corpus already carries 1,370
specialist traces whose manifests had to be rebuilt and 823 that had
to be relabelled over exactly this. `CodeIndexTools` is the shape
that works — one function, one agent, one grant.

The cost is real and worth naming: a lookup is now a delegation
round-trip, so the main agent will consult documents less often than
if it held `artifact_search` itself. If that turns out to suppress
reads, the fix is not to hand the tool back but to surface relevant
documents as *context* rather than as a tool call — no trace to
attribute, no manifest to widen.

## Suggested order

1. ~~Wire the six panel controls.~~ **Done.**
2. Comments + rights, server first, push second.
3. The local review screen.
4. The three config flags — cheap, and each is a promise currently
   not kept.
