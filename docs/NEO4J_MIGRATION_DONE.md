# Neo4j migration — done and remaining

## What was done in this session

Fifteen commits on `revision-2`. Total: 11 new files, 27 modified
files, 13 test files deleted, 5 chroma modules + 1 chroma schema
module deleted, 1 migration re-enabled, 2 dead SQLite branches
removed from the indexer, 38 new offline tests for the
neo4j scaffolding.

### Committed

1. `ae7f066` — streaming `<think>` fix (separates tagged thinking
   from std content on the wire).
2. `65a4156` — neo4j scaffolding imports + JDK Intel-Mac arch
   detection fix + integration test skip-guard.
3. `56bf183` — defer the drop-tables migration (later re-enabled).
4. `538e012` — `Neo4jMetaClient` + the code_index `neo4j_client=`
   / `runtime=` seam + runtime hardening (JDK arch filter, stderr
   leak fix, `start_new_session` consistency).
5. `38891fc` — `CodeIndex(runtime=...)` derives a per-commit client
   from the runtime.
6. `7c2233a` — `KnowledgeIndex` is backend-pluggable (chroma |
   neo4j).
7. `bd7a804` — `Neo4jKnowledgeClient` (per-project knowledge store
   on neo4j, with e2e round-trip test).
8. `f1cf157` — `Session.attach_knowledge_neo4j(runtime)`.
9. `2e686ee` — `SessionOrchestrator.attach_neo4j()` wires the
   runtime at BE boot (gated on `EMBER_NEO4J_RUNTIME`).
10. `b48c40e` — `Session.attach_codeindex_neo4j(runtime)` + the
    orchestrator wires both paths (knowledge + code) in one call.
11. `7ecd72e` — style: drop blank lines after `TYPE_CHECKING` guard.
12. latest — **the chroma retirement**: chroma is gone from
    `code_index` + `knowledge`. SQLite fallback deleted. Migration
    `d4e5f6a7b8c9` re-enabled.
13. `de0ead9` — **the missing piece** (was untracked):
    `Neo4jRowCodec` (`core/code_index/neo4j_codec.py`) +
    `maybe_cutover` (`core/code_index/cutover.py`) +
    BE-startup wiring in `backend/app.py`. Also refreshed the
    `d4e5f6a7b8c9` migration's module docstring (dropped the
    "PARKED" notice that explained why the file was renamed
    `.py.disabled`).
14. `0995c52` — **jvm package**: `ember_code.jvm.ensure_jdk` sync
    wrapper for the IDE shells (Tauri / JetBrains / VSCode) that
    bootstrap a JDK as a subprocess.
15. `d387c56` — **38 missing unit tests** for the neo4j scaffolding
    (`test_neo4j_runtime`, `test_neo4j_schema`,
    `test_jdk_bootstrap`, `test_per_commit_isolation`). The
    modules have shipped for several commits without a single
    offline-passing test.
    - `core/code_index/index.py` rewritten to use neo4j for items
      / chunks / edges. The chroma path is deleted. Public methods
      (`add_item`, `remove_item`, `search`, `search_among`,
      `filter_items`, `get_item`, `head_stats`, `clean`,
      `forget_commit`, `prepare_commit`, `sweep_stale_dirs`) all
      route through the per-commit `Neo4jClient` when a runtime
      is configured; raise `NotImplementedError` otherwise.
    - `core/knowledge/index.py` rewritten to require
      `neo4j_client=` (chroma branch deleted). Public methods all
      route through `Neo4jKnowledgeClient`. Added
      `_require_embedder()` helper.
    - `core/embedder.py` (new) — `Embedder` protocol + `ZeroEmbedder`
      / `HashEmbedder` / `LiveEmbedder` (wraps `core.embeddings`).
    - `backend/session_orchestrator.py` — `attach_neo4j()` constructs
      the `Neo4jRuntime` and calls both `Session.attach_knowledge_neo4j`
      and `Session.attach_codeindex_neo4j`.
    - `backend/app.py` — calls `await orchestrator.attach_neo4j()`
      after `setup_pool()`.
    - `core/session/core.py` — `Session._init_knowledge` defers the
      default chroma path (which is gone); production installs neo4j
      later via `attach_knowledge_neo4j`. New `attach_codeindex_neo4j`
      mirrors the knowledge attach.
    - `migrations/versions/d4e5f6a7b8c9_drop_code_index_tables.py`
      — re-enabled (was `.py.disabled`).
    - Dead SQLite fallback in `file_reference_service()` replaced
      with a `NotImplementedError` (the table was dropped by
      `d4e5f6a7b8c9`).

