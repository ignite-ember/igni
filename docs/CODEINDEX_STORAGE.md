# CodeIndex Storage

What the per-commit code index actually looks like on disk and what
the agent sees when it queries it.

## On-disk layout

```
~/.ember/projects/<project_id>/
├── code_index/
│   ├── manifest.json
│   ├── <sha_a>.chroma/   # one chroma directory per indexed commit
│   └── <sha_b>.chroma/
└── state.db              # SQLite — project-scoped reference graph
```

Each `<sha>.chroma/` is fully self-contained (copy-on-write from the
parent commit, then the JSONL changeset is replayed). The reference
graph lives in SQLite next to chroma because edges are
*project-scoped* — you want callers across all indexed history, not
just the current commit.

CodeIndex tools are exposed to the agent only when
`<HEAD>.chroma/` exists locally. No directory → no tool, no prompt.

## Two chroma collections

Both collections use `all-MiniLM-L6-v2` (384-dim cosine). Every
chroma row has four parallel fields:

| Field | What it is |
|---|---|
| `id` | A string the consumer chose. Stable across operations. |
| `document` | The text that was embedded. **This is where the actual content lives.** |
| `embedding` | 384-dim vector, auto-derived from `document`. |
| `metadata` | Flat dict (str/int/bool only). Filterable via `where=`. |

### `code_index_documents` — one row per item

`id` = the item's UUID5. `document` = the full content (folder
summary / file summary / entity summary / doc-section body, wrapped
in `[SECTION:...]` markers when applicable).

The metadata is **structured by design** — every quality dimension is
its own typed field, so chroma can index and exact-match each one.
There is no catch-all `tags` field; multi-value categories like
`vulnerabilities` are dedicated fields too.

#### Identity / scope

| Field | Type | Set on | Filter |
|---|---|---|---|
| `name` | str | all | `{"name": "verify_password"}` |
| `type` | str | all | `{"type": "entity"}` (or `"folder"`/`"file"`) |
| `kind` | str | all | `{"kind": "code"}` or `{"kind": "docs"}` |
| `entity_type` | str | entity | `{"entity_type": "function"}` |
| `path` | str | all | `{"path": "src/auth.py"}` |
| `parent_id` | str | file/entity | `{"parent_id": "u2"}` |
| `file_extension` | str | file/entity | `{"file_extension": ".py"}` |
| `repository_id` | str | all | rare |
| `archived` | bool | all | `{"archived": false}` |
| `timestamp` | str | all | rare |
| `token_count` | int | all | `{"token_count": {"$gt": 100}}` |
| `line_from` / `line_to` | int | entity = real, others = `-1` | `{"line_from": {"$gte": 1}}` |

#### Quality categoricals (single-value enum strings)

Each is a chroma metadata string set to one of the enum values; empty
string `""` for rows where it doesn't apply. Exact-match queries via
`{"<field>": "<value>"}`.

| Field | Values |
|---|---|
| `quality` | `excellent` / `good` / `fair` / `poor` / `unknown` |
| `complexity` | `low` / `medium` / `high` / `very-high` / `unknown` |
| `security` | `secure` / `minor-issues` / `major-issues` / `critical` / `unknown` |
| `testing` | `well-tested` / `partially-tested` / `untested` / `unknown` |
| `testability` | `easy` / `moderate` / `difficult` / `unknown` |
| `documentation` | `excellent` / `good` / `minimal` / `missing` / `unknown` |
| `performance` | `optimized` / `acceptable` / `inefficient` / `critical` / `unknown` |
| `issues` | `none` / `minor` / `moderate` / `severe` / `unknown` |
| `maintainability` | `excellent` / `good` / `fair` / `poor` / `unknown` |
| `architecture` | `excellent` / `good` / `fair` / `poor` / `unknown` |
| `technical_debt` | `none` / `low` / `medium` / `high` / `unknown` |
| `cohesion` | `high` / `medium` / `low` / `unknown` |
| `coupling` | `loose` / `moderate` / `tight` / `unknown` |
| `stability` | `stable` / `evolving` / `unstable` / `unknown` |
| `priority` | `critical` / `high` / `medium` / `low` / `none` |
| `needs_refactoring` | bool |

Not every dimension applies to every row type — `cohesion`/`coupling`/
`stability` are folder-only; `documentation`/`issues` are entity-only;
`testing` and `maintainability`/`architecture`/`technical_debt` are
file/folder. Inapplicable fields are stored as `""` (or absent on the
JSONL line, depending on the producer).

#### Multi-value categories

