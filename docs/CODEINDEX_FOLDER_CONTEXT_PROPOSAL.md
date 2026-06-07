# Proposal: folder-summary annotations on `codeindex_query` results

> **STATUS — IMPLEMENTED.** The nested-tree response shape described below shipped in commit `c482514` ("codeindex: nested-tree response, OOP package, prompt refresh"). The `codeindex/` package now returns folder→file→class→entity trees with `summary`, `siblings`, `line_from`/`line_to`, and entity-level disambiguation refs at the leaves. A companion change (commit `0885ec0`) moved the per-commit project-map render to ember-server so every developer indexing the same commit sees the same overview — see the [Project Map](CODEINDEX.md#project-map) section in CODEINDEX.md for the live shape. This document is kept as the design record explaining *why* the response shape ended up this way.

Concrete example using the failing case from v2.3 run 1 — `codewrite_slack_notifications_reuse_pattern`.

---

## The scenario

User asks: *"Add Slack notifications for commit-analysis events (started / completed / failed), parallel to the existing email paths."*

Agent's first query:

```python
codeindex_query(
    query_text="commit analysis email notification started completed failed",
    limit=5,
    sections=["summary"],
)
```

The chroma search ranks `smtp_service.py` and a couple of email helpers high. The actual `app/services/slack/` module exists but doesn't surface in the top-5 because the agent's query was email-flavored.

---

## CURRENT response shape (today)

```json
{
  "commit": "db43edd3...",
  "total": 5,
  "items": [
    {
      "item_id": "9c12...",
      "name": "smtp_service.py",
      "type": "file",
      "kind": "code",
      "entity_type": "",
      "path": "app/services/email/smtp_service.py",
      "parent_id": "8b21...",
      "score": 0.671,
      "content": "[SECTION:purpose_and_functionality]SMTP-based email service that sends commit-processing notifications (started, completed, failed) to repository owners. Uses asyncio + aiosmtplib for async SMTP; templates live alongside in templates.py.[/SECTION]",
      "file_extension": ".py",
      "repository_id": "ember-server-rebuild-fix5"
    },
    {
      "item_id": "a4f8...",
      "name": "send_commit_processing_started",
      "type": "entity",
      "kind": "code",
      "entity_type": "function_definition",
      "path": "app/services/email/smtp_service.py::SMTPEmailService::send_commit_processing_started",
      "parent_id": "9c12...",
      "score": 0.654,
      "content": "[SECTION:summary]Sends a 'commit analysis started' email to the repo owner via the cached SMTP client. Returns True on success, logs and returns False on SMTP failure.[/SECTION]"
    },
    {
      "item_id": "f7e2...",
      "name": "send_commit_processing_completed",
      "type": "entity",
      "kind": "code",
      "entity_type": "function_definition",
      "path": "app/services/email/smtp_service.py::SMTPEmailService::send_commit_processing_completed",
      "parent_id": "9c12...",
      "score": 0.640,
      "content": "[SECTION:summary]Sends a 'commit analysis completed' email …[/SECTION]"
    }
    /* … two more … */
  ]
}
```

What the agent sees: three results, all in `app/services/email/`. Naturally infers "the email module is the reuse target" — copies `send_*` naming, never queries for `app/services/slack/`.

This is why the v2.3 response says `app/services/slack/notify_service.py` (a *new file*) and uses `send_*` names rather than the existing `notify_*` names. The grader fails it.

---

## PROPOSED response shape — fully nested tree

The response is a tree that mirrors the codebase's structure: **folder → file → class → entity**. Each level has its own `summary`, and its `matches` array holds the next level down. The matched entities are the leaves; everything between them and the root is structural context the agent can't skip.

```json
{
  "commit": "db43edd3...",
  "total": 5,
  "items": [
    {
      "item_id": "8b21...",
      "type": "folder",
      "name": "email",
      "path": "app/services/email",
      "score": 0.671,
      "summary": "Email notification service. Core responsibilities are cleanly separated: notifications.py communicates user-facing events, templates.py generates email content, smtp_service.py handles secure delivery.",
      "siblings": ["ai", "auth", "cache", "changeset_uploader", "codeindex", "commits", "github", "jsonl_changeset", "organizations", "portal", "webhooks"],
      "matches": [
        {
          "item_id": "9c12...",
          "type": "file",
          "name": "smtp_service.py",
          "path": "app/services/email/smtp_service.py",
          "line_start": 1,
          "line_end": 312,
          "score": 0.671,
          "summary": "SMTP-based async email service. Cached aiosmtplib client constructed in __init__; all send_* methods reuse it. Retry-once on transient SMTP errors via _send_with_retry().",
          "siblings": ["__init__.py", "notifications.py", "templates.py"],
          "matches": [
            {
              "item_id": "b3a5...",
              "type": "class",
              "name": "SMTPEmailService",
              "path": "app/services/email/smtp_service.py::SMTPEmailService",
              "line_start": 38,
              "line_end": 287,
              "summary": "Wraps aiosmtplib.SMTP with cached connection, structured logger.info(extra={...}), retry-once on transient SMTP errors. All send_* methods return bool.",
              "siblings": ["_DEFAULT_TIMEOUT", "_TEMPLATE_BASE_DIR", "_get_template_env"],
              "matches": [
                {
                  "item_id": "a4f8...",
                  "type": "entity",
                  "entity_type": "function_definition",
                  "name": "send_commit_processing_started",
                  "path": "app/services/email/smtp_service.py::SMTPEmailService::send_commit_processing_started",
                  "line_start": 142,
                  "line_end": 168,
                  "score": 0.654,
                  "content": "[SECTION:summary]Sends a 'commit analysis started' email to the repo owner via the cached SMTP client. Returns True on success, logs and returns False on SMTP failure.[/SECTION]",
                  "siblings": ["__init__", "send_otp", "send_password_reset", "send_welcome", "send_team_invite", "send_commit_processing_completed", "send_commit_processing_failed", "_send_with_retry"],
                  "refs": {
                    "called_by": [
                      { "item_id": "...", "name": "process_github_event", "path": "app/tasks/process_git_event.py::process_github_event", "summary": "Celery task entry-point for GitHub webhook events. Calls the email notifier on commit-processing transitions.", "score": 0.624 },
                      { "item_id": "...", "name": "_emit_started_event", "path": "app/services/webhooks/github_event_processor.py::GitHubEventProcessor::_emit_started_event", "summary": "Emits the 'started' lifecycle hook for a webhook event — calls the email notifier and updates DB status.", "score": 0.591 }
                    ],
                    "calls": [
                      { "item_id": "...", "name": "_send_with_retry", "path": "app/services/email/smtp_service.py::SMTPEmailService::_send_with_retry", "summary": "Wraps aiosmtplib.send() with one transient-error retry. Used by every send_* method.", "score": 0.580 }
                    ],
                    "via_parent": null
                  }
                },
                {
                  "item_id": "f7e2...",
                  "type": "entity",
                  "entity_type": "function_definition",
                  "name": "send_commit_processing_completed",
                  "path": "app/services/email/smtp_service.py::SMTPEmailService::send_commit_processing_completed",
                  "line_start": 170,
                  "line_end": 196,
                  "score": 0.640,
                  "content": "[SECTION:summary]Sends a 'commit analysis completed' email …[/SECTION]",
                  "siblings": ["__init__", "send_otp", "send_password_reset", "send_welcome", "send_team_invite", "send_commit_processing_started", "send_commit_processing_failed", "_send_with_retry"],
                  "refs": {
                    "called_by": [
                      { "item_id": "...", "name": "process_github_event", "path": "app/tasks/process_git_event.py::process_github_event", "summary": "Celery task entry-point …", "score": 0.611 }
                    ],
                    "calls": [
                      { "item_id": "...", "name": "_send_with_retry", "path": "app/services/email/smtp_service.py::SMTPEmailService::_send_with_retry", "summary": "Wraps aiosmtplib.send() with one transient-error retry …", "score": 0.560 }
                    ],
                    "via_parent": null
                  }
                }
              ]
            }
          ]
        }
      ]
    },
    {
      "item_id": "5d33...",
      "type": "folder",
      "name": "notifications",
      "path": "app/services/email/notifications.py",
      "score": 0.564,
      "summary": "Top-level orchestrator file: notify_commit_started/completed/failed are async functions that wrap SMTPEmailService.send_commit_processing_*. This is the public API that callers use; smtp_service.py is the implementation.",
      "siblings": ["__init__.py", "smtp_service.py", "templates.py"],
      "line_start": 1,
      "line_end": 96,
      "matches": [
        {
          "item_id": "0fe1...",
          "type": "entity",
          "entity_type": "function_definition",
          "name": "notify_commit_started",
          "path": "app/services/email/notifications.py::notify_commit_started",
          "line_start": 25,
          "line_end": 44,
          "score": 0.564,
          "content": "[SECTION:summary]Async wrapper that resolves recipient email and calls SMTPEmailService.send_commit_processing_started.[/SECTION]",
          "siblings": ["notify_commit_completed", "notify_commit_failed"]
        }
      ]
    }
  ]
}
```

What this gets the agent:

1. **One tree per result, structure-shaped like the code.** Folders contain files contain classes contain entities. Each level holds its `summary` and the next-level `matches` array. No flat lists, no separate ancestor-by-id table — the position of an item in the tree IS its ancestor chain.

2. **Reading the response IS reading the structure.** The agent cannot reach `send_commit_processing_started` without first passing through the folder summary (email purpose + sibling files: `notifications.py`, `templates.py`, `smtp_service.py`), the file summary (cached client, retry shape), and the class summary (aiosmtplib wrapper, return types, logging shape). All four levels are forced reading, in order, every time.

3. **`siblings` at every level** lists the names of the OTHER children under the same parent (the ones that didn't match). So at the folder level the agent sees the peer folders under `app/services/` (`ai`, `auth`, `cache`, `codeindex`, `commits`, `github`, `webhooks`, …); at the file level inside `app/services/email/` it sees `notifications.py` and `templates.py` even though only `smtp_service.py` matched; at the class level inside `smtp_service.py` it sees `_DEFAULT_TIMEOUT`, `_get_template_env`; at the entity level it sees the other `send_*` methods on the same class. **This directly addresses the case-2 failure shape**: when only `smtp_service.py` semantically matched but `notifications.py` is the right reuse target, the file-level `siblings` field surfaces it for free.

4. **`line_start` / `line_end` on every node.** The index already tracks line ranges for files, classes, and entities. Surfacing them lets the agent cite real `file:line` ranges in the preamble's `Reuse target` bullet (e.g., `app/services/email/smtp_service.py:142-168 SMTPEmailService.send_commit_processing_started`) instead of guessing or omitting line numbers. Cheap (two integers per node), high signal — it's what the preamble template already asks for.

5. **No ancestor summary is duplicated on the wire.** Multiple matched methods on the same class share that class's summary once; multiple matched classes in the same file share the file once. The verbose-flat-ancestors form would have repeated each ancestor per match.

6. **Leaves are matched items.** A leaf can be at any level — a folder match (the folder itself was the top hit), a file match (the file matched but no entities under it did), a class match, or an entity. `matches: []` at any level means "this level is the leaf for this branch."

7. **Score propagates upward as max(child scores).** A folder's score = max(file scores under it); a file's score = max(class scores under it); a class's score = max(entity scores under it). Folders/files/classes that contain a strong leaf rank high; ones that don't, rank low.

8. **Top-level entities (no class)** simply have one fewer level. `app/utils/helpers.py::standalone_function` produces folder → file → entity, with no class layer.

9. **Disambiguation refs ride on the leaf entity** (the existing per-query reference re-ranking — `called_by`, `calls`, `via_parent`). The leaf is the natural carrier because refs are *cross-cutting edges* in the graph, not part of the structural tree. Each ref entry already gets re-scored against `query_text` via `search_among` and capped at `DISAMBIGUATION_REFS_PER_DIRECTION` per direction; the new shape just attaches that result to the leaf instead of returning it via a top-level `refs: {item_id: …}` map. **Two orthogonal axes are now both visible at once:**
   - **Structural axis** (folder → file → class → entity): *where* in the codebase this thing lives.
   - **Reference axis** (`called_by` / `calls`): *who else uses this thing*, and *what does it use*.

   The `refs` field only appears on entity-level matches (folders/files/classes don't have call-graph edges of their own). Field shape is identical to today's:

   ```
   refs: {
     called_by: [ { item_id, name, path, summary, score }, ... ],
     calls:     [ { item_id, name, path, summary, score }, ... ],
     via_parent: "<parent name> (<parent path>) | null"
   }
   ```

   Each ref's `summary` is its own one-line description (already produced by Phase 4); `score` is its similarity to the original `query_text`. `via_parent` is set when the matched entity has no direct call edges and the refs come from its enclosing class/file (today's parent-fallback behavior).

This is a structural change to the API: from "ranked list of items" to "ranked tree of where the query landed, with each leaf carrying its cross-cutting references." The agent's mental model shifts from "what individual things matched?" to "which paths through the codebase did the query land on, what does each waypoint look like, and who else touches the leaf?"

---

## Why this fixes case 2

The agent's query lands all hits in `app/services/email/`. Today's flat response shows only those items, leaving the agent assuming the email module *is* the reuse target. With the nested tree, the agent reads top-down:

1. **Folder summary** for `app/services/email/` — explicitly names the sibling `app/services/slack/` module.
2. **File summary** for `smtp_service.py` — flags the cached-client convention.
3. **Class summary** for `SMTPEmailService` — flags `send_*` returning `bool`, `logger.info(extra={...})`.
4. **Entity content** for the matched methods.

The agent's next step becomes: *"There's an existing Slack module at app/services/slack/. Let me check it before mirroring email."* Query 2: `codeindex_query(query_text="notify_commit", path_prefix="app/services/slack")` → returns `notify_service.py::notify_commit_started/completed/failed`.

The agent now writes the preamble with `Reuse target: app/services/slack/notify_service.py` and uses the `notify_*` naming. Grader passes.

---

## Why this also fixes case 8 (commit retry)

Same shape, different table. Today the agent finds `app/db/sql/commit_processing.py` and stops. With the proposal, the result for the SQL folder leads the response, and reading top-down forces the agent past the folder summary first:

```json
{
  "type": "folder",
  "name": "sql",
  "path": "app/db/sql",
  "score": 0.612,
  "summary": "SQL persistence layer. CommitProcessing parent model + commit_processing_step.py (INSERT-only step history with attempt + step_order columns), webhook_events.py (per-event retry tracking), portal_repositories.py (repo metadata).",
  "matches": [
    {
      "type": "file",
      "name": "commit_processing.py",
      "path": "app/db/sql/commit_processing.py",
      "score": 0.612,
      "summary": "CommitProcessingDB CRUD + status transitions for the parent commit_processing row. update_status accepts increment_retry; the per-step audit trail lives in commit_processing_step.py.",
      "matches": [
        {
          "type": "entity",
          "name": "update_status",
          "path": "app/db/sql/commit_processing.py::CommitProcessingDB::update_status",
          "score": 0.581,
          "content": "[SECTION:summary]Updates parent CommitProcessing.status …[/SECTION]"
        }
      ]
    }
  ]
}
```

The folder summary names `commit_processing_step.py` and the discriminating column `step_order` *before* the agent reaches the matched children. The file summary again points at `commit_processing_step.py` as the audit trail. Agent's next move: query that file or walk it via codeindex_tree. The grader's required strings (`commit_processing_step` + `step_order`) end up in the preamble as actual cited entities.

---

## Implementation sketch (server side)

The grouping is recursive: collect every ancestor uuid of every matched item, hydrate them all in one batched read, then build the tree by walking each match up to its root.

```python
# query_service.py — after chroma returns ranked items

# 1. For every match, collect the chain of ancestor uuids
#    (entity → class → file → folder, walking parent_id).
ancestor_ids: set[str] = set()
chains: dict[str, list[str]] = {}  # match_id → [root, …, parent, self]
for item in ranked_items:
    chain = []
    cur = item
    while cur is not None:
        chain.append(cur.item_id)
        ancestor_ids.add(cur.item_id)
        cur = items_by_id.get(cur.parent_id)
    chains[item.item_id] = list(reversed(chain))   # root → … → match

# 2. Batched hydrate: one chroma call returns every node we need, with summary.
nodes = await idx.filter_items(ids=list(ancestor_ids), commit=sha)
nodes_by_id = {n.item_id: n for n in nodes}

# 3. Build the nested tree. Each node carries its own summary; matches[] holds
#    children that are themselves on a chain to a matched leaf.
def build_tree(matches: list[CodeIndexResult]) -> list[TreeNode]:
    """Recursively assemble: roots → … → matched leaves, each with summary."""
    by_root: dict[str, list[CodeIndexResult]] = defaultdict(list)
    for m in matches:
        chain = chains[m.item_id]
        if len(chain) == 1:
            # m is itself a root-level match (e.g. a folder hit directly).
            by_root[m.item_id].append(m)
        else:
            by_root[chain[0]].append(m)

    out = []
    for root_id, leaves in by_root.items():
        node = nodes_by_id[root_id]
        # Recurse: take every leaf whose chain runs through this root, drop
        # the root from each chain, recurse on the next level.
        next_level: list[CodeIndexResult] = []
        for leaf in leaves:
            chain = chains[leaf.item_id]
            if len(chain) > 1:
                # Pretend the leaf's chain skips this root for the next call.
                chains[leaf.item_id] = chain[1:]
                next_level.append(leaf)
        children = build_tree(next_level) if next_level else []
        # Children's score bubbles up; otherwise score = leaf score.
        score = (
            max(c.score or 0 for c in children) if children
            else max(l.score or 0 for l in leaves)
        )
        out.append(TreeNode(
            item_id=node.item_id,
            type=node.type,
            name=node.name,
            path=node.path,
            score=score,
            summary=shorten_summary(node.content),
            matches=children,
        ))
    out.sort(key=lambda t: t.score, reverse=True)
    return out

result_items = build_tree(ranked_items)

return ItemsResponse(
    commit=sha,
    items=result_items,
    total=len(ranked_items),
)
```

Cost: one batched chroma read per query (metadata + summary, no embedding). The total nodes hydrated equal the union of all ancestor chains — typically 3-4× the number of matched leaves, capped by the index's depth. Payload grows by ~50-150 tokens per intermediate node (each summary is short by construction at Phase-4).

---

## Trade-offs

**For:**
- Fixes case 2 and case 8 by surfacing peer modules in a position the agent literally cannot skip.
- Tool-level — works regardless of prompt rules, prompt size, or agent variance.
- Uses data the indexer already produced (folder summaries from Phase 4). No new computation.
- Compact when results cluster in 1–3 folders (very common): 5 entity matches in one folder become one folder + 5 inline matches, not 5 disjoint flat items.
- Restores natural hierarchy: the index has always been folder → file → entity, but the API response was flattening that. Folder-grouping puts the structure back into the wire format.

**Against:**
- **Breaking API change.** Every consumer of `codeindex_query` (the agent's prompt examples, the disambiguation refs path, the comparison runner) needs updating to read the new shape. Migrating callers is the bulk of the work, not the server-side grouping.
- **Singletons get extra wrapping.** A query that returns one entity in one folder still becomes `[{folder, matches: [entity]}]` instead of `[entity]`. Bigger payload for sparse cases.
- **Folder-summary quality is now the bottleneck.** If Phase 4's folder summaries are generic ("email-related services"), the proposal gives the agent a weak hint instead of a strong one. The lift depends on indexer summary quality, which we'd want to audit before shipping.
- **Top-N semantics need redefining.** `limit=10` today means 10 items. After grouping it could mean 10 folders (probably ~30 child matches), or 10 total matches across folders. Pick one and document it.

**Mitigations:**
- Add a `flat=True` legacy mode that returns the old shape for callers that haven't migrated.
- Cap matches per folder (e.g. top-5 within each folder) to keep payloads bounded.
- Audit folder summaries first — sample 20 random folders, eyeball whether their summaries name sibling modules. If they don't, extend the indexer's folder-summarization prompt to explicitly call out peers before shipping the response change.

---

## Compared with the original "Idea A" (separate `folders` map)

The original proposal kept `items[]` flat and added a parallel top-level `folders` map. The agent had to cross-reference each item's `parent_id` to the folder summary — a lookup the agent might or might not perform. The folder-grouped shape removes that step: the folder summary is *the parent envelope* of the item, not an annotation. The agent reads it on the way down, every time.

The trade is API breakage vs reliability. Idea A is backward-compat but trusts the agent to look up the folders map. Folder-grouping changes the wire format but eliminates the lookup, giving the structural change a stronger compliance guarantee.