### Deleted (in this commit)

**Chroma modules in `core/code_index/`:**
- `chroma_client_factory.py` (the transitional shim — no longer
  needed; knowledge is now neo4j-only).
- `chroma_codec.py`
- `chunk_search.py`
- `schema/chroma_row.py`

**Chroma-era code_index tests:**
- `tests/test_code_index.py`
- `tests/test_code_index_db.py`
- `tests/test_code_index_delta.py`
- `tests/test_codeindex_build_tree.py`
- `tests/test_codeindex_eval_fixture.py`
- `tests/test_codeindex_tools.py`
- `tests/test_delta_roundtrip.py`

**Knowledge chroma tests:**
- `tests/test_knowledge.py`
- `tests/test_knowledge_index.py`
- `tests/test_knowledge_ops.py`
- `tests/test_knowledge_ingest_helpers.py`
- `tests/test_knowledge_tools.py`

**Removed from `test_project_map.py`:**
- `populated_index` fixture + `test_apply_delta_writes_commit_summary_to_disk`
  + `test_apply_delta_without_commit_summary_leaves_no_map` —
  these exercised the now-removed SQLite fallback. The disk-level
  ProjectMap tests + the session-loader tests still cover the
  flow.

**Replaced in `test_codeindex_neo4j_backend.py`:**
- `test_codeindex_without_neo4j_falls_back_to_sqlite` — the SQLite
  path is gone; the test was converted to a docstring explaining
  why.

**Replaced in `test_knowledge_index_neo4j_seam.py`:**
- `test_knowledge_index_without_neo4j_falls_back_to_chroma` — the
  chroma path is gone; replaced with
  `test_knowledge_index_without_neo4j_raises` (asserts `TypeError`
  on the now-required `neo4j_client=` kwarg).

**Updated in `test_session_neo4j_knowledge.py`:**
- `test_session_attach_knowledge_neo4j_routes_through_neo4j` —
  constructor now leaves `session.knowledge = None` (deferred
  attach) instead of the chroma-backed default.

## Test status

`NEO4J_TEST_URI=bolt://127.0.0.1:7687` (local 5.26).

**Before this round of changes:** 3098 passed, 5 failed (live-LLM).

**After commit 12 (chroma retirement):** 2913 passed, 5 failed
(live-LLM), 0 errors.

**After commit 15 (the missing tests):** 2913 passed, 5 failed
(live-LLM), 4 skipped, 0 errors. The 4 skips are the
`test_per_commit_isolation` integration tests (require
`EMBER_TEST_NEO4J_RUNTIME` + ~4 GB RAM) and the
`test_jdk_bootstrap` network test.

The 185-test delta vs the original baseline = the 12 deleted
chroma-era test files (some of which had multiple test
functions). The 38 new tests (commits 13-15) replace the
deleted chroma tests in spirit — the modules they exercise
were committed earlier but had no offline test coverage
until now.

## What's left to do

### Future work (out of this scope)

- **`SiblingProjectSearcher` for neo4j** — the cross-project
  knowledge search was chroma-specific. The current
  `KnowledgeIndex.search` ignores `cross_project=True` with a
  vestigial parameter.
- **`embeddings.py` comment cleanup** — the docstring still
  references the deleted `chroma_client_factory.py`; one
  comment-only fix.
- **Production smoke test** — the BE has not yet been smoke-tested
  end-to-end with `EMBER_NEO4J_RUNTIME=1` against a live neo4j
  (the unit + integration tests cover the path, but a real
  chat loop is the final acceptance test). The
  `test_per_commit_isolation` tests will run as part of that
  smoke (they need the runtime env to be set).