Each is a chroma metadata string holding the list values joined and
bracketed by `\x1f` (ASCII unit separator), e.g.
`"\x1fsql-injection\x1fxss\x1f"`. The bracketing prevents
`$contains: "sql"` from false-matching `sql-injection`. Filter via
`{"vulnerabilities": {"$contains": "\x1fsql-injection\x1f"}}` — the
`codeindex_query` tool wraps the brackets internally so the agent
just passes plain values.

| Field | Examples of values |
|---|---|
| `vulnerabilities` | `sql-injection`, `xss`, `token-leak` |
| `frameworks` | `fastapi`, `sqlalchemy`, `react` |
| `domain` | `auth`, `payments`, `api` |
| `concerns` | `performance`, `security`, `testability` |
| `layers` | `presentation`, `business-logic`, `data-access` |
| `patterns` | `singleton`, `factory`, `observer` |
| `keywords` | `async`, `cache`, `retry` |
| `file_issues` | `memory-leak`, `n+1-query`, `race-condition` |

### `code_index_chunks` — many rows per item

The chunker splits each document into ~800-char overlapping windows.
Each chunk gets its own embedding and is what semantic search
actually scores against. `parent_doc_id` joins back to
`code_index_documents` for the full body and full metadata.

- `id` = `f"{item_id}::{chunk_index}"`
- `document` = the chunk text (a slice of the parent's content)
- `metadata` = narrow, denormalized from the parent: `parent_doc_id`,
  `chunk_index`, `name`, `type`, `path`, `file_extension`,
  `repository_id`

If you want to filter chunks by quality fields directly, those aren't
denormalized today — query against `code_index_documents` instead and
follow `parent_doc_id` to chunks.

## References (SQLite, not chroma)

Reference edges live in `state.db` because they're project-scoped
(callers across all history, not just current commit) and have no
semantic value worth embedding.

```sql
CREATE TABLE file_references (
    from_uuid   TEXT NOT NULL,
    to_uuid     TEXT NOT NULL,
    relation    TEXT NOT NULL,    -- "calls" / "called_by" / "imports" / ...
    meta        TEXT NOT NULL,    -- JSON: from_entity_path, to_entity_path,
                                  --       from_entity_name, to_entity_name,
                                  --       from_entity_type, to_entity_type
    PRIMARY KEY (from_uuid, to_uuid, relation)
);
CREATE INDEX ix_file_references_relation ON file_references (relation);
```

Every relation emits two edges (forward + reverse). Canonical
relations: `calls` / `called_by`, `imports` / `imported_by`, `extends`
/ `extended_by`, `implements` / `implemented_by`, `decorates` /
`decorated_by`, `types_as` / `typed_by`.

## What the agent sees

Agents do not write raw chroma queries. The `codeindex_query` tool
takes typed enum args and translates them internally.

```python
# "Find all entities with critical security issues"
codeindex_query(security="critical", type="entity")

# "Untested complex functions" — quality + categorical filters compose
codeindex_query(complexity="high", testing="untested", entity_type="function")

# "SQL injection candidates near auth code" — semantic + tag-list
codeindex_query(query_text="raw SQL with user input",
                vulnerabilities=["sql-injection"],
                domain=["auth"])

# "What calls X?" — reference traversal
codeindex_query(target="references", item_ids=["<uuid>"], relations=["called_by"])

# "Fetch this specific item"
codeindex_query(ids=["<uuid>"])
```

Returned shape:

```json
{
  "commit": "a1b2c3",
  "items": [
    {
      "item_id": "u3",
      "name": "verify_password",
      "type": "entity",
      "entity_type": "function",
      "kind": "code",
      "path": "src/auth.py::verify_password",
      "line_from": 12,
      "line_to": 18,
      "score": 0.81,
      "chunk_preview": "...",
      "content": "[SECTION:summary] ..."
    }
  ],
  "total": 1,
  "truncated": false
}
```

For `target="references"`:

```json
{
  "target": "references",
  "outgoing": [{"from_id": "u3", "to_id": "u_bcrypt", "relation": "calls", "meta": {"_etc": "..."}}],
  "outgoing_total": 1,
  "incoming": [],
  "incoming_total": 0,
  "truncated": false
}
```

## File references

- Index core: `src/ember_code/core/code_index/index.py`
- Op parsing + delta application: `src/ember_code/core/code_index/delta.py`
- Item schema: `src/ember_code/core/code_index/schema/items.py`
- Agent tool: `src/ember_code/core/tools/codeindex.py`
- Tool gate + prompt section: `src/ember_code/core/session/core.py`
- Server emitter (producer): `ember-server/app/services/jsonl_changeset/{writer,emitter}.py`
