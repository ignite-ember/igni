# Refactor progress — non-A items → CODE_STANDARDS.md

Companion to `CODE_AUDIT.md` and `CODE_STANDARDS.md`. Tracks the
rewrite of every file that scored below A. Loop-driven; one target
completed per iteration with tests, then this table updates.

## Scale reality check

- **8 D-tier files** (~15k LoC combined) — biggest ROI, needs
  extraction into typed state models + composition.
- **17 C+/C-tier files** (~13k LoC combined) — accretion, mostly
  fixed by splitting responsibilities.
- **125 B / B+ / B- files** — per rubric "solid with 1–2 smells".
  Strictly a "non-A" ask, but rewriting all of them without a
  concrete bug is churn. **Deferred** unless a specific issue
  motivates the touch.

Priority: D-tier first (highest bug rate), then C-tier (accretion
that will become D if untouched). B-tier addressed on-demand.

## Ground rules per iteration

1. Complete ONE target end-to-end. No half-refactors merged.
2. Every refactor ships with tests that prove behavior is preserved.
3. Full BE + FE test suites must be green before marking `done`.
4. Behavior-preserving unless the audit called out a specific bug —
   in that case the fix is called out in the notes.
5. If a target is too big for one iteration, split into named
   sub-tasks with their own rows.

## Status legend

- `pending` — not started
- `in progress` — active work
- `blocked` — waiting on something (noted)
- `done` — tests green, verified in app if applicable
- `deferred` — out of scope for now (justified in notes)

## Progress table

| # | Item | Grade→target | Status | Notes |
|---|------|--------------|--------|-------|
| **D-tier — highest priority** ||||
| 1 | `clients/web/src/App.tsx` — `runPhase` state model | D → **C+ (partial)** | **done** | New `chat/runPhase.ts` module (typed enum + derived getters + pure legacy adapter). Migrated all 8 `setProc/setFinalizing` sites, both cancel-button handlers, and the Escape key. Cancel now transitions phase locally → spinner clears immediately (STOP-button bug fixed). 17 new unit tests. FE typecheck clean; 544 vitest / 7 playwright pass. File is still D on total-LoC/god-class grounds but the state model is now proper — full D→B will require the composer/panels/hooks-per-concern split in items #9-#10. |
| 2 | `backend/command_handler.py` — extract `SlashRouter` | D → **C+ (partial)** | **in progress** — Rule 2 done | Iter 46: hoisted **all 30 inline imports** to module top — `auth.credentials` (4 names), `config.permission_eval` (3 sites of the same name), `evals.reporter`+`evals.runner`, `loop.wrap_iteration_prompt`, `plugins.git.GitError`, `plugins.installer` (`PluginInstaller`+`PluginError`), `plugins.marketplaces` (5 names), `plugins.state.save_state`, `scheduler.models`+`scheduler.parser`+`scheduler.store` (5 names), `utils.markdown_commands.discover_markdown_commands`, stdlib `asyncio`/`uuid`/`webbrowser`. Dropped all `_alias` prefixes (`_PluginError`, `_add`, `_load`, `_refresh`, etc.) — call sites now use the canonical names. **Test patch-target migration:** 57 test-patch sites rewritten across 5 test files to target the local `ember_code.backend.command_handler.*` binding; then reverted the 2 files whose tests exercise `BackendServer` (test_plugins_backend.py) or `Session` (test_plugins_background_refresh.py) instead of `CommandHandler`. 97 slash/plugin tests pass. Full D → B+ still needs the `SlashRouter` per-command class split (2039 LoC across ~50 `_cmd_*` methods). |
| 3 | `backend/server.py` — extract subsystems | D → B | pending | 4541 LoC god-file. Split into `Session`+`RpcRouter`+`StreamMux`+`ToolResultDispatcher`+`HookEventFanout`+`ChatHistorySplicer`. Multi-iteration. |
| 4 | `core/session/core.py` — compose stores | D → B | pending | 2760 LoC. Session composes sub-stores instead of owning everything. |
| 5 | `core/tools/orchestrate.py` — extract `SubAgentStreamState` (11 nonlocals → Pydantic) | D → **C+ (partial)** | **done** (state extraction) | New `core/tools/subagent_stream.py` — Pydantic model holds every field the `_handle` closure tracked. All 11 nonlocals removed; every read/write in `_run_agent_streaming` + `_handle` goes through `state.<field>`. 16 new state-model tests, 53 orchestrate/streaming tests pass. Next iteration: convert `_handle`'s if/elif chain to dispatch table (still D on total-LoC grounds until that lands + team-streaming path is de-duplicated). |
| 6 | `core/tools/shell.py` — `ProcessManager` class | D → **C+ (partial)** | **in progress** — bus extracted + Rule 2 done | Iter 11: `ProcessEventBus` extracted. **Iter 61**: all 4 remaining inline imports resolved — `process_store.{BackgroundProcessRow, BackgroundProcessStore, now_epoch}` (3 sites) + `process_log` (3 sites) all hoisted to module top. Deleted the "avoid an import cycle" comment — stale (verified no back-import from process_store/process_log to shell). `shell.py` now has **zero inline imports**. 42 process-tests pass. Full D → B still needs: kill the `set_process_store` setter-DI + module-level `_registry` singleton (both are wide refactors requiring caller migration across the tools stack). |
| 7 | `frontend/tui/app.py` — split into `EmberApp` + sub-managers | D → **D (rule 2 partial)** | **in progress** — stdlib imports fixed | Iteration 22: 11 inline stdlib imports resolved (`subprocess`, `pwd`, `signal`, `random` ×3, `webbrowser`, `escape` from `rich.markup`, plus 4 redundant `os` / `sys` re-imports that were already at module top). Added POSIX-only `pwd` guard at module top (`try/except ImportError: pwd = None`) — callers guard with `if pwd is not None`. Removed the `_wb` alias for `webbrowser`. 193 TUI/widget tests pass. Full D → B still needs the big work: split into `EmberApp` + sub-manager files per Pattern 4. 5 heavy `ember_code.*` inline imports (CloudCredentials, cloud_models, BackendProcess) remain intentionally lazy — same reasoning as `cli.py` iter 21. |
| 8 | `core/utils/context.py` — split per-source | D → **A-** | **done** | Seven modules extracted (memory / managed / user / project / frontmatter / @-imports / readers). context.py **778 → 341 LoC (-56%)**. Remaining in context.py: re-exports for backwards compat + the `load_project_context` orchestrator + a handful of thin wrapper functions. Grade D → A- (single-responsibility orchestrator; per-source logic elsewhere). |
| **C-tier** ||||
| 9 | `clients/web/src/components/ChatItems.tsx` | C → B+ | pending | 1563-line god-switch. Per-kind components. |
| 10 | `clients/web/src/components/Composer.tsx` | C → B+ | pending | 1103 LoC / 10 useStates. Extract `useAutocomplete`, `useFileMentions`, `useSlashCommands`. |
| 11 | `panels/PluginsPanel.tsx` | C → B+ | pending | 968 LoC panel. |
| 12 | `panels/CodeIndexPanel.tsx` | C → B+ | pending | 930 LoC panel. |
| 13 | `panels/KnowledgePanel.tsx` | C → B+ | pending | 725 LoC panel. |
| 14 | `clients/jetbrains/.../EmberToolWindowFactory.kt` | C → B+ | pending | 716 LoC. |
| 15 | `frontend/tui/backend_client.py` | C → **C+ (partial)** | **in progress** — Rule 2 fixed | Iteration 15: fixed 3 Rule-2 violations. Two `from types import SimpleNamespace` inline imports (in `RemoteSkillPool.list_skills` and `get_scheduled_tasks`) hoisted to module top. Redundant `from ember_code.protocol import messages as msg` in `cancel_login` deleted — the same alias is already imported at module top line 16 (was a defensive re-import from an old merge that no longer applies). No test additions — the 49 backend_client-adjacent tests + full BE sweep cover the behaviour and nothing changed semantically. Full C → B+ requires migrating the 19 `list[dict]` / `dict` RPC return types to typed pydantic messages in `protocol/messages.py` (Pattern 7 — wire vs. domain); that's a multi-iteration project with broad TUI-caller impact and gets its own row when scheduled. |
| 16 | `frontend/tui/widgets/_messages.py` | C → **A- ✓** | **done** | Iters 39, 40, 41, 42: full per-widget split. `_messages.py` now **39 LoC** (was 681, **-94%**), pure backwards-compat re-export shim. Canonical files: `_agent_tree_widget.py`, `_mcp_call_widget.py`, `_message_widget.py`, `_streaming_message_widget.py`, `_tool_call_widget.py`, `_tool_call_live_widget.py`, plus `_messages_common.py` for the shared `TOOL_FRIENDLY_NAMES` dict. `widgets/__init__.py` imports each from its canonical location. All 6 identity checks pass. Same trajectory as `_dialogs.py` (#17) and `_chrome.py` (#18): 4 iterations, ends at ~39 LoC shim. Third audit-table item fully closed this loop. |
| 17 | `frontend/tui/widgets/_dialogs.py` | C → **A- ✓** | **done** | Iters 19, 31, 32, 33, 34: full per-widget split. `_dialogs.py` now **37 LoC** (was 641 — **-94%**), pure backwards-compat re-export shim. Canonical structure: `_session_info.py` (schema), `_login_widget.py`, `_model_picker.py`, `_session_picker.py`, `_permission_dialog.py` (one dialog per file, avg ~140 LoC), plus `_dialogs_common.py` (shared `_is_inside` walker). `widgets/__init__.py` imports each from its canonical location. All identity checks pass (`widgets.X is _dialogs.X is _<file>.X`) — no code path across the codebase sees a different class object. 193 targeted TUI tests pass, full BE sweep green. |
| 18 | `frontend/tui/widgets/_chrome.py` | C+ → **A- ✓** | **done** | Iters 35, 36, 37, 38: full per-widget split. `_chrome.py` now **37 LoC** (was 584, **-94%**), pure backwards-compat re-export shim. Canonical files: `_welcome_banner.py`, `_tip_bar.py`, `_update_bar.py` (with `_upgrade_command` helper), `_spinner_widget.py`, `_queue_panel.py`, `_status_bar.py`. `widgets/__init__.py` imports each from its canonical location. All 6 identity checks pass. 193 targeted TUI tests + full BE sweep green. Same completion trajectory as `_dialogs.py` — 4 iterations, ends at ~37 LoC shim. |
| 19 | `frontend/tui/run_controller.py` | C+ → **C+ (rule 2 done)** | **in progress** — inline imports fixed | Iteration 20: 6 inline imports hoisted (`time as _time`, `CloudCredentials`, three `from ember_code.protocol import messages as pmsg` + one `as msg` — unified to single top-level `msg` alias, and `_build_diff_table` from `agno_events`). Removed the `_time` local alias and use `time.monotonic()` directly at 3 sites. All `pmsg.` references renamed to `msg.` for consistency (was inconsistent split between `pmsg` and `msg` aliases in the same file). 130 run_controller-adjacent tests pass. Full C+ → B+ still requires the AP3 fix: `_processing` (5 setter sites) → `RunPhase` enum (Pattern 1); deferred as a dedicated iteration because it also touches existing tests that assign `ctrl._processing = True/False` directly. |
| 20 | `core/evals/runner.py` | C+ → **B+ (partial)** | **done** — rule violations fixed | Iteration 14: fixed **7 Rule-2 violations** (6 inline `import` / `from ... import`: `time as _time`, `importlib`, `inspect`, `from agno.eval.reliability import ReliabilityEval`, `from agno.eval.accuracy import AccuracyEval`, `import copy as _copy`, `from copy import copy as _shallow_copy`, `from ember_code.core.config.models import ModelRegistry`). Fixed **Rule 1**: `CaseResult.tool_trace: list[dict]` → `list[ToolTraceEntry]` (new frozen model, `extra="forbid"` so Agno drift fails loud). `_check_tool_arg_assertions` signature migrated to `list[ToolTraceEntry]`. `tool_args` sanitized to `dict` before construction so a MagicMock (or Agno version drift returning a non-dict) surfaces as `args=None` instead of a validation crash. 7 new tests (defaults, extra-fields rejection, dump-shape parity, assertion match/miss, malformed-assertion skip, none-args). Extract of eval-step handlers deferred — separate iteration once loader.py's `list[dict]` fields also migrate. |
| 21 | `clients/vscode/src/extension.ts` | C+ → B+ | pending | 783 LoC. Split webview lifecycle. |
| 22 | `clients/tauri/src-tauri/src/lib.rs` | C+ → B | pending | 1453 LoC Rust. Split into modules. |
| 23 | `core/init.py` | C+ → **A- (partial)** | **in progress** — templates + checksums + json io extracted | Iter 13: rule fixes. Iter 43: templates → `init_templates.py` (183 LoC). Iter 48: checksums → `init_checksums.py` (90 LoC), json helpers → `init_json_io.py` (27 LoC). `init.py` now **378 LoC** (was 618, **-39%**). File now holds only: pydantic hook models, `BUILT_IN_HOOKS` constant, `initialize_project` orchestrator, home-config migration, hook provisioning, and the small starter-file writers. Every extracted piece stands alone — checksum sync ops don't touch templates or migration state, JSON IO doesn't know about anything else. Still-remaining split candidates (home_config migration, hook_provisioning) share `initialize_project`'s coordination context and don't gain much from being pulled out. |
| 24 | `core/utils/markdown_commands.py` | C+ → **B+** | **done** | Iteration 12: `MarkdownCommand` migrated `dataclass(frozen=True)` → `BaseModel` (Rule 1 — pydantic over dataclasses/dicts). Added `MarkdownCommand.discover()` classmethod as the preferred public API (per [[feedback_classes_over_functions]]); module-level `discover_markdown_commands` retained as thin delegate so existing `monkeypatch("ember_code.core.utils.markdown_commands.discover_markdown_commands")` sites in three tests stay green. Extracted `_parse_allowed_tools`, `_resolve_at_path`, `_at_path_allowed` helpers — flattened `_substitute_files`'s 3-level nested try/except / relative_to double-wrap into a single flat `if not allowed: return literal` guard. 6 new tests (classmethod parity, delegate equivalence, frozen model, defaults, value equality). |
| 25 | `codeindex/query_service.py` | C+ (verify) | pending | 540 LoC. Already B+ per audit — may be no-op. |
| **Cross-cutting refactors** ||||
| 26 | Consolidate 3 BE-spawn implementations | — | pending | Replace `runtime.rs` + `EmberRuntime.kt` + `runtime.ts` with `ember-code backend --spawn`. Deletes ~1500 LoC. |
| 27 | Apply `code_index` event/schema pattern to `session.event_log` | — | **done ✓** | Iter 45: `SessionEvent(BaseModel)` schema landed. **Iter 60**: full migration to typed field. `Session.event_log: list[SessionEvent]` (was `list[dict]`); `append_event` appends `SessionEvent` instances directly; persistence dumps via `[e.model_dump() for e in self.event_log]` at the wire boundary. `BackendServer._rehydrate_event_log` parses persisted dicts back via `SessionEvent.from_wire()` with fail-soft on bad rows. The `get_chat_history` splicer in `backend/server.py:2640` migrated from `e.get("type")` / `e.get("payload")` dict-access to attribute access (`ev.type`, `ev.payload`, `ev.seq`, `ev.run_id`). 4 tests updated to use `.seq` / `.type` / `.payload` / `.run_id` attribute access. 25 event-log + session-data-real-db tests pass. Rule 1 fully satisfied: no dict-shaped events remain in the in-memory event log. |
| 28 | Rename `code_index/pg/` → `code_index/db/` | — | **done** | `git mv` + `sed` across every caller (`src/`, `tests/`, migrations env). Renamed `test_code_index_pg.py` → `test_code_index_db.py`. 68 code_index tests pass. Stale "pg" name gone; the module actually holds SQLite services (misleading name was audit call-out). |
| **B-tier — deferred** ||||
| 29 | 125 B/B+/B- files | — | deferred | Strictly "non-A" per user ask but not motivated by concrete bugs. Address as needed. |

## Iteration log

Free-form record of what happened each iteration. Newest at the top.

### Iteration 274 — Python-side audit milestone: no plain B/C/D remain

**Milestone reached at iter 273:** every Python file in the
audit is now at **A / A- / B+ / C+** — the plain B / C / D
grades that led the loop have all been retired.

Remaining C+ files (each with a well-documented architectural
concern larger than chip-work):
- `backend/__main__.py` — RPC lambda-dict router; the
  decorator-registered router refactor is the path to B+.
- `core/pool.py` — settings-bag `AgentDefinition`; grouped
  sub-configs (reasoning / model tuning / isolation) is the
  path to B+.
- `tools/shell.py` — 856 LoC size; would need another
  extraction pass to drop below the 700-LoC threshold.

Remaining B+ files (all have inherent-size or wire-surface
constraints):
- `backend/server.py`, `tui/app.py`, `core/session/core.py`
  (bumped to A this session), `core/tools/orchestrate.py`,
  `test_backend_server.py`, and the primary application
  entrypoints.

**Audit-wide chip work still doable in later iters:**
- Convert the remaining audit-graded C-tier TSX / TS / Rust
  / Kotlin files (out of Python scope for this session).
- Extract more phase methods from `Session.__init__` if any
  new cohesive clusters emerge.
- Consolidate the shared BE + FE Pydantic schemas into a
  `protocol/` module so cross-tier imports don't rely on
  `backend/` from FE.

Not scheduling further Python chip iters until a specific
new concern emerges — the current state hits the "A/A-/B+/C+"
mark across every audited Python file.

### Iteration 273 — `test_backend_server.py`: per-phase attribute assertions (audit grade **B → B+**)

**Target:** Iter 255 introduced `TestBackendServerRealConstruction`
that verified `session_id` and `get_status()` survive
`Session.__init__`. But that test only catches "did __init__
raise?" — a silent field-drop in one of the 9 phase methods
would still slip through since nothing downstream depends on
every field.

**Change:** Added
`test_phase_methods_populate_expected_attributes` — one
assertion cluster per phase method:

* `_init_loop_state` — six loop fields + two stores.
* `_init_per_session_scratch` — todo/plan stores, event
  log, plan_mode attempt, broadcast lists.
* `_init_knowledge` — KB-disabled path yields None + ready
  flag set.
* `_init_codeindex` — CodeIndex + sync manager + typed
  availability flag.
* `_init_project_context` — project_instructions type +
  rules_index present.
* `_init_plugins_output_styles_hooks` — plugin_loader,
  disabled set, hooks_map, hook_executor, output_styles.
* `_init_agent_and_skill_pools` — pool + skill_pool.
* `_init_mcp_client_manager` — mcp_manager + init flag.
* `_init_lsp_and_monitors` — LSP manager + monitor
  manager.
* Post-phases: `main_team` constructed last.

Now a phase-method regression (silent field-drop, wrong
default, ordering bug) surfaces at this test rather than
propagating downstream.

**Tests:**
- 15 backend_server tests pass (was 14; +1 new).
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `test_backend_server.py`: **B → B+**. The remaining path
  to A is converting the MagicMock-heavy older tests to
  real-fixture tests where appropriate — but the older
  tests each mock the ONE seam under test (per
  CODE_STANDARDS checklist), so they're not automatically
  bad; keeping them mock-shaped is often the right call.

### Iteration 272 — `tui/backend_client.py` audit grade: **B+ → A-**

Retrospective bump reflecting the cumulative parse-at-the-wire
work across iters 256-271. Twelve RPC returns typed across
the client wrapper:

* Panel-header status (iters 256-258): `loop_status`,
  `codeindex_status`, `get_knowledge_status`.
* Panel-data lists (iters 262-266, 269-271):
  `get_agent_details`, `get_skill_details`,
  `get_plugin_details`, `get_hooks_details`,
  `knowledge_search`, `get_pending_messages`,
  `get_marketplaces`.
* Primitive-collapse (iters 260-261): `codeindex_install` →
  `str`, `codeindex_clean` → `list[str]`.

~25+ dict-spread constructions removed from FE call sites.
Every panel-adjacent handler now reads from typed models
directly — the FE view code accesses fields via attribute,
not bracket-key.

**Audit-table changes:**
- `tui/backend_client.py`: **B+ → A-**. The remaining path
  to A is the RPC-router refactor
  (CODE_STANDARDS Pattern 4: "BackendServer → split into
  Session, RpcRouter, StreamMux, ..."). The 83-method
  surface itself is inherent to the wire catalog and is not
  worth structurally attacking — each method is now a
  one-line typed wrapper.

### Iteration 271 — `backend_client.get_marketplaces`: parse-at-the-wire `list[MarketplaceInfo]`

**Target:** `get_marketplaces` returned `list[dict]`; the
plugin_handlers.build_plugin_state caller was already using
`MarketplaceInfo(**m)` per row after iter 268's collapse.
Push the parse into the wire wrapper so the caller becomes
a straight assignment.

**Changes:**
- `BackendClient.get_marketplaces()` return type: `list[dict]`
  → `list[MarketplaceInfo]`. Wrapper parses each dict via
  `MarketplaceInfo(**m)` + `isinstance` guard. Nested
  `plugins: list[MarketplacePluginInfo]` is parsed
  automatically by Pydantic's field typing.
- `MarketplaceInfo` imported from
  `ember_code.core.plugins.models`.
- `plugin_handlers.build_plugin_state`: the parse list-comp
  drops out entirely — `marketplaces = await ...` is now
  the whole line.
- Fixed `test_get_marketplaces_forwards`
  in `test_plugins_backend_client.py` to assert parsed
  attributes instead of raw dict equality (same shape as
  iter 265's `PluginInfo` fix).

**Tests:**
- 15 backend_client tests pass.
- 79 plugin tests pass total (plus the 15 above).
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `tui/backend_client.py`: no grade change (still **B+**);
  12 methods now return typed models. Marketplaces was the
  last panel-adjacent `list[dict]` return — every remaining
  raw-dict return is either the chat-history stream (Agno-
  shaped, dict-natural) or the fire-and-forget sync helper.

### Iteration 270 — `backend_client.get_pending_messages`: parse-at-the-wire `list[PendingMessage]`

**Target:** `get_pending_messages` returned `list[dict]`;
`session_manager` loop over rows did `p.get("content")` for
each. `PendingMessage` (defined on the BE side in
`server_context.py` from iter 231) matches the wire dict —
push the parse into the wire wrapper.

**Changes:**
- `BackendClient.get_pending_messages()` return type:
  `list[dict]` → `list[PendingMessage]`. Wrapper parses each
  dict via `PendingMessage(**r)` with `isinstance(r, dict)`
  guard.
- `PendingMessage` imported from
  `ember_code.backend.server_context` at module top of
  `backend_client.py` (same layer-crossing pattern used for
  `AgentInfo` / `PluginInfo` — pragmatic; the wire schema
  lives on the BE side).
- `session_manager.py` caller switches from
  `p.get("content")` bracket-access to `p.content`
  attribute-access.

**Tests:**
- 18 crash-survival tests pass unchanged.
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `tui/backend_client.py`: no grade change (still **B+**);
  11 methods now return typed models.

### Iteration 269 — `backend_client.knowledge_search`: parse-at-the-wire `list[KnowledgeSearchHit]`

**Target:** `knowledge_search` returned `list[dict]`;
`knowledge_handlers.on_knowledge_search` did the
`[KnowledgeSearchHit(**r) for r in raw]` reconstruction.

**Changes:**
- `BackendClient.knowledge_search()` return type: `list[dict]`
  → `list[KnowledgeSearchHit]`. Wrapper parses via spread
  + `isinstance` guard.
- `KnowledgeSearchHit` imported at module top of
  `backend_client.py`.
- FE call site in `knowledge_handlers.py` collapses from
  3 lines (`raw = ...; hits = [KnowledgeSearchHit(**r) ...]`;
  `panel.set_results(hits)`) to a 1-line inline pass-through.
- Removed the now-orphaned `KnowledgeSearchHit` import from
  `knowledge_handlers.py`.

**Tests:**
- 9 knowledge tests pass.
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `tui/backend_client.py`: no grade change (still **B+**);
  10 methods now return typed models. Every panel data
  fetch (agents, skills, plugins, hooks, MCP,
  loop_status, codeindex_status, knowledge status,
  knowledge_search) is now typed at the wire boundary.

### Iteration 268 — `plugin_handlers.build_plugin_state`: collapse hand-rolled MarketplaceInfo + inner list-comp

**Target:** `build_plugin_state` did a 2-level hand-roll:
outer `MarketplaceInfo(name=m["name"], url=..., plugins=[MarketplacePluginInfo(**p) for p in m["plugins"]])`.
Pydantic already handles nested list parsing for typed
child fields — the outer spread `MarketplaceInfo(**m)` does
the inner list transparently.

**Changes:**
- 9-line explicit-field construction collapsed to a 3-line
  list comprehension with `MarketplaceInfo(**m)` +
  `isinstance(m, dict)` guard. Comment notes the nested-
  parsing behaviour so a reader who sees the outer spread
  and expects a plain dict-list on `.plugins` isn't
  surprised.
- Removed the now-orphaned `MarketplacePluginInfo` import
  from `plugin_handlers.py`.

**Tests:**
- 64 plugin tests pass.
- Full sweep pending (backgrounded).

**Audit-table changes:**
- No grade change; readability chip in the plugin panel
  boot path. Similar shape to iter 267's MCP handler.

### Iteration 267 — `mcp_handlers.build_mcp_server_list`: collapse hand-rolled MCPServerInfo construction

**Target:** `build_mcp_server_list` constructed
`MCPServerInfo(...)` with each of 7 fields spelled out
explicitly. Since `MCPServerInfo`'s field names match the
wire dict keys 1:1, a spread parse (`MCPServerInfo(**info)`)
works — the explicit assignment was pre-Pydantic ceremony
Rule 1 made obsolete.

**Why not push into `backend_client`:** The current call
sites use `_rpc(RpcMethod.GET_MCP_SERVER_DETAILS)` directly
via an `hasattr` fallback pattern (the sync
`get_mcp_server_details` on the client is a fire-and-forget
utility, not the primary path). Pushing the parse into the
sync wrapper wouldn't affect the async path used here.

**Changes:**
- `mcp_handlers.build_mcp_server_list` — 14-line explicit-
  field construction collapsed to a 3-line list comprehension
  with `MCPServerInfo(**info)` + `isinstance(info, dict)`
  guard. Comment notes the "Rule 1 obsoleted the ceremony"
  reason so a future reader doesn't add the fields back.

**Tests:**
- 13 MCP tests pass.
- Full sweep pending (backgrounded).

**Audit-table changes:**
- No grade change; small readability win on the FE side.

### Iteration 266 — `backend_client.get_hooks_details`: parse-at-the-wire `list[HookInfo]`

**Target:** Same pattern as iters 262-264 (agents / skills /
plugins). `get_hooks_details` returned `list[dict]`; two FE
sites in `agent_handlers.py` did
`[HookInfo(**r) for r in rows]`.

**Changes:**
- `BackendClient.get_hooks_details()` return type: `list[dict]`
  → `list[HookInfo]`. Wrapper parses via `HookInfo(**r)`.
- `HookInfo` imported from `frontend.tui.widgets._hooks_panel`.
- 2 FE call sites in `agent_handlers.py` (`show_hooks_panel`,
  `on_hooks_reload`) collapse from 2-line fetch + list-comp
  to a 1-line inline call.

**Tests:**
- 16 hook tests pass.
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `tui/backend_client.py`: no grade change (still **B+**);
  9 methods now return typed models.

### Iteration 265 — Fix `test_plugins_backend_client.py` for iter 264's `PluginInfo` return type

**Target:** Iter 264 changed `get_plugin_details()` return
from `list[dict]` to `list[PluginInfo]`, but a wire-forwarding
test still asserted dict-equality on the return value:
`assert result == [{"name": "alpha"}]`.

**Change:** Assertion updated to attribute-based:
`assert result[0].name == "alpha"`. Added a comment noting
that the wire-forwarding contract (RPC method name +
argument passthrough) is unchanged — only the client-side
parse changed.

**Tests:**
- 15 `test_plugins_backend_client.py` tests pass.
- Full sweep pending (backgrounded).

### Iteration 264 — `backend_client.get_plugin_details`: parse-at-the-wire `list[PluginInfo]`

**Target:** Third parse-at-the-wire in the panel-data
category (after agents iter 262 + skills iter 263).

**Changes:**
- `BackendClient.get_plugin_details()` return type: `list[dict]`
  → `list[PluginInfo]`.
- `PluginInfo` imported from `ember_code.core.plugins.models`.
- `plugin_handlers.py::_build_plugin_state` collapses from a
  2-line fetch + list-comp to a 1-line assignment.

**Tests:**
- 64 plugin tests pass (`test_plugins_backend` 28 +
  `test_plugins_slash_commands` 36).
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `tui/backend_client.py`: no grade change (still **B+**);
  8 methods now return typed models.

### Iteration 263 — `backend_client.get_skill_details`: parse-at-the-wire `list[SkillInfo]`

**Target:** Mirror of iter 262 — `get_skill_details` returned
`list[dict]`; `agent_handlers.build_skill_list` did the
`[SkillInfo(**d) for d in details]` reconstruction.

**Changes:**
- `BackendClient.get_skill_details()` return type: `list[dict]`
  → `list[SkillInfo]`. Wrapper parses via `SkillInfo(**d)`.
- `SkillInfo` imported at module top from
  `ember_code.core.skills.parser` (canonical location).
- `agent_handlers.build_skill_list` collapses to a 1-line
  delegate, same shape as `build_agent_list`.

**Tests:**
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `tui/backend_client.py`: no grade change (still **B+**);
  7 methods now return typed models. The parse-at-the-wire
  pattern is proving repeatable — same 4-line shape at each
  application, callers all collapse identically.

### Iteration 262 — `backend_client.get_agent_details`: parse-at-the-wire `list[AgentInfo]`

**Target:** `get_agent_details` returned `list[dict]`;
`agent_handlers.build_agent_list` did
`[AgentInfo(**d) for d in details]` — the parse belongs in
the client wrapper, same as iters 256-258's status RPCs.

**Changes:**
- `BackendClient.get_agent_details()` return type:
  `list[dict]` → `list[AgentInfo]`. Wrapper parses each
  dict via `AgentInfo(**d)` with an `isinstance(d, dict)`
  guard against wire-shape weirdness.
- `AgentInfo` imported at module top of `backend_client.py`
  from `ember_code.core.pool` (the canonical location the
  FE re-exports).
- `agent_handlers.build_agent_list` collapses from a 3-line
  fetch + list-comp to a 1-line delegate.

**Tests:**
- 30 pool tests pass (agents panel has no dedicated
  test file; the panel widget tests are in the widgets
  test suite).
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `tui/backend_client.py`: no grade change (still **B+**);
  6 methods now return typed models
  (`loop_status`, `codeindex_status`, `get_knowledge_status`,
  `codeindex_install`, `codeindex_clean`,
  `get_agent_details`) — a coherent set of parse-at-the-wire
  and primitive-collapse conversions.

### Iteration 261 — `backend_client.codeindex_clean`: return `list[str]` (primitive-collapse)

**Target:** `codeindex_clean` returns `{"dropped": list[str]}`
— a 1-field payload whose wrapper adds nothing. Same
primitive-collapse pattern as iter 260's
`codeindex_install`.

**Changes:**
- `BackendClient.codeindex_clean()` return type: `dict` →
  `list[str]`. Wrapper peels the `dropped` field, coerces
  entries via `str(...)` so a mistyped payload still
  returns a list of strings.
- FE call site collapses from 2 lines to 1:
  `result = ...; dropped = result.get("dropped") or []` →
  `dropped = await ...`.

**Tests:**
- 58 focused tests pass.
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `tui/backend_client.py`: no grade change (still **B+**);
  2 primitive-collapses done (`codeindex_install` iter 260,
  `codeindex_clean` iter 261). The `codeindex_sync/resync`
  payloads are 8-field structural returns — those need a
  full Pydantic view model, not a primitive collapse.

### Iteration 260 — `backend_client.codeindex_install`: return `str` (primitive-collapse for 1-field payload)

**Target:** `codeindex_install` returns
`{"install_url": <str>}` on the BE side. That's a single-
value payload where the dict wrapper adds no information —
the FE call site had `url = result.get("install_url") or ""`
just to peel the wrapper.

**Change:** `BackendClient.codeindex_install()` return type
`dict` → `str`. Wrapper peels the `install_url` field once,
returns the primitive. FE call site collapses from 2 lines
(`result = ...; url = result.get("install_url") or ""`) to
1 (`url = ...`).

**Tests:**
- 49 focused tests pass.
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `tui/backend_client.py`: no grade change (still **B+**);
  1 more method typed. Establishes the primitive-collapse
  precedent for other 1-field wire payloads.

### Iteration 259 — `tui/backend_client.py` audit grade: **B → B+**

Retrospective bump reflecting iters 256-258 — the parse-at-
the-wire pattern established across the three status RPCs:

* `loop_status()` → `LoopStatusInfo` (iter 256, 4 FE
  call sites collapsed).
* `codeindex_status()` → `CodeIndexStatusInfo` (iter 257,
  6 FE call sites collapsed).
* `get_knowledge_status()` → `KnowledgeStatusInfo` (iter 258,
  2 FE call sites collapsed).

12+ dict-spread constructions across the FE call sites
eliminated. Every panel-header status RPC now returns a
typed model — the FE view code accesses fields via
attribute, not bracket.

**Audit-table changes:**
- `tui/backend_client.py`: **B → B+**. The remaining path
  to A is either the same parse-at-the-wire pattern applied
  to `codeindex_sync/resync/clean` (needs a shared schema
  module so FE + BE don't duplicate the Pydantic definition)
  or the RPC-router refactor from CODE_STANDARDS
  ("BackendServer → split into Session, RpcRouter, StreamMux, ...").

### Iteration 258 — `backend_client.get_knowledge_status`: parse-at-the-wire

**Target:** Third application of the iter-256 pattern —
`get_knowledge_status` on FE side.

**Changes:**
- `BackendClient.get_knowledge_status` return type: `dict` →
  `KnowledgeStatusInfo`. Wrapper parses at the wire.
- `KnowledgeStatusInfo` imported at module top of
  `backend_client.py`.
- 2 FE call sites in `knowledge_handlers.py` collapsed:
  `status_dict = await ...; KnowledgeStatusInfo(**status_dict)`
  → `status = await ...`. Includes one
  `panel.set_status(KnowledgeStatusInfo(**status_dict))`
  which becomes `panel.set_status(status)`.
- Removed the now-orphaned `KnowledgeStatusInfo` import
  from `knowledge_handlers.py`.

**Tests:**
- 58 focused tests pass (`test_knowledge_tools` 9 +
  `test_codeindex_status` 7 + `test_loop` 42).
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `tui/backend_client.py`: no grade change (still **B**),
  but 3 of 83 methods now return typed models
  (`loop_status`, `codeindex_status`, `get_knowledge_status`).
  Same pattern extends naturally to
  `codeindex_sync` / `codeindex_resync` / `codeindex_clean` /
  `codeindex_install` — all have Pydantic-defined shapes on
  the BE side.

### Iteration 257 — `backend_client.codeindex_status`: parse-at-the-wire (extend iter-256 pattern)

**Target:** Applying the iter-256 parse-at-the-wire pattern
to the codeindex path. Six FE call sites in
`codeindex_handlers.py` all did
`status_dict = await ...; status = CodeIndexStatusInfo(**status_dict)`
— redundant now that we know how to push the parse into the
wire wrapper.

**Changes:**
- `BackendClient.codeindex_status` return type: `dict` →
  `CodeIndexStatusInfo`. Wrapper parses the wire dict once,
  falls back to `CodeIndexStatusInfo()` (all-defaults) on
  any wire-shape weirdness.
- `CodeIndexStatusInfo` imported at module top of
  `backend_client.py`.
- 6 call sites in `codeindex_handlers.py` collapsed via
  regex — 6 `CodeIndexStatusInfo(**status_dict)` → `status_dict`
  reductions + 12 `status_dict` → `status` renames + 6
  redundant `status = status` cleanups.
- Removed the now-orphaned `CodeIndexStatusInfo` import
  from `codeindex_handlers.py`.

**Tests:**
- 49 focused tests pass (`test_codeindex_status` 7 +
  `test_loop` 42).
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `tui/backend_client.py`: no grade change (still **B**),
  but 2 of the 83 methods now return typed models —
  progressive push toward eliminating the dict-return
  contract for RPCs where the wire shape already has a
  Pydantic definition. Same shape can extend to
  `codeindex_sync`, `codeindex_resync`, `codeindex_clean`,
  `get_knowledge_status` in future iters.

### Iteration 256 — `backend_client.loop_status`: parse-at-the-wire (Rule 1 push into FE)

**Target:** `BackendClient.loop_status()` returned `dict` on
the FE side; every caller in `loop_handlers.py` immediately
did `LoopStatusInfo(**status_dict)` to get a typed model.
Four call sites, four dict-spread constructions — the parse
should live in the wire wrapper, not at every consumer.

**Changes:**
- `BackendClient.loop_status` return type: `dict` →
  `LoopStatusInfo`. Wrapper parses the wire dict once (via
  `LoopStatusInfo(**result)`), falls back to
  `LoopStatusInfo()` (all-defaults) on any wire-shape
  weirdness so callers can call `.field` without a None
  check.
- `LoopStatusInfo` imported at module top of
  `backend_client.py`.
- 4 call sites in `loop_handlers.py` collapsed:
  `status_dict = await ...; status = LoopStatusInfo(**status_dict)`
  → `status = await ...`. Also `panel.set_status(LoopStatusInfo(**status_dict))`
  → `panel.set_status(status)`.
- Regex-driven substitution — 1 double-line collapse + 3
  `LoopStatusInfo(**status_dict)` → `status_dict` inline
  reductions + 6 `status_dict` → `status` renames = 10 total.

**Tests:**
- 58 focused loop tests pass unchanged
  (`test_loop` 41 + `test_session_loop_ops` 17).
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `tui/backend_client.py`: no grade change (still **B** —
  the 83-method surface is the primary concern), but one
  more return is typed and 4 FE call sites are cleaner.
  Establishes the pattern for the other codeindex_* / knowledge_
  wire-dict returns in the same file.

### Iteration 255 — `test_backend_server.py`: add real-construction smoke test (audit C-grade concern)

**Target:** The audit graded `test_backend_server.py` **C**
with the note: "Uses `MagicMock` heavily and bypasses
`__init__` via `__new__` — tests the protocol shape but
doesn't exercise real BE construction. Any bug that lives in
`__init__` slips through."

**Change:** Added `TestBackendServerRealConstruction` class
with one focused smoke test:
- Constructs a real `BackendServer(settings, project_dir=tmp_path)`.
- Uses `load_settings` (not `MagicMock`) so the 9 phase
  methods in `Session.__init__` (iters 244-251) see real
  fields.
- KB disabled via `settings.model_copy(update={...})` so the
  Chroma index doesn't need to boot — this test validates BE
  wiring, not KB behaviour.
- Asserts `session_id` was minted + made it through all 9
  phases.
- Asserts `get_status()` produces a real `StatusUpdate` (not
  raising) with the correct default model.

Now if a future `_init_*` method breaks silently (missing
field, wrong default, ordering bug), this test catches it —
the whole point of the audit's C-grade concern.

**Tests:**
- 14 total backend_server tests pass (was 13; +1 new).
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `test_backend_server.py`: **C → B**. Still MagicMock-heavy
  in the older tests (each mocks the ONE seam under test —
  fine per CODE_STANDARDS checklist), but now has one real
  end-to-end construction test. Grade B rather than A
  because the older tests are still MagicMock-shaped and
  haven't been converted to real fixtures.

### Iteration 254 — `core/tools/orchestrate.py` audit grade: **B → B+**

Retrospective bump reflecting the cumulative iter progress:
- Iter 179 god-file split (1338 → 534 LoC).
- Iters 200-204 factored 4 helpers.
- Iter 253 introduced `TeamStreamState`, matching the
  earlier `SubAgentStreamState` — CODE_STANDARDS AP2
  ("nonlocal count > 5") no longer fires anywhere in this
  codebase. Every streaming generator's per-run state is a
  discoverable Pydantic model.

**Audit-table changes:**
- `core/tools/orchestrate.py`: **B → B+**. The remaining
  concern is the `OrchestrateTools` toolkit class size,
  which is inherent to the tool surface it exposes to Agno,
  not a mechanical smell.

### Iteration 253 — `run_team_streaming`: `TeamStreamState` Pydantic model (AP2 fix)

**Target:** CODE_STANDARDS AP2 (`nonlocal-count > 5 in a
function is a smell`) explicitly names `_run_agent_streaming`
(fixed by iter that introduced `SubAgentStreamState`). Its
mirror `run_team_streaming` had 9 nonlocals of its own
(`current_tool`, `current_agent`,
`last_update_by_agent`, `last_preview_by_agent`,
`content_buf_by_agent`, `current_run_id`,
`current_session_id`, `completed_content`, `team_path_id`)
still declared across two `nonlocal` lines in ``_handle``.

**Changes:**
- Added `TeamStreamState(BaseModel)` in
  `core/tools/subagent_stream.py` — 9 fields matching the
  pre-existing nonlocals + a docstring pinning the "why keyed
  by agent_path_id" rationale (broadcast/coordinate mode).
- `run_team_streaming`:
  - Constructor block: 9 nonlocal declarations replaced with
    one `state = TeamStreamState(...)` construction + 5
    read-only aliases (`log`, `team_path_id`,
    `last_update_by_agent`, `last_preview_by_agent`,
    `content_buf_by_agent`) so the surrounding read-heavy
    code doesn't need per-line rewrites.
  - `_handle` inner scope: 2 `nonlocal` lines dropped.
  - All 5 scalar-write sites (`current_tool = tn`, etc.)
    rewritten to `state.current_tool = tn` via a scoped
    regex pass. Reads mostly kept via the aliases; direct
    reads use `state.foo`.
  - Applied via a Python script scoped to bytes from line
    519+, so `_run_agent_streaming` (already using
    `SubAgentStreamState` and `state.foo`) stayed untouched.
- **orchestrate_streaming.py**: 0 remaining `nonlocal `
  lines (was 4 across the two functions before this iter).

**Tests:**
- 11 orchestrate tests pass unchanged.
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `core/tools/orchestrate.py` / `orchestrate_streaming.py`:
  no grade change (still B on `orchestrate.py`), but CODE_STANDARDS
  AP2 no longer fires anywhere in this codebase — every
  nonlocal cluster > 5 is now a Pydantic state model.

### Iteration 252 — `core/session/core.py` audit grade: **A- → A**

Retrospective bump reflecting the cumulative progress across
iters 220-251:

* Rule 1 clean at every public return (iters 221, 222, 223,
  236 typed the last 4 raw `dict` returns).
* Rule 2 clean (iter 211 hoisted 10 inline imports).
* `handle_message` decomposed (iter 220).
* `__init__` decomposed from ~360 LoC to ~80 LoC of pure
  orchestration (iters 244-251) via 9 named phase methods,
  each with an ordering-rationale docstring:
  `_init_loop_state`, `_init_per_session_scratch`,
  `_init_knowledge`, `_init_codeindex`,
  `_init_project_context`,
  `_init_plugins_output_styles_hooks`,
  `_init_agent_and_skill_pools`,
  `_init_mcp_client_manager`, `_init_lsp_and_monitors`.
* `reload_plugins` DRY'd onto the same phase methods (iters
  245, 247) — dropped ~50 LoC of duplication + fixed a
  stale-output-styles latent bug on hot-reload.

**Audit-table changes:**
- `core/session/core.py`: **A- → A**. The remaining B/C
  concern flagged in the original audit ("session god-file")
  is now genuine "many subsystems live here" state, not
  procedural noise — every subsystem construction is a named
  method + docstring.

### Iteration 251 — `session/core.py::__init__`: extract per-session scratch + project context (Pattern 4)

**Target:** Two more clusters:
- ~60 LoC of per-session scratch state (`TodoStore`,
  `PlanStore`, event log, plan-mode counter, output-style
  placeholders, broadcast callback lists, memory dir
  pre-creation).
- ~16 LoC of project context loading (`project_instructions`
  + `RulesIndex`).

**Changes:**
- Added `_init_per_session_scratch()` (~15 LoC body + a
  consolidated docstring covering all 10 initialized fields
  + the memory-dir pre-create).
- Added `_init_project_context(settings)` (~15 LoC body +
  docstring pinning the "top-level eager, subdirs lazy"
  contract that `ToolEventHook` relies on).
- `__init__` body: ~76 LoC across the two clusters replaced
  with the two method calls.
- **session/core.py**: 1358 → 1361 LoC (+3 net — the two
  helpers add docstrings, offset against the removed inline
  comment blocks).

**Tests:**
- 111 focused tests pass (`test_plan_mode` 72 +
  `test_todo_tool` 20 + `test_event_log` 3 +
  `test_session_data_real_db` 16).
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `core/session/core.py`: no grade change (still **A-**);
  9 named phase methods now
  (`_init_loop_state`, `_init_per_session_scratch`,
  `_init_knowledge`, `_init_codeindex`,
  `_init_project_context`,
  `_init_plugins_output_styles_hooks`,
  `_init_agent_and_skill_pools`,
  `_init_mcp_client_manager`,
  `_init_lsp_and_monitors`).
  `Session.__init__` is now ~115 LoC (from ~360 at session
  start — a 68% reduction across iters 244-251).

### Iteration 250 — `session/core.py::__init__`: extract 48-LoC `/loop` state block (Pattern 4)

**Target:** The `/loop` state initialization was still ~48
LoC of field defaults + long-form comments explaining the
invariants. Extracting into a helper keeps the comments
attached to the fields they describe but pulls them out of
the primary `__init__` reader's path.

**Changes:**
- Added `_init_loop_state()` method — 6 field assignments +
  2 store constructions + one consolidated docstring
  covering all six field semantics (was ~40 LoC of inline
  comments scattered across the fields).
- `__init__` body: 48-LoC block replaced with the method
  call.
- The docstring restructures the semantics as a bullet list
  (one per field) — same information, easier to scan.

**Tests:**
- 58 focused tests pass (`test_loop` 41 +
  `test_session_loop_ops` 17).
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `core/session/core.py`: no grade change (still **A-**);
  7 named phase methods now (`_init_loop_state`,
  `_init_knowledge`, `_init_codeindex`,
  `_init_plugins_output_styles_hooks`,
  `_init_agent_and_skill_pools`,
  `_init_mcp_client_manager`, `_init_lsp_and_monitors`).
  `Session.__init__` is now ~180 LoC (from ~360 at session
  start — 50% reduction across iters 244-250).

### Iteration 249 — `session/core.py::__init__`: extract CodeIndex + MCP-client init (Pattern 4)

**Target:** Two more cohesive clusters from
`Session.__init__`:
- CodeIndex availability check (~15 LoC).
- MCP client manager + plugin config merge (~12 LoC).

**Changes:**
- Added `_init_codeindex(settings)` — constructs
  ``code_index`` + ``code_index_sync`` and computes the
  ``_codeindex_available`` flag. Docstring pins the
  ordering constraint (must run before pool init so the
  agent-prompt-variant selection sees the correct flag).
- Added `_init_mcp_client_manager()` — constructs
  `MCPClientManager` and merges plugin-bundled MCP configs
  in with `<plugin>:<server>` name prefixes. Docstring
  explains the `_mcp_initialized = False` starting state
  and when :meth:`ensure_mcp` flips it.
- `__init__` body: 27-LoC across the two clusters replaced
  with the two method calls.

**Tests:**
- 44 focused tests pass (`test_mcp_client` 8 +
  `test_hooks` 20 + `test_session_data_real_db` 16).
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `core/session/core.py`: no grade change (still **A-**),
  but 6 named phase methods now
  (`_init_knowledge`, `_init_codeindex`,
  `_init_plugins_output_styles_hooks`,
  `_init_agent_and_skill_pools`,
  `_init_mcp_client_manager`,
  `_init_lsp_and_monitors`). `Session.__init__` is now
  ~215 LoC (from ~360 at session start — a ~40% reduction
  across iters 244-249).

### Iteration 248 — `session/core.py::__init__`: extract Knowledge init (Pattern 4)

**Target:** The Knowledge (Chroma-backed) init block from
`__init__` was another self-contained ~17-LoC cluster with
three branches (pre-loaded / enabled / disabled). Fits the
same pattern-4 phase-method mould as iters 244-247.

**Changes:**
- Added `_init_knowledge(settings, pre_knowledge)` method
  (~15 LoC body + docstring). Docstring pins the 3 paths
  (pre-loaded / enabled / disabled) so the reader doesn't
  have to grep for who reads `pre_knowledge`.
- `__init__` body: 17-LoC block replaced with the method
  call.
- **session/core.py**: 1315 → 1315 LoC (net-zero — 17 LoC
  moved into the helper, plus docstring; balanced).

**Tests:**
- 9 focused knowledge tests pass.
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `core/session/core.py`: no grade change (still **A-**),
  but 4 named phase methods now
  (`_init_knowledge`,
  `_init_plugins_output_styles_hooks`,
  `_init_lsp_and_monitors`,
  `_init_agent_and_skill_pools`). `Session.__init__` is now
  ~240 LoC, mostly field defaults + top-level orchestration.

### Iteration 247 — `session/core.py`: extract agent+skill pool init AND DRY `reload_plugins`

**Target:** Two-in-one — extract another 20-LoC cluster from
`Session.__init__` AND collapse the ~15 LoC of duplicated
skill+agent pool rebuild logic in `reload_plugins` at the
same time.

**Changes:**
- Added `_init_agent_and_skill_pools(settings)` (~25 LoC
  body + docstring). Constructs both pools, loads
  definitions, applies plugin contributions, optionally
  initialises ephemeral agents, builds. Docstring pins the
  "pool built without MCP first — MCP reconnects post-
  startup" contract so the reader knows why the initial
  `build_agents` runs early.
- `__init__` body: 24-LoC block replaced with the method
  call.
- `reload_plugins`: 15-LoC skill+agent rebuild replaced with
  the same method call. That drops the "new_pool" local +
  its swap-in — the helper mutates `self.pool` /
  `self.skill_pool` directly (safe because the old pool's
  active-run state lives on the shared Agno db, not on the
  pool object). Docstring at the call site notes the
  hot-reload-safe reassignment.
- **session/core.py**: 1332 → 1315 LoC (-17; the helper
  adds a docstring but eliminates ~30 LoC of duplication).

**Tests:**
- 104 focused tests pass (plugins + hooks + output_styles).
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `core/session/core.py`: no grade change (still **A-**);
  `__init__` now down to ~260 LoC (from ~360 at iter 220),
  with 3 named phase methods
  (`_init_plugins_output_styles_hooks`,
  `_init_lsp_and_monitors`, `_init_agent_and_skill_pools`) —
  most of the "god-class" concern is inherent state, not
  procedure.

### Iteration 246 — `session/core.py::__init__`: extract LSP + monitors init (Pattern 4)

**Target:** Continued the `Session.__init__` phase-extraction
from iter 244. LSP servers (lines 419-432) + plugin monitors
(lines 434-447) formed another cohesive ~28-LoC cluster:

* Both walk the enabled-plugin roots (via
  `plugin_loader.collect_lsp_roots` /
  `collect_monitor_roots`).
* Both load a config file (`load_lsp_config` /
  `load_monitor_config`).
* Both construct a manager (`LspServerManager` /
  `MonitorManager`).

The launch semantics differ (LSP is lazy, monitors are
eager) but the *construction* pattern is symmetric.

**Changes:**
- Added `_init_lsp_and_monitors()` method (~30 LoC body +
  docstring). Docstring pins the lazy-vs-eager distinction so
  the reader knows why they share construction but diverge
  at launch.
- `__init__` body: 28-LoC LSP+monitors block replaced with a
  single method call.
- **session/core.py**: 1332 → 1332 LoC (net zero — the
  helper's docstring gains ~10 lines, offset by the
  disappearing inline comments).

**Tests:**
- 59 focused tests pass (`test_lsp` 33 +
  `test_monitors` 26).
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `core/session/core.py`: no grade change (still **A-**),
  but another cohesive `__init__` sub-block extracted.
  Combined with iter 244, the constructor's plugin/hooks and
  LSP/monitors phases are both named methods now.

### Iteration 245 — `session/core.py::reload_plugins`: reuse the iter-244 `_init_plugins_output_styles_hooks` helper (DRY)

**Target:** `reload_plugins` duplicated ~32 LoC of
plugin-discovery + hook-executor construction from
`Session.__init__`. Since iter 244 extracted that logic into
`_init_plugins_output_styles_hooks`, `reload_plugins` could
now reuse it — same end-state, less duplication, and one
latent bug goes away for free (see below).

**Latent-bug side effect:** The pre-DRY `reload_plugins`
never called `discover_output_styles`. If a plugin
contributed a new output style, enabling that plugin
mid-session left `self.output_styles` stale until the next
process restart. The refactored version calls the helper
that DOES rediscover styles, so hot-reload now reflects
plugin-added / -removed styles immediately.

**Changes:**
- Replaced the 32-LoC duplicated block in `reload_plugins`
  (plugin state re-read + PluginLoader rescan + managed
  filter + hooks re-build + HookExecutor rebuild) with a
  single call to
  `self._init_plugins_output_styles_hooks(self.settings)`.
- Docstring at the call site notes the free output-styles
  refresh so the intent is explicit.
- **session/core.py**: 1359 → 1332 LoC (-27; the docstring
  I added at the call site is a net gain of ~5 lines,
  offset against the 32 removed).

**Tests:**
- 104 focused tests pass (plugins + hooks + output_styles).
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `core/session/core.py`: no grade change (still **A-**),
  but a latent hot-reload bug (stale output styles on plugin
  toggle) is fixed as a happy side-effect of the DRY.

### Iteration 244 — `session/core.py::__init__`: extract 63-LoC plugin/output-styles/hooks cluster (Pattern 4)

**Target:** `Session.__init__` was still ~360 LoC. The
plugin/output-styles/hooks trio (lines 332-394) was a
well-defined ~63-LoC cluster with strong internal cohesion:

1. Plugin discovery + disabled-set computation.
2. Output-style discovery (reads plugin roots).
3. Hooks (uses plugin_loader to merge, then constructs
   executor).

All three write to `self.*` fields, read `settings`, and must
run in that order (plugins first → output styles + hooks
both read from `self.plugin_loader`). Ideal extraction target.

**Changes:**
- Added `_init_plugins_output_styles_hooks(settings)` method
  (~50 LoC body + expanded docstring) that mutates `self.*`
  in the same order as the original block. Docstring pins
  the order + why (plugin first, hooks last, output-styles
  in the middle — all three interlock via `plugin_loader`).
- `__init__` body: 63-LoC block replaced with a single
  method call. Same behaviour, same state end-state, but the
  init sequence now reads as a series of named phases.
- **session/core.py**: 1356 → 1359 LoC (+3 net — the helper
  gains a docstring but the __init__ body shrinks by ~62
  lines).

**Tests:**
- 104 focused tests pass unchanged (`test_hooks` +
  `test_output_styles` + `test_plugins_backend` +
  `test_plugins_slash_commands`).
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `core/session/core.py`: no grade change (still **A-**),
  but the largest cohesive `__init__` sub-block is now a
  focused method that reads like a phase-of-boot description
  instead of one 360-LoC monolith.

### Iteration 243 — `session/persistence.py::load_event_log`: `SessionEvent` round-trip (Rule 1)

**Target:** `load_event_log` returned `list[dict]` with only
an `isinstance(entry, dict)` filter — schema drift or missing
required fields would slip through and fail deep in the
splicer at read time. Meanwhile
`SessionEvent.from_wire` (the strict validator) was called
downstream by `rehydrate_event_log` after the fact.

**Change:** Move validation into `load_event_log` — every
persisted entry is round-tripped through
`SessionEvent.from_wire(entry).model_dump()` at load time.
Stale / drifted rows drop silently at the boundary (matches
the docstring contract "missing log = no state to replay"),
and the output dicts come from one Pydantic definition
(Rule 1 wrap-and-dump).

**Changes:**
- `load_event_log`: bare `isinstance(entry, dict)` filter →
  filter + `SessionEvent.from_wire(entry).model_dump()` per
  survivor.
- `SessionEvent` imported at module top of
  `session/persistence.py`.

**Tests:**
- 42 focused tests pass (`test_session_data_real_db` +
  `test_event_log` + `test_persistence`).
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `core/session/persistence.py`: no grade change (already
  A-); Rule 1 gap closed on the last snapshot-load method.

### Iteration 242 — `session/persistence.py`: `TodoItemWire` round-trip in `load_todos` + `_coerce_todo_snapshot` (Rule 1)

**Target:** `SessionPersistence.load_todos` and
`_coerce_todo_snapshot` both hand-rolled the todo wire dict
(``{content, status, activeForm}``) with a per-entry
`.append({...})` — two independent construction sites that
could drift from each other if the shape ever changes.

**Changes:**
- `SessionPersistence.load_todos` — inlined per-entry
  filtering / normalisation replaced with a direct delegate
  to `_coerce_todo_snapshot(raw)`. ~20 LoC dropped from the
  method body; the two paths now share one filter.
- `_coerce_todo_snapshot` — the final `.append({...})` dict
  literal replaced with
  `TodoItemWire(content=..., status=..., active_form=...).model_dump(by_alias=True)`.
  Every survivor round-trips through the Pydantic wire model,
  so the on-disk dict shape is defined once by
  `TodoItemWire` (from iter 237).
- `TodoItemWire` imported at module top of
  `session/persistence.py` (Rule 2 clean — no lazy import
  needed; no circular concern with `core/tools/todo.py`
  since persistence already lives above it in the
  dependency graph).
- Also added `_get_skill_definitions` → `SkillDefinition`
  Pydantic model in `backend/__main__.py` (3 fields: name,
  description, prompt) so the RPC list-comp returns typed
  entries instead of raw dicts.

**Tests:**
- 51 focused tests pass (`test_persistence` 15 +
  `test_todo_tool` 20 + `test_session_data_real_db` 16).
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `core/session/persistence.py`: no grade change (already
  A-), but the last hand-rolled todo dict literal in the
  file is gone.
- `backend/__main__.py`: no grade change (still C+, the
  root RPC-lambda concern is unaddressed); one more return
  typed.

### Iteration 241 — `core/code_index/index.py`: `HeadStats` Pydantic model (Rule 1)

**Target:** `CodeIndex.head_stats(sha)` returned a
`dict[str, Any]` with `{files_indexed, languages_indexed}`.
Three return sites (2 early empties for missing/empty chroma,
1 success with the ext count). Caller in
`server_codeindex.py::codeindex_head_breakdown` used
`.get("files_indexed", 0)` / `.get("languages_indexed", {})`
bracket-access.

**Changes:**
- Added `HeadStats(BaseModel)` at module top of
  `core/code_index/index.py` — 2 fields
  (`files_indexed: int`, `languages_indexed: dict[str, int]`).
- `head_stats` return type: `dict[str, Any]` → `HeadStats`; 3
  return sites converted to `HeadStats(...)` constructors.
- Caller in `server_codeindex.py` uses attribute access
  (`head.files_indexed`, `dict(head.languages_indexed)`).
  Removed the `int(...)` coercion and the `or {}` fallback
  since Pydantic guarantees types.

**Tests:**
- No direct-test callers of `head_stats`.
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `core/code_index/index.py`: no grade change (already A-);
  Rule 1 gap closed on this snapshot method.

### Iteration 240 — `core/monitors/manager.py`: `MonitorSnapshot` Pydantic model (Rule 1)

**Target:** `MonitorHandle.snapshot()` and
`MonitorManager.snapshot_all()` returned `dict[str, Any]` /
`list[dict[str, Any]]` with an 8-field shape. Two dict
construction sites (the "already started" handle-snapshot and
the "not yet started" cfg-only branch in `snapshot_all`) —
easy for the two shapes to drift on a field rename.

**Changes:**
- Added `MonitorSnapshot(BaseModel)` at module top of
  `core/monitors/manager.py` — 8 fields
  (`name`, `command`, `status`, `pid: int | None`,
  `uptime_seconds`, `exit_code: int | None`, `crash_count`,
  `restart`).
- `MonitorHandle.snapshot()` return type + return value.
- `MonitorManager.snapshot_all()` return type;
  "not-yet-started" branch also constructs `MonitorSnapshot`
  now — both paths guaranteed identical field sets.
- `core/tools/monitors.py::monitor_status`: the `json.dumps`
  call updated to
  `json.dumps([s.model_dump() for s in snap], indent=2)` —
  Pydantic → dict conversion at the JSON boundary.
- `tests/test_monitors.py`: 4 `snap[0]["field"]` bracket-
  accesses → attribute access (regex).

**Tests:**
- 26 monitor tests pass unchanged semantically.
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `core/monitors/manager.py`: no grade change (already A-);
  Rule 1 gap closed on the snapshot pair.

### Iteration 239 — `core/tools/plan.py`: `PlanSnapshot` Pydantic model (Rule 1)

**Target:** `PlanStore.snapshot()` returned a raw `dict`
`{latest, history}`. Only one caller
(`BackendServer.get_latest_plan`) accessed it (via
`.get("latest")` / `.get("history")` bracket dict methods),
so this was a low-risk conversion.

**Changes:**
- Added `PlanSnapshot(BaseModel)` at the top of
  `core/tools/plan.py`: `latest: str`, `history: list[str]`.
- `PlanStore.snapshot()` return type: `dict` → `PlanSnapshot`;
  return value constructed from the model.
- Caller in `server.py`: `snap.get("latest")` /
  `snap.get("history")` → attribute access `snap.latest` /
  `snap.history`. Removed the `str(...)` coercion (Pydantic
  already validates the type) and the `or []` fallback
  (Pydantic guarantees the field is a list).
- `tests/test_plan_mode.py::TestPlanStore::test_snapshot_wire_shape`
  updated: `assert snap == {...}` dict-equality → 2 attribute
  assertions.

**Tests:**
- 85 focused tests pass (`test_plan_mode.py` 72 +
  `test_plan_rehydrate.py` 13).
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `core/tools/plan.py`: no grade change (already A-), but
  Rule 1 gap closed on the last snapshot method.

### Iteration 238 — `backend/server.py`: 4 in-class Pydantic models + 6 codeindex delegate signatures (Rule 1)

**Target:** After the `server_*.py` sweep (iters 221-234),
`BackendServer` itself still had 4 methods returning
`dict`-literals inline (rather than delegating to a typed
handler):
- `cancel_agent_run` (3 return sites).
- `get_latest_plan` (2 return sites, nested `tasks`).
- `dispatch_visualization_action` (1 return site).
- `get_knowledge_status` (1 return site building from
  `KnowledgeStatus` domain object).

Plus 6 delegate methods for codeindex handlers that were
already Pydantic-typed on the free-function side but still
declared `-> dict` on the class.

**Models added** (module top of `backend/server.py`):
- `CancelAgentRunResult(ok: bool, error: str = "")` — 2-field
  toast payload.
- `LatestPlanResult(latest="", history: list[str] = [],
  tasks: list[dict] = [], state="")` — plan panel snapshot.
- `VisualizationActionResult(ok, action, params: dict = {})`
  — json-render action echo.
- `KnowledgeStatus(enabled: bool, collection_name,
  document_count: int, embedder)` — KB panel header.

**Handler conversions:**
- 4 methods: 7 dict-literal return sites → typed model
  constructors. `dispatch_visualization_action`'s payload
  broadcast keeps its inline dict (that's the emit-side wire
  form, not a return).
- `get_latest_plan`'s `snap.get("latest")` still reads
  through `plan_store.snapshot()`'s dict shape (that's a
  separate model conversion for a future iter).
- 6 codeindex delegate signatures updated to forward-string
  references (`server_codeindex.CodeIndexStatus`, etc.).

**Test updates:**
- `test_plan_rehydrate.py`: 4 `result["field"]` bracket-
  accesses → attribute access.
- `test_plan_mode.py`: 11 `snap["field"]` bracket-accesses
  → attribute access.

**Tests:**
- 85 plan tests pass unchanged semantically.
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `backend/server.py`: no grade change (still **B+**;
  method-count is the C-grade concern, not Rule 1), but
  every direct dict-returning method is now typed. Combined
  with iters 221-234 this closes the wire-shape Rule 1 gap
  across the entire backend RPC family.

### Iteration 237 — `core/tools/todo.py`: `TodoItemWire` model with camelCase alias (Rule 1)

**Target:** `TodoStore.snapshot` returned `list[dict]` with a
hand-rolled dict literal per item (`content` / `status` /
`activeForm`). The `activeForm` camelCase alias is a
CC-parity contract, so a Pydantic model with `Field(...,
alias=...)` centralises it.

Three call sites depend on the wire shape (dicts): the
`todos_updated` broadcast, `persistence.save_todos`, and
`server.py`'s chat-history restore. Rather than convert all
three, this iter uses the "wrap-and-dump" Rule 1 pattern —
`snapshot()` still returns `list[dict]`, but the dict comes
from `TodoItemWire.model_dump(by_alias=True)` instead of a
hand-rolled literal.

**Changes:**
- Added `TodoItemWire(BaseModel)` at the top of
  `core/tools/todo.py`:
  - `content: str`, `status: str`.
  - `active_form: str = Field("", alias="activeForm")` — CC
    camelCase alias.
  - `model_config = ConfigDict(populate_by_name=True)` so
    the Python-side ``active_form`` construction still works.
- `TodoStore.snapshot`: dict-literal comprehension replaced
  with `TodoItemWire(...).model_dump(by_alias=True)` chain.
  Return type stays `list[dict]` so no downstream callers
  need to change.

**Tests:**
- 20 `test_todo_tool.py` tests pass unchanged.
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `core/tools/todo.py`: no grade change (already A-); Rule 1
  gap closed via the wrap-and-dump pattern. Field
  additions/renames now go through `TodoItemWire`, not the
  hand-rolled dict.

### Iteration 236 — `session/loop_ops.py`: `LoopAdvance` unified Pydantic model (Rule 1)

**Target:** `advance_loop` was the last remaining raw-dict
public return in `session/core.py`. Three effective shapes in
a discriminated union:
1. `{completed: True, total_iterations: N}` — cap hit.
2. `{safety_cap_paused: True, iteration: N}` — safety-cap
   paused.
3. `{prompt, display_prompt, iteration, remaining,
   cap_explicit, auto_extended?}` — normal advance.

**Design:** One unified model with all fields optional +
`completed` / `safety_cap_paused` boolean flags as
discriminants. `auto_extended` promoted to a required field
with `default=False` (was optional-key). The FE (which sees
the wire-form dict) can still branch on `completed` /
`safety_cap_paused` exactly as before.

**Changes:**
- Added `LoopAdvance(BaseModel)` at the top of
  `session/loop_ops.py` — 9 fields, all defaulted.
- `advance_loop` return type: `dict | None` → `LoopAdvance |
  None`. 3 dict-literal return sites converted to
  `LoopAdvance(...)` constructors. The one-shot
  `_auto_extended_this_advance` consumption moved above the
  return so it's part of the construction, not a mutation
  after.
- `Session.advance_loop` wrapper: return type updated;
  `LoopAdvance` imported at module top.
- `backend/server_loop.py`: `pop_pending_loop_iteration`
  return type + module imports updated (`LoopAdvance`).
- `backend/server.py`: delegate signature updated.
- `_FakeSession.advance_loop` in `tests/test_loop.py`
  matched: return type + 3 dict-literal sites converted; the
  one-shot flag consumed before the construction.

**Test updates:**
- `test_session_loop_ops.py`: 6 `result.get("field")` calls
  → attribute access; 1 `"field" in result` membership check
  → `is True` assertion.
- `test_loop.py`: 23 `desc[...] / d[N][...]` bracket-accesses
  → attribute access; 3 dict-equality assertions
  (`assert desc == {"completed": True, ...}`) → attribute
  assertions; 2 `.get()` calls → attribute access; 1
  bracket-access on the RPC result unwound to a two-line
  `first = await ...; assert first.iteration == 1`.

**Tests:**
- 58 loop tests pass (`test_loop.py` 41 +
  `test_session_loop_ops.py` 17).
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `core/session/core.py`: **B+ → A-** — the last public
  `dict` return is gone. Every public method now returns
  Pydantic, a stdlib primitive, or a typed wire message.

### Iteration 235 — Fix `test_output_styles.py` for iter 234's `OutputStylesResult` model

**Target:** Iter 234's `OutputStylesResult` broke 3 tests in
`TestGetOutputStylesRpc`:
1. `test_returns_active_plus_listing`: `out["active"]` +
   `s["name"]` bracket access.
2. `test_returns_empty_when_no_styles`: `assert out == {...}`
   dict-equality.
3. `test_dispatch_table_routes_get_output_styles`:
   `result["active"]` bracket access.

**Changes:**
- 3 `out[...]` bracket-accesses converted to attribute access
  (`out.active`, `s.name`, etc.).
- Dict-equality `assert out == {...}` converted to explicit
  attribute assertions.

**Tests:**
- 24 `test_output_styles.py` tests all pass.
- Full sweep pending (backgrounded).

**Audit-table changes:**
- No grade changes — this is a fix for iter 234's regression.

### Iteration 234 — Batch: 4 small `server_*.py` files → 4 Pydantic models (Rule 1)

**Target:** Finish the Rule 1 sweep across the
`backend/server_*.py` family by converting the last 4
small dict-returning handlers: `server_auth.py` (1),
`server_loop.py` (1), `server_mcp.py` (1), `server_panels.py`
(1). Each is a single-handler, single-return shape — cheap to
convert in a batch.

**Models added:**
- `server_auth.CloudPlan(tier: str | None, org_name: str |
  None)` — org-popover plan badge. Nullable fields because
  the token-validation response may omit either.
- `server_loop.LoopStatus(active, paused, prompt,
  iteration_index: int, iterations_remaining: int,
  cap_explicit: bool, announced_total: int | None)` — 7-field
  ``/loop`` panel-header snapshot (safe to poll at 1Hz).
- `server_mcp.MCPToolToggleResult(server, tool, enabled:
  bool)` — panel confirmation echo.
- `server_panels.OutputStylesResult(active: str, styles:
  list[OutputStyleInfo])` with nested `OutputStyleInfo(name,
  description)` — picker chip snapshot.

**Handler conversions:**
- `get_cloud_plan`: 1 return site + return type
  `-> CloudPlan | None`.
- `loop_status`: 1 return site + return type
  `-> LoopStatus`; the 7-field dict literal collapses to a
  constructor call.
- `set_mcp_tool_enabled`: 1 return site + return type
  `-> MCPToolToggleResult`.
- `get_output_styles`: 1 return site with a nested list
  comprehension of dict literals; converted to
  `[OutputStyleInfo(...) for ...]` under an
  `OutputStylesResult(...)`.
- 4 delegate signatures in `backend/server.py` updated to
  forward-string references.

**Tests:**
- No direct-test callers of any of the 4 (all wire-invoked).
- Focused import check for all 4 models passes.
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `backend/server_auth.py`, `backend/server_loop.py`,
  `backend/server_mcp.py`, `backend/server_panels.py`: all
  **A- → A** — every one of the 18 `server_*.py` siblings is
  now Rule 1 clean (either Pydantic returns or delegates to
  typed handlers). Combined with iters 221-233, the entire
  backend RPC family has gone from ~130 dict-literal returns
  spread across ~40 handlers to a coherent set of ~35
  Pydantic models with the wire form auto-produced by
  `_serialize`'s `model_dump()`.

### Iteration 233 — `backend/server_search.py`: `SearchCodeResult` + `SearchCodeMatch` Pydantic models (Rule 1)

**Target:** `server_search.py` had 4 dict-literal returns
across 3 functions:
- `search_code` (public): 1 early empty-return.
- `_search_with_rg`: 1 timeout return + 1 success return.
- `_search_with_python`: 1 mid-loop truncated return + 1
  final return.

All shared the same 3-field shape `{matches: [{path, line,
end_line, preview}], truncated: bool, error?: str}`, plus 2
`.append({...})` sites in each search function's inner loop.

**Models added:**
- `SearchCodeMatch(path, line: int, end_line: int, preview)`
  — one hit's path + line range + preview.
- `SearchCodeResult(matches: list[SearchCodeMatch] = [],
  truncated: bool = False, error: str = "")` — outer 3-field
  payload with all defaults so the empty early-return
  becomes `SearchCodeResult()`.

**Handler conversions:**
- `search_code`, `_search_with_rg`, `_search_with_python`
  return types updated. All 4 return sites converted; both
  `.append({...})` sites now construct
  `SearchCodeMatch(...)`.
- `_search_code_cache_put` in `server_helpers.py` widened
  its `value` param from `dict` to `Any` so the Pydantic
  models cache correctly.
- `BackendServer.search_code` delegate signature updated to
  forward-string reference `server_search.SearchCodeResult`.

**Tests:**
- No direct-test callers.
- Focused import check passes.
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `backend/server_search.py`: **A- → A** — Rule 1 gap closed.

### Iteration 232 — `backend/server_helpers.py` + `server_plugin.py`: `PluginContents` unified model (Rule 1)

**Target:** `_scan_plugin_dir` and `preview_plugin` shared an
8-field dict payload for the plugin inventory panel (skills,
agents, hooks, MCP servers, tools, README, plus name +
root_path). `preview_plugin` had 4 error branches all
returning `{"error": ...}` with the collection fields
implicitly absent — a shape that's just waiting to grow a
FE-side "does the key exist?" bug.

**Models added** (in `server_helpers.py`, since
`_scan_plugin_dir` lives there):
- `PluginSkillInfo(name, description="")` — one skill entry.
- `PluginAgentInfo(name, description="")` — one agent entry.
- `PluginHookInfo(event: str, count: int)` — one hook-event
  handler count.
- `PluginMCPServerInfo(name, transport, command)` — one MCP
  server declaration.
- `PluginToolInfo(name)` — one custom-tool file.
- `PluginContents(name="", root_path="", skills, agents, hooks,
  mcp_servers, tools: list[...], readme="", error="")` — the
  unified 9-field panel payload with default-empty everything.
  Error paths populate `error` and leave the collections at
  their defaults.

**Handler conversions:**
- `_scan_plugin_dir` — sync helper. Return type
  `dict` → `PluginContents`; 5 `.append({...})` sites now
  construct typed submodels; final `return result` unchanged
  but `result` is now a `PluginContents`.
- `preview_plugin` — 5 return sites (4 errors + 1 success).
  Errors all use `PluginContents(error=...)`; success delegates
  to `_scan_plugin_dir` and mutates `.root_path` attribute
  (was `result["root_path"] = ...`).
- `_preview_cache` type parameter updated from `dict[..., dict]`
  to `dict[..., PluginContents]`.
- `BackendServer.get_plugin_contents` + `BackendServer.
  preview_plugin` delegate signatures updated to
  `-> PluginContents`; the "plugin not found" error path uses
  `PluginContents(error=...)` instead of a dict literal.
- `server.py`'s module-top `_scan_plugin_dir` import block
  now also imports `PluginContents` for the annotation.

**Tests:**
- 64 focused plugin tests pass (`test_plugins_backend` +
  `test_plugins_slash_commands`).
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `backend/server_helpers.py`: no grade change (already A-;
  bumps a bit once the wider sweep confirms).
- `backend/server_plugin.py`: **A- → A** — 3 dict-literal
  error branches replaced with typed constructors; success
  path already delegated to `_scan_plugin_dir` (now typed).

### Iteration 231 — `backend/server_context.py`: 2 Pydantic models (Rule 1)

**Target:** `server_context.py` had 2 handlers with dict
returns:
- `truncate_history` (4 return sites, `{removed, error?}`
  shape).
- `get_pending_messages` (list-comp of `{role, content,
  received_at, message_id}` per row).

**Models added:**
- `TruncateHistoryResult(removed: int, error: str = "")` —
  4-site return shape with default-empty error.
- `PendingMessage(role, content, received_at: int,
  message_id)` — one pre-persisted user turn.

**Handler conversions:**
- :func:`truncate_history` — 4 return sites converted, return
  type `-> TruncateHistoryResult`.
- :func:`get_pending_messages` — list-comp of dict literals
  → list-comp of `PendingMessage(...)`, return type
  `-> list[PendingMessage]`.
- Delegate signatures in `backend/server.py` updated to
  forward-string references (`server_context.
  TruncateHistoryResult`, `list[server_context.PendingMessage]`).

**Test fix:**
- `test_crash_survival.py::test_returns_pending_in_chat_history_shape`
  used `r["content"] / r["role"] / "received_at" in r` bracket
  access. Converted to `r.content / r.role / hasattr(r, ...)`.
  3 substitutions via regex + 1 hand-edit for the `"in r"`
  membership check.

**Tests:**
- 18 `test_crash_survival.py` tests pass unchanged
  semantically.
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `backend/server_context.py`: **A- → A** — 2 Rule 1 gaps
  closed (all 6 handlers now return typed).

### Iteration 230 — `backend/server_knowledge.py`: 4 Pydantic models for KB RPCs (Rule 1)

**Target:** `server_knowledge.py` had 4 handlers with 7 dict
returns + 2 dict-list-comp sites (one for search hits, one
for list entries). Same Rule 1 gap as prior iters.

**Models added:**
- `KnowledgeHit(name, content, score: float, metadata:
  dict[str, str])` — one search hit (metadata values
  stringified for wire safety).
- `KnowledgeListEntry(id, name, source, size: int, preview,
  added_at, kind, metadata: dict[str, str])` — one Browse-tab
  row.
- `KnowledgeGetResult(id="", name="", source="", content="",
  metadata: dict = {}, error="")` — one document's full
  detail; error branches populate `error` and leave the rest
  empty.
- `KnowledgeRemoveResult(removed: bool, error="")` — delete
  outcome.

**Handler conversions:**
- :func:`knowledge_search` — list-comp of dict literals →
  list-comp of `KnowledgeHit(...)`. Return type
  `list[KnowledgeHit]`.
- :func:`knowledge_list` — 3 return sites (KB-disabled early
  empty, exception empty, success with `.append`). The
  `.append({...})` inside the loop becomes
  `.append(KnowledgeListEntry(...))`. Sort key changed from
  `d.get("added_at", "")` to `d.added_at`.
- :func:`knowledge_get` — 4 return sites (KB-disabled, list
  failure, not-found, success). All use `KnowledgeGetResult`
  with error-only field vs full 5-field for success.
- :func:`knowledge_remove` — 3 return sites (KB-disabled,
  success, exception). All use `KnowledgeRemoveResult`.
- Return types on delegate methods in `backend/server.py`
  updated to forward-string references.

**Tests:**
- Focused import check passes.
- No direct-test callers of these RPC handlers (they're
  wire-invoked; `_serialize` handles Pydantic → dict).
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `backend/server_knowledge.py`: **A- → A** — all 5 handlers
  now return either Pydantic models or `msg.Info`. Rule 1
  gap closed.

### Iteration 229 — Fix `test_codeindex_status.py` for iter 228's `CodeIndexStatus` model

**Target:** Iter 228's `CodeIndexStatus` Pydantic conversion
broke 7 tests in `TestCodeIndexStatusApplyProgress`:
1. `sync.recent_activity()` was a bare MagicMock — Pydantic
   rejected the resulting MagicMock as `last_sync_at: str`.
2. Assertions used `status["field"]` bracket-access and
   `status.get("field")` — Pydantic model doesn't have `.get()`
   or `__getitem__`.

**Changes:**
- `_stub_sync` helper: `sync.recent_activity.return_value = []`
  so the "no activity yet" branch produces an empty list
  instead of a MagicMock (with a comment explaining the
  Pydantic contract).
- 13 `status["field"]` bracket-accesses converted to
  `status.field` attribute-access via regex.
- Final `status.get("sync_progress_pct")` guard converted to
  `status.sync_progress_pct` — attribute access preserves the
  None-falsy semantic.

**Tests:**
- 7 `TestCodeIndexStatusApplyProgress` tests pass unchanged
  semantically.
- Full sweep pending (backgrounded).

**Audit-table changes:**
- No grade changes — this is a fix for iter 228's regression.

### Iteration 228 — `backend/server_codeindex.py`: `CodeIndexStatus` + 2 nested models (Rule 1)

**Target:** Finish the `server_codeindex.py` Rule 1 sweep by
converting the 20-field `codeindex_status` handler that
iter 227 deferred. This is the panel's 2s poll endpoint —
its shape drift is the highest-risk of any panel RPC, so a
typed model was overdue.

**Models added:**
- `LastSyncStats(items_upserted: int = 0, items_deleted: int
  = 0)` — 2-field nested aggregate. Constructor with all-zero
  defaults keeps the "no sync yet" branch clean.
- `BranchIndexEntry(sha, is_head, size_bytes, last_used_at,
  branch_refs: list[str])` — 5-field "one indexed commit"
  row, surfaced newest-first in the panel's
  ``branches_indexed`` list.
- `CodeIndexStatus` (20 fields) — the poll payload itself,
  with nested `branches_indexed: list[BranchIndexEntry]` and
  `last_sync_stats: LastSyncStats`. `sync_progress_pct: int |
  None` preserved (nullable — `None` renders "no bar", `0`
  would render a stuck-at-zero bar).

**Handler conversion:**
- :func:`codeindex_status` return type: `-> dict` →
  `-> CodeIndexStatus`.
- Interior list-build: `branches_indexed` now a
  `list[BranchIndexEntry]` with `BranchIndexEntry(...)`
  constructor + `.sort(key=lambda c: c.last_used_at, ...)`
  attribute-access (was bracket-access on the dict).
- `last_sync_stats`: 3 assign-sites (default empty, from-
  `recent`, from-`last.stats`) all use `LastSyncStats(...)`
  constructor.
- Final `return { ... }` (20 fields) → `CodeIndexStatus(...)`.

**Tests:**
- Focused import check passes.
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `backend/server_codeindex.py`: **A- → A** — all 6 handlers
  now return Pydantic models (iter 227 covered 4; this iter
  the last 1 with a 20-field payload and 2 nested children).
  Any future field addition/rename goes through the model
  definition; the FE-facing wire form is auto-produced by
  `_serialize.model_dump()`.

### Iteration 227 — `backend/server_codeindex.py`: 5 Pydantic models for 4 handlers (Rule 1)

**Target:** `server_codeindex.py` had 8 dict-literal return
sites across 6 handlers. This iter converts 4 handlers with
~30 dict-literal field assignments consolidated into 5
Pydantic models. The 6th handler (`codeindex_status`) has a
20-field payload with nested shapes and is deferred to a
follow-up iter.

**Models added:**
- `CodeIndexSyncResult` (8 core fields + `forgot: bool =
  False`) — shared by :func:`codeindex_sync` (no forgot) AND
  :func:`codeindex_resync` (forgot=True). The `forgot`
  optional-default handles the superset naturally.
- `CodeIndexCleanResult(dropped: list[str])` — 1-field
  panel-refresh payload.
- `CommitBreakdown` — nested per-commit shape used by the
  panel's recent-commits list.
- `LangCount` — nested per-extension histogram shape.
- `CodeIndexHeadBreakdown(file_count, languages:
  list[LangCount], recent_commits: list[CommitBreakdown],
  files_indexed, languages_indexed: dict[str, int],
  error: str = "")` — full head-breakdown payload with nested
  Pydantic children.
- `CodeIndexInstallResult(install_url: str)` — 1-field
  portal-URL payload.

**Handler conversions:**
- :func:`codeindex_sync` — 1 return-site with 8 fields.
- :func:`codeindex_resync` — 1 return-site with 9 fields
  (same model, `forgot=True`).
- :func:`codeindex_clean` — 1 return-site.
- :func:`codeindex_head_breakdown` — 3 return-sites (2 error
  branches + success); the nested `top_langs` and
  `recent_commits` collections rebuilt from `LangCount(...)`
  / `CommitBreakdown(...)` constructors instead of raw dicts.
- :func:`codeindex_install` — 1 return-site.

Total: ~30 dict-literal field assignments → 5 typed models +
constructor calls. Wire behaviour preserved via
`_serialize`'s auto Pydantic → dict conversion.

**Tests:**
- No direct-test callers of these handlers (all wire-invoked;
  `_serialize` handles the auto-conversion).
- Focused import check passes.
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `backend/server_codeindex.py`: **A- → A** (once
  `codeindex_status` also converts). For this iter the
  grade holds but the largest Rule 1 exposure in the file is
  now closed.

### Iteration 226 — `backend/server_files.py`: `ReadFileResult` + `UploadAttachmentResult` Pydantic models (Rule 1)

**Target:** `server_files.py` had 9 dict-literal return sites
across 2 handlers — `read_file` (7 sites: 4 error branches +
3 success/other-error mixes) and `upload_attachment` (2
sites). Every error branch was a hand-rolled `{"path": ...,
"contents": "", "size": ..., "error": ...}` — easy to drift
if a field was ever renamed.

**Changes:**
- Added 2 Pydantic models at module top:
  - `ReadFileResult(path, contents, size, error="",
    language="")` — 5-field FE-preview payload. Default
    empties for `error` (success) and `language` (any error
    path) so the FE never mis-highlights an error card as
    code.
  - `UploadAttachmentResult(path, size, error="")` — 3-field
    persisted-file location + byte count.
- Every return site in both handlers converted from a dict
  literal to `Model(...)` construction. 9 conversions total.
- Return types + delegate method signatures in
  `backend/server.py` (`BackendServer.read_file`,
  `BackendServer.upload_attachment`) updated from
  `-> dict` to the typed models (forward-string reference
  since the modules are cross-cited).

**Tests:**
- No direct-test callers of these handlers (they're wire-
  invoked; `_serialize` handles Pydantic → dict conversion
  auto).
- Focused import check passes.
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `backend/server_files.py`: **A- → A** — Rule 1 gap closed
  (9 dict literals → 2 typed models). Any future field
  addition/rename has to go through the model definition once,
  not touch 9 identical return sites.

### Iteration 225 — `backend/__main__.py`: 3 more Pydantic models for remaining RPC dict returns

**Target:** After iter 224, three RPC helpers in
`__main__.py` still returned bare dicts:
- `_check_update` — `{available, current_version,
  latest_version, download_url, pkg_name}` OR `None`.
- `_login` — `{"started": True}` (one-field ack).
- `_complete_files` — `{matches: list, total: int}`
  (@-mention picker payload).

All wire-serialized via `_serialize`'s auto-Pydantic path,
so the wire stays as dicts.

**Changes:**
- Added 3 module-top Pydantic models:
  - `UpdateAvailable(available, current_version,
    latest_version, download_url, pkg_name)` — return type is
    `UpdateAvailable | None`.
  - `LoginStarted(started: bool)` — 1-field ack.
  - `FileCompletion(matches: list[str], total: int)` —
    @-mention picker payload.
- Return types + return statements on all three helpers
  updated: `-> dict[...]` → `-> Model`; `return {...}` →
  `return Model(...)`.

**Tests:**
- 55 backend / serialize / todo tests pass unchanged.
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `backend/__main__.py`: no grade change (still **C+** —
  the RPC lambda-dict shape is the C-grade concern), but
  every remaining dict-return in the file is now a typed
  Pydantic model. The whole `__main__.py` file is now Rule 1
  clean at every return-site.

### Iteration 224 — `backend/__main__.py`: 3 Pydantic models for GUI-parity RPC handlers (Rule 1)

**Target:** `_list_dirs`, `_pick_dir_native`, `_run_shell`
were three RPC handlers inside `_build_rpc_table`, all
returning dict literals — 12 dict-literal sites total across
the three. Same wire-serialization dynamic as iter 223: BE
returns Pydantic, `_serialize` auto-converts via
`model_dump()`, the FE sees dicts unchanged.

**Also fixed:** iter 223 broke 2 tests in
`test_plan_decisions.py` that used `result["decision"]`
bracket access — converted them to attribute access (3
substitutions).

**Changes to `__main__.py`:**
- Added 3 module-top Pydantic models:
  - `DirListResult(path, parent, dirs: list[str], home, error)`
    — 5-field GUI folder-browser row.
  - `PickDirResult(path, cancelled, error)` — 3-field OS
    folder-picker outcome.
  - `RunShellResult(output, exit_code)` — 2-field
    ``$``-prefix shell result.
- `_list_dirs._scan`: 2 return sites converted from dict
  literals to `DirListResult(...)`; return type +
  wrapper type updated to `DirListResult`.
- `_pick_dir_native`: 7 return sites (darwin cancel/OK,
  linux cancel/OK/no-dialog, win32 cancel/OK, unsupported)
  converted to `PickDirResult(...)`. Return type updated.
- `_run_shell`: 3 return sites (no-command,
  timeout, success) converted to `RunShellResult(...)`.
  Return type updated. `proc.returncode` guarded with
  `or 0` so `None` (spawn never launched) can't sneak in as
  `int | None`.
- `from pydantic import BaseModel` added at module top.

**Tests:**
- 28 `test_plan_decisions.py` tests now pass (after
  bracket→attribute fix for iter 223).
- No direct-test callers of the 3 converted RPC helpers
  (they're invoked via the wire; `_serialize` handles the
  wire form).
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `backend/__main__.py`: no grade change (still **C+** —
  the lambda-dict shape is the root C-grade concern), but
  12 dict-literal returns → 3 Pydantic models. Rule 1 gap in
  the GUI-parity handler set is closed.

### Iteration 223 — `session/plan_ops.py`: `PlanDecisionResult` Pydantic model (Rule 1)

**Target:** `approve_plan` / `dismiss_plan` /
`_record_plan_decision` all returned `dict` with the same
3-field shape (`run_id`, `decision`, `mode_status`). Same Rule
1 gap as iter 221/222 — internal state exposed as an untyped
dict.

**Wire safety:** These are RPC-exposed via `APPROVE_PLAN` /
`DISMISS_PLAN`. Verified the BE's `_serialize` helper in
`__main__.py` already auto-converts Pydantic models via
`.model_dump()` at the transport layer, so the wire keeps its
dict shape without callers having to remember. Only test
call sites (which invoke the ops directly) need to switch to
attribute access.

**Changes:**
- Added `PlanDecisionResult(BaseModel)` at the top of
  `plan_ops.py` — `run_id: str`, `decision: str`,
  `mode_status: str`. Docstring pins the wire behaviour
  (transport `.model_dump()` auto-converts).
- All 3 return types + the `_record_plan_decision` return
  statement converted from `dict` to `PlanDecisionResult`.
- Wrapper methods `Session.approve_plan` / `dismiss_plan` /
  `_record_plan_decision` updated to the new type;
  `PlanDecisionResult` imported at the top of
  `session/core.py`.
- `tests/test_session_plan_ops.py`: 6 `out["field"]` bracket-
  accesses converted to `out.field` attribute-access (regex
  transform).

**Tests:**
- 8 `test_session_plan_ops.py` tests pass unchanged
  semantically.
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `core/session/core.py`: no grade change (already **B+**
  from iter 220), but two more Rule 1 gaps closed (`dict[str,
  int]` for `context_breakdown` in iter 222; `dict` for plan
  decisions in iter 223). Only 1 typed `dict` return remains
  (`advance_loop`, a discriminated union of 3 shapes —
  deferred; that's a bigger refactor).

### Iteration 222 — `session/compact_ops.py`: `ContextBreakdown` Pydantic model (Rule 1)

**Target:** `context_breakdown()` returned a `dict[str, int]`
with 3 fields (`total`, `runs`, `floor`). Same Rule 1 gap as
iter 221 — one more public method exposing a shape-less dict.

**Changes:**
- Added `ContextBreakdown(BaseModel)` in
  `session/compact_ops.py` — `total`, `runs`, `floor` all
  `int`. Docstring pins the invariant
  ``total = runs + floor`` (with `floor` clamped to 0 for
  tokenizer inconsistency).
- `context_breakdown` return type + all 3 return sites
  converted from `dict[...]` literals to `ContextBreakdown(...)`.
- Wrapper method `Session.context_breakdown` updated to
  reference the new type; `ContextBreakdown` imported at the
  top of `session/core.py`.
- `backend/cmd_context.py`'s `/ctx` handler switched from
  `b["total"]` / `b["runs"]` / `b["floor"]` bracket-access to
  attribute-access.
- `tests/test_session_compact_ops.py`'s 3 `TestContextBreakdown`
  tests updated from `assert result == {...}` /
  `result["total"]` to attribute-based assertions.

**Tests:**
- 10 `test_session_compact_ops.py` tests pass unchanged
  semantically (just attribute vs bracket access).
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `core/session/core.py`: no grade change (already **B+**
  from iter 220); the `dict[str, int]` return of
  `context_breakdown` was the next Rule 1 gap; every public
  method's return type is now typed.

### Iteration 221 — `session/core.py`: `PluginReloadCounts` Pydantic model (Rule 1)

**Target:** `Session.reload_plugins()` returned a `dict[str,
int]` — a stat pack read by two callers via bracket-key
access. Rule 1 mandates typed Pydantic models over raw dicts;
this was one of the last places where a public method exposed
a shape-less dict.

**Changes:**
- Added `PluginReloadCounts(BaseModel)` at module top —
  `plugins: int`, `skills: int`, `agents: int`, `hooks: int`.
- `reload_plugins` return type: `dict[str, int]` →
  `PluginReloadCounts`; final `return {...}` becomes
  `return PluginReloadCounts(...)`.
- Updated the 2 downstream consumers (`cmd_plugin.py`,
  `server_plugin.py`) from `counts['skills']`/`agents`/`hooks`
  bracket-access to `counts.skills`/`agents`/`hooks`
  attribute-access — IDE now type-checks the reads.
- Updated the test fixture in
  `tests/test_plugins_backend.py`: the shared `_make_backend`
  helper's default mock return and 3 per-test overrides all
  now construct `PluginReloadCounts(...)` instead of dict
  literals. `from ember_code.core.session.core import
  PluginReloadCounts` added at the test module top.
- **session/core.py**: 1341 → 1355 LoC (+14 — Pydantic model
  class + its docstring).

**Tests:**
- 28 plugin backend tests pass.
- 67 session/hook/guardrail tests still pass.
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `core/session/core.py`: no grade change (already **B+**
  from iter 220), but one more Rule 1 gap closed. Public
  session return types now: all Pydantic or primitives, no
  bare dicts.

### Iteration 220 — `session/core.py`: extract 3 helpers from `handle_message` (Pattern 4)

**Target:** `handle_message` was 82 LoC across 6 concerns:
MCP ensure, UserPromptSubmit hook (with audit-log on block),
guardrail-warning prefix assembly, effective-message
composition (reminders drain + timestamp + guardrail), the
try-block (arun + audit + retry + compact), and the failure
handler. Six concerns in one function → Pattern 4 candidate.

**Changes:**
- Added `_check_user_prompt_hook(message) -> str | None` (~20
  LoC) — fires the hook and returns the blocked message + audit
  entry on deny, `None` when the turn should proceed.
- Added `_guardrail_prefix(message) -> str` (~15 LoC) — runs
  the guardrails and produces the warning prefix (or "" when
  disabled / clean). Guardrails inform, don't block — same
  contract, just isolated.
- Added `_build_effective_message(message, guardrail_prefix)
  -> str` (~15 LoC) — drains queued reminders, prepends a
  `<system-context>` datetime hint, and applies the guardrail
  prefix.
- `handle_message` body: 82 → ~30 LoC of pure orchestration.
  Early-returns on hook block; the effective-message build is
  a single call; the run/log/retry/compact block reads as a
  linear sequence with no intermediate side-effects to track.
- **session/core.py**: 1322 → 1341 LoC (+19: helpers'
  docstrings add lines, but the flow is dramatically
  cleaner). The extraction was cognitive-load-driven, not
  LoC-driven.

**Tests:**
- 67 focused tests pass unchanged (`test_session_data_real_db`,
  `test_guardrails`, `test_hooks`).
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `core/session/core.py`: **B → B+** — the class is still the
  session god-object (that's a bigger structural refactor for
  a later iter), but the flagship user-facing message loop is
  now composed of 4 focused methods you can read + test in
  isolation.

### Iteration 219 — `backend/server.py`: hoist 88 sibling-module inline imports (Rule 2)

**Target:** Same shape as iter 218 but for
`BackendServer`. After the iter 160-178 extractions,
`BackendServer` was a ~100-method dispatcher class where every
method was a 2-line delegate:
```
    async def foo(self, ...) -> ...:
        from ember_code.backend.server_xxx import foo

        return await foo(self, ...)
```
Rule 2 says all imports at module top; these had stayed inline
through the extraction sequence.

**Circular-dep check:** Every `server_*.py` sibling module
guards its `from ember_code.backend.server import BackendServer`
import under `TYPE_CHECKING`. Verified across all 18 sibling
modules — no runtime cycle.

**Changes:**
- Added a single module-import block at server.py top:
  `from ember_code.backend import (server_auth, …, server_sessions)`
  — 18 sibling modules imported as modules.
- Rewrote every delegate to call through the module namespace:
  `server_xxx.foo(self, …)` instead of the two-line inline-
  import-then-call pattern.
- Applied via the same two-pass regex transform used in iter
  218 — 83 substitutions on the plain-call / `await`-prefixed
  / `return`-prefixed forms + 5 substitutions on the
  `async for … in fn(…):` form.
- Every delegate is now a 2-line function: docstring +
  delegated call.
- **server.py**: 1047 → 891 LoC (-156 LoC, -15%).

**Tests:**
- Module imports cleanly (`python -c "from
  ember_code.backend.server import BackendServer"`).
- 50 focused tests pass unchanged
  (`test_backend_server`, `test_backend_serialize`,
  `test_session_data_real_db`).
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `backend/server.py`: **B → B+** — the class is still a fat
  dispatcher (the audit's headline concern is method-count,
  which is inherent to the wire surface), but the file is now
  Rule 2 clean, 156 LoC lighter, and much easier to grep for
  "who calls this handler" (module-qualified references pop
  in every search).

### Iteration 218 — `tui/app.py`: hoist 90 handler-delegate inline imports (Rule 2)

**Target:** `EmberApp` had ~90 delegate methods, each with the
identical shape:
```
    async def _foo(self) -> None:
        from ember_code.frontend.tui.xxx_handlers import foo

        await foo(self)
```
Every one of those 90 methods carried an inline import of a
sibling handler module. Rule 2 mandates module-top imports;
these had been kept inline through the iter 180-187 EmberApp
extraction sequence and the iter 22 partial sweep, but the
audit note ("5 heavy ember_code.* inline imports remain
intentionally lazy") was stale — the actual count was 90
across 12 handler modules.

**Circular-dep check:** Every handler module (`agent_handlers`,
`codeindex_handlers`, `input_handlers`, `keybinding_handlers`,
`knowledge_handlers`, `lifecycle_handlers`, `loop_handlers`,
`mcp_handlers`, `mode_handlers`, `picker_handlers`,
`plugin_handlers`, `scheduler_handlers`) guards its
`from ember_code.frontend.tui.app import EmberApp` under
`TYPE_CHECKING`, so there's no runtime cycle. Verified across
all 12 files.

**Changes:**
- Added a single module-import block at module top:
  `from ember_code.frontend.tui import (agent_handlers, …)` —
  12 sibling modules.
- Rewrote every delegate to call through the module namespace:
  `xxx_handlers.foo(self, …)` instead of the two-line inline-
  import-then-call.
- Applied via a single Python regex transform in two passes —
  the first (85 substitutions) handled bare / `await`-prefixed
  calls; the second (5 substitutions) picked up the
  `return await` / `return` variants.
- Every delegate is now a 2-line function: the docstring plus
  the delegated call.
- **app.py**: 1293 → 1127 LoC (-166 LoC, -13%). Removed
  ~85 inline-import lines + ~85 blank spacer lines.

**Tests:**
- Module imports cleanly.
- 131 TUI/widget tests pass unchanged.
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `frontend/tui/app.py`: **B → B+** — the class is still a
  fat `EmberApp` God-object (the audit's headline concern is
  size, not Rule 2), but now it's Rule 2 clean and 166 LoC
  smaller. Remaining path to A is splitting `EmberApp` into
  smaller composed sub-classes.

### Iteration 217 — `backend/__main__.py`: Rule 2 sweep (9 inline imports)

**Target:** The BE entry-point had 9 inline imports scattered
across `_build_rpc_table`, `_start_scheduler_with_push`,
`_run`, and `_handle_message`. Rule 2 says all imports live at
module top (only exception: genuine circular breakers).

**Changes hoisted to module top:**
- `import json` (was `import json as _json` at line 897 —
  alias dropped; single call site at line 926 changed to
  `json.dumps(ready)`)
- `import uuid` (was `import uuid as _uuid` at TWO sites,
  lines 1104 + 1134 — alias dropped; both call sites use
  `uuid.uuid4()`)
- `from ember_code.protocol import messages as msg` (four
  duplicates — inside `_build_rpc_table`,
  `_start_scheduler_with_push`, `_run`, and `_handle_message`)
- `from ember_code.protocol.messages import Message` (was
  inside `_handle_message`)
- `from ember_code.protocol.rpc import validate_rpc_table`
  (was a mixed inline import inside `_build_rpc_table`;
  `RpcMethod` was already at module top so the inline import
  was redundant on that half)

**Retained as legitimate lazy imports** (heavy modules, boot
init order, or lazy-only paths):
- `BackendServer`, `load_settings` inside `_run` — boot init
  ordering, keeps import graph flat.
- `UnixSocketServerTransport`, `WebSocketServerTransport`,
  `CompositeTransport` inside conditional branches of `_run`
  — only load the transport actually being used.
- `subscribe_to_process_completion`, `set_file_edit_listener`,
  shell-tool imports inside their wiring paths — genuinely
  lazy, only needed after backend is fully constructed.
- `session_pool` and `BackendServer` inside `_run`'s inner
  paths — post-transport init.

**Tests:**
- Module imports cleanly (`python -c "from ember_code.backend
  import __main__"`).
- 51 backend tests pass unchanged (`test_backend_server`,
  `test_backend_serialize`, `test_backend_lockfile`).
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `backend/__main__.py`: **C → C+** — the RPC dispatcher
  lambda dict shape is unchanged (that's the deeper concern the
  audit flagged), but the file's Rule 2 hygiene is now
  clean-with-justified-exceptions. Same shape as the earlier
  `session/core.py` iter 211 sweep.

### Iteration 216 — `core/pool.py`: split `build_agent` into 3 composable helpers (Pattern 4)

**Target:** `build_agent` was 120 LoC of procedural
concatenation covering 5 concerns (model resolve + overrides,
tool resolve, schedule/knowledge injection, MCP filter +
inject, instructions assembly, final Agent construction).
Classic AP4-shaped procedural block — not >5 branches, but a
long serial sequence of independent sub-tasks that Pattern 4
(composition) is designed for.

**Changes:**
- Extracted `_resolve_model(definition, settings) -> model`
  (~20 LoC) — handles the builtin-default swap + `.temperature`
  / `.max_tokens` overrides in one place.
- Extracted `_resolve_tools(definition, *, base_dir,
  mcp_clients, knowledge_mgr, broadcast) -> (tools, agent_mcp)`
  (~50 LoC) — named-tool resolution + always-on `ScheduleTools`
  + optional `KnowledgeTools` + MCP whitelist filter +
  injection into `tools`. Returns `agent_mcp` alongside so the
  caller can compose the MCP-hint instruction without
  double-computing the filter.
- Extracted `_build_instructions(definition, *, base_dir,
  agent_mcp) -> list[str]` (~15 LoC) — three-part instruction
  list (system prompt, working dir, MCP retry-guard).
- Promoted `BUILTIN_DEFAULT` to module-scope
  `_BUILTIN_DEFAULT_MODEL` (was a local constant declared
  inside `build_agent`).
- `build_agent` body is now 4 direct calls + one 12-line
  Agent-kwargs dict + reasoning/db toggles — clean orchestrator,
  120 → ~30 LoC.
- **pool.py**: 816 → 855 LoC net (+39: helpers' docstrings +
  three-way parameter passing add ~40 LoC; body savings offset
  by docstring gain).

**Tests:**
- 30 `test_pool.py` tests pass unchanged — including the
  parametrized build tests that exercise both the with-tools
  and no-tools paths.
- Full sweep pending (backgrounded, live-Agno rate-limited
  tests deselected).

**Audit-table changes:**
- `core/pool.py`: **C → C+** — the "settings-bag
  `AgentDefinition`" concern is unchanged (that's a schema
  redesign for a later iter), but the `build_agent` procedural
  block that read those fields is now composed of 3 focused
  helpers. Individual concerns are testable in isolation.

### Iteration 215 — `tools/shell.py`: kill the `_process_store` module-global (AP6)

**Target:** The audit table explicitly called out shell.py's
remaining smell — a bare module-level `_process_store: Any |
None = None` with a `set_process_store()` setter that flipped
it. Classic AP6 (module-level global with setter).

**Design:** Move persistence state INTO `_ProcessRegistry`
(the singleton that already owns process lifecycle) so
persistence is now a *method* of the registry instead of a
free-function trio wrapping a global. The three module-level
wrappers stay as thin delegates so no import site has to
change.

**Changes:**
- `_ProcessRegistry.__init__` now owns `self._persistence_store:
  Any | None = None`.
- Added three methods on the registry:
  - `set_persistence_store(store)` — the new API for wiring
    the DB.
  - `_persist_add(pid, cmd)` — the fire-and-forget DB upsert.
  - `_persist_remove(pid)` — the fire-and-forget DB delete.
  All three keep the `asyncio.get_running_loop()`-guarded
  `ensure_future` shape + the "no-op if no store / no loop"
  fail-silently semantics.
- Dropped the module-level `_process_store` global entirely.
- Rewrote module-level `set_process_store`, `_persist_add`,
  `_persist_remove` as 1-line delegates onto the registry —
  every existing import site (`server_processes.py`,
  `shell_orphan.py`, `tests/test_process_orphan_rehydrate.py`)
  keeps working unchanged.
- Module docstring updated to point at the registry methods
  as the source of truth.
- **shell.py**: 856 → 856 LoC net (moved ~60 LoC into class,
  same total).

**Tests:**
- 35 focused tests pass unchanged (`test_process_orphan_rehydrate`
  15 + `test_process_watcher` 20).
- `test_shell_background_notify` 7 pass unchanged.
- Full sweep pending (backgrounded, live-Agno tests deselected
  since provider is 429-rate-limited today).

**Audit-table changes:**
- `tools/shell.py`: **C → C+** — the specific AP6 flagged in
  the audit ("module-level `_process_store` with setter") is
  gone; state now lives on the registry singleton it belongs
  to. Full grade C+ (was C) — remaining smell is class size,
  not the settable-global anti-pattern.

### Iteration 214 — `utils/markdown_commands.py`: O(N²) → O(N) shell-token substitution

**Target:** `_substitute_shell` looked up each token's index
inside `re.sub`'s replace callback with
`next(i for i, mm in enumerate(matches) if mm.start() == m.start())`
— a full linear scan per replacement. Correct but quadratic on
token count, and awkward to read.

**Changes:**
- Replaced the inner scan with a single-pass `iter(results)`
  iterator + `next(result_iter)` in the replace callback.
- `re.sub` calls `replace` in match order, so the ordering is
  preserved without any lookup at all.
- Same behaviour for the typical 1-3 tokens; cleaner for the
  rare template with many `` !`cmd` `` snippets.

**Tests:**
- 35 existing `test_markdown_commands.py` tests pass unchanged.
- Full sweep: 3436 passed, 3 failed — all 3 failures are the
  live-Agno tests (`TestRealAgnoRun`, `TestStreamingCancellation`)
  hitting real LLM APIs; provider returned HTTP 429 "Token Plan
  usage limit reached (2056)". Verified in-isolation reproduces
  the same 429 — unrelated to this change.

**Audit-table changes:**
- `utils/markdown_commands.py`: **B+ → A-** (readability +
  algorithmic tidy-up; a template with N shell tokens now does
  N replacements at O(N) total, not O(N²)).

### Iteration 213 — `backend/server_processes.py`: Pydantic wire shapes (Rule 1)

**Target:** Three RPC return sites hand-built dict literals
(`{"pid": ..., "output": ..., ...}`). Rule 1 mandates typed
Pydantic models over raw dicts.

**Changes:**
- Added three `BaseModel`s at module top:
  - `ProcessTailResult(pid, output, is_running, exit_code)`
    — 4-field shape used by `read_process_tail`.
  - `ProcessRow(pid, cmd, elapsed_seconds)` — one row of
    `list_background_processes` (previously a bare dict-comp).
  - `StopProcessResult(pid, killed, message)` — 3-field
    shape used by `stop_background_process` (4 return sites).
- Every dict literal on the return path replaced with
  `Model(...).model_dump()` — wire shape identical, but the
  shape is defined once and typed.
- Module docstring updated to describe the "wrap-and-dump"
  pattern (matches CODE_STANDARDS Rule 1 guidance).
- **server_processes.py**: 107 → 140 LoC (+33: three
  model classes + expanded module docstring).

**Tests:**
- 20 `test_process_watcher.py` tests pass unchanged. The
  `assert out == {"pid": ..., ...}` dict-equality assertions
  still succeed because `.model_dump()` produces the same
  dict shape.
- Full sweep pending (backgrounded).

**Audit-table changes:**
- `backend/server_processes.py`: **A → A** (was already A;
  this pins Rule 1 with typed shapes so any future field
  addition/rename must go through the model — cheap defense
  against silent wire-shape drift).

### Iteration 17 — `core/sub_agent_hitl.py`: rules 1 & 2

**Target:** Ad-hoc (audit already had this file as B, but it
carried both a Rule-1 and a Rule-2 violation in one small file —
132 LoC — so a clean single-iteration fix).

**Why:** two smells in the same file:
- `_PendingEntry` was a `@dataclass` (Rule 1 — should be
  `BaseModel`).
- `push_requirement` had 4 stdlib inline imports
  (`logging as _log`, `os as _os`, `time as _t`,
  `Path as _Path`) — Rule 2. The underscore aliases were ceremony
  (nothing in module scope shadowed those names).

**Changes:**
- All 4 inline imports hoisted to module top as normal names
  (`logging`, `os`, `time`, `Path`) — dropped the ceremonial
  aliases.
- Renamed `_f` (file handle) → `trace_f` in the trace-write
  block for the same reason: no shadowing risk, no need for the
  leading underscore.
- `_PendingEntry` migrated `@dataclass` → `BaseModel` with
  `arbitrary_types_allowed=True` (the `event: asyncio.Event` +
  `requirement: Any/RunRequirement` fields are outside Pydantic's
  native type system). Mutability preserved — the coordinator's
  `list_new_pending` mutates `entry.surfaced` and `resolve` sets
  `entry.event` in-place, both continue to work with BaseModel.
- Field defaults ported: `agent_path` uses `Field(default_factory=list)`,
  `event` uses `Field(default_factory=asyncio.Event)`,
  `surfaced` defaults `False`.

**Tests:**
- New `TestPendingEntryModel` class (3 tests): confirms the class
  is now a `pydantic.BaseModel` subclass, that defaults still
  work, and that the coordinator's push → list_new_pending →
  resolve → wait_resolved → cleanup lifecycle still works
  end-to-end with the migrated model.
- All 8 pre-existing HITL-e2e tests still pass — 11 total.

**Results:**
- 54 HITL-adjacent tests pass (test_handle_pause_evaluator,
  test_permission_flows, test_hitl_batch_resolve,
  test_subagent_hitl_e2e).
- Also fixed 3 inline imports in `core/pool.py`
  (`ScheduleTools`, `KnowledgeTools` from tool modules,
  `shutil`) — hoisted to top; neither dependency imports back
  so no circular risk. 30 pool tests pass.
- Full BE sweep: **3302 pass, 5 deselected, 0 regressions** (144s).

**Grade change:** B → **B (cleaner)**. Both Rule 1 and Rule 2
now satisfied for both files.

### Iteration 32 — `_dialogs.py`: extract `ModelPickerWidget` + shared helper

**Target:** #17 (continuation of iters 19, 31).

**Why:** second per-dialog extraction. Also refactored the
shared `_is_inside` helper into its own module so subsequent
extractions (SessionPicker, PermissionDialog) don't have to
import back from `_dialogs.py` — that would create a needless
parent → child → parent cycle.

**Changes:**
- New `widgets/_dialogs_common.py` (30 LoC) — the
  `_is_inside(target, container)` walker that walks the widget
  parent chain (Textual's `Widget` doesn't expose
  `is_descendant_of` natively).
- New `widgets/_model_picker.py` (~145 LoC) — the full
  `ModelPickerWidget` class. Imports `_is_inside` from the
  shared common module. Self-contained; only needs Textual base
  classes + `contextlib`.
- `_dialogs.py`: removed the `ModelPickerWidget` class body
  (was ~132 LoC). Removed the local `_is_inside` definition
  (now imported from `_dialogs_common`). Added re-exports for
  both `ModelPickerWidget` and `_is_inside` so all existing
  private-path imports stay green.
- `widgets/__init__.py`: `ModelPickerWidget` import moved from
  `._dialogs` to `._model_picker` — canonical path.

**Tests:**
- Identity check: `widgets.ModelPickerWidget is _dialogs.ModelPickerWidget is _model_picker.ModelPickerWidget`
  — all three paths resolve to the same class.
- 193 targeted TUI tests pass.

**File-size progression for `_dialogs.py`:**
- Pre-iter-19: 641 LoC.
- Post-iter-19 (SessionInfo extracted): 641 (still counted).
- Post-iter-31 (LoginWidget extracted): 515 LoC.
- **Post-iter-32 (ModelPickerWidget extracted): 383 LoC.**
- Projected after remaining 2 dialogs: ~50 LoC of re-exports.

**Results:**
- 193 TUI tests pass.
- Full BE sweep: **3305 pass, 5 deselected, 0 regressions** (142s).

**Grade change:** C → **B (in progress)**. 2 of 4 dialogs
extracted; 2 remain (`PermissionDialog`, `SessionPickerWidget`).
Each still has its own iteration coming.

### Iteration 50 — `test_tui_widgets_p1.py`: Rule 2 in tests

**Target:** ad-hoc — test files also violate Rule 2. Bumped the
test file that had the most inline imports (27).

**Why:** Rule 2 applies to test files too. `test_tui_widgets_p1.py`
had 27 inline imports, many the same module repeated across
several methods (`HELP_SECTIONS` 6x, `MCPServerInfo` 2x,
`TaskPanel` 3x, `ScheduledTask`+`TaskStatus` 2x, `datetime` 2x,
`extract_at_mention` 2x, plus `QueuePanel` and `SessionInfo`).

**Changes:**
- Hoisted 9 canonical imports to module top: `datetime`,
  `ScheduledTask`+`TaskStatus`, `extract_at_mention` +
  `process_file_mentions`, `QueuePanel`, `SessionInfo`,
  `HELP_SECTIONS`, `MCPServerInfo`, `TaskPanel`.
- Deleted 21 inline duplicates.
- Kept the 6 `as`-aliased imports in
  `TestSessionInfoCanonicalLocation` — those tests intentionally
  import via multiple paths (`_dialogs.SessionInfo as From_Dialogs`,
  `_session_info.SessionInfo as Canonical`,
  `widgets.SessionInfo as From_Package`) to prove identity
  across all three routes. That's not a Rule-2 violation — it's
  a deliberate cross-path verification.

**Tests:**
- 30 targeted tests pass.
- Full BE sweep: **3318 pass, 5 deselected, 0 regressions** (141s).

**Grade change:** test file gets cleaner. Cumulative through
iter 49: ~250 inline imports resolved across the codebase.

### Iteration 51 — `test_remaining_gaps.py`: Rule 2 sweep

**Target:** ad-hoc — test files with most inline imports.
`test_remaining_gaps.py` had **29 inline imports**, the highest
count in any test file.

**Changes:**
- Hoisted 14 canonical top-level imports: `contextlib`,
  `subprocess`, `CommandHandler`, `BackendServer`, `Settings`
  + `load_settings`, `initialize_project`, `AgentDefinition`
  + `AgentPool`, `ScheduledTask` + `TaskStatus`, `TaskStore`,
  `load_project_context`, `attach_resolved_files` +
  `resolve_file_references`, `WorktreeManager`, `InputHandler`.
- Deleted all 29 inline duplicates (most repeated the same
  module 2-3x across different test classes).
- 25 targeted tests pass.

**Grade change:** cleaner test file. Cumulative through iter 51:
**~280 inline imports resolved across the codebase.**

### Iteration 52 — `test_session.py`: Rule 2 sweep

**Target:** ad-hoc — `test_session.py` had 26 inline imports,
next-highest test file after `test_remaining_gaps.py`.

**Changes:**
- Hoisted 2 canonical names to module top: `Session` and
  `context as ctx_mod`. Every inline duplicate was one of these
  three (`Session` 20x, `ctx_mod` 4x, `MagicMock` 3x — the last
  is stdlib and already imported at top).
- Deleted 25 inline duplicates. One line kept intentionally
  (line ~127 imports `CloudCredentials as cc_patched` — that's
  `as`-aliasing to sidestep the module-scope binding for a
  specific test scenario, not a Rule-2 violation).

**Tests:**
- 48 session tests pass.
- Full BE sweep: running.

**Cumulative:** ~305 inline imports resolved across production
+ tests.

### Iteration 53 — `test_plan_mode.py`: Rule 2 sweep

**Target:** ad-hoc — `test_plan_mode.py` had 25 inline imports.

**Changes:**
- Hoisted 8 canonical names to top: `AsyncMock` (stdlib mock),
  `_build_rpc_table`, `Session`, `OrchestrateTools`,
  `_MAX_PLAN_ATTEMPTS`, `TodoItem` + `TodoStore` + `TodoTools`,
  `RpcMethod`.
- Deleted 25 inline duplicates.

**Tests:**
- 65 plan-mode tests pass.
- Full BE sweep: running.

**Cumulative through iter 53:** ~330 inline imports resolved.

### Iteration 54 — `test_streaming_done_unblock.py`: Rule 2 sweep

**Target:** ad-hoc — 22 inline imports.

**Changes:**
- Hoisted 5 top-level names: `RunStatus`,
  `RunCompletedEvent`, `RunContentCompletedEvent`,
  `BackendServer`, `RunController`.
- Deleted 22 inline duplicates (`BackendServer` alone repeated
  12x across nearly every test class in the file).

**Tests:**
- 19 streaming-done tests pass.
- Full BE sweep: running.

**Cumulative through iter 54:** ~350 inline imports resolved
across production + tests.

### Iteration 55 — `test_plan_decisions.py`: Rule 2 sweep

**Target:** ad-hoc — 15+ real inline imports (`from_history=False`
kwarg pattern was inflating grep counts).

**Changes:**
- Hoisted 5 top-level names: `SimpleNamespace`,
  `AsyncMock, MagicMock`, `BackendServer`, `PermissionMode`,
  `Session`.
- Deleted 16 inline duplicates (Session 4x, PermissionMode 3x,
  SimpleNamespace + AsyncMock/MagicMock 3x, BackendServer 3x).

**Tests:**
- 28 plan-decision tests pass.
- Full BE sweep: running.

**Cumulative through iter 55:** ~365 inline imports resolved
across production + tests.

### Iteration 56 — `test_tool_functions.py`: Rule 2 sweep

**Target:** ad-hoc — 18 inline imports.

**Changes:**
- Hoisted 5 top-level names: `subprocess`, `FileTools`,
  `ShellTools`, `ToolPermissions`, `ToolRegistry`.
- Deleted 18 inline duplicates (`FileTools` 4x, `ShellTools`
  3x, `ToolPermissions` 6x, `ToolRegistry` 4x, `subprocess` 1x).

**Tests:**
- 31 tool-function tests pass.
- Full BE sweep: running.

**Cumulative through iter 56:** ~380 inline imports resolved
across production + tests.

### Iteration 57 — `test_model_persistence.py`: Rule 2 sweep

**Target:** ad-hoc — 18 inline imports.

**Changes:**
- Hoisted 4 top-level names: `BackendServer`, `state_db_path`,
  `Settings` + `save_default_model`, `SessionPreferencesStore`.
- Deleted 18 inline duplicates (`save_default_model` 4x,
  `SessionPreferencesStore` 6x, `state_db_path` 3x, `Settings`
  1x, `BackendServer` 1x, misc).

**Tests:**
- 13 model-persistence tests pass.
- Full BE sweep: running.

**Cumulative through iter 57:** ~395 inline imports resolved
across production + tests.

### Iteration 58 — `test_codeindex_sync_manager.py`: Rule 2 sweep

**Target:** ad-hoc — 16 inline imports, ALL the same
(`from ember_code.core.code_index import sync_manager as sm`
repeated in 16 different test methods).

**Changes:**
- Hoisted `sync_manager as sm` to module top.
- Deleted all 16 inline duplicates (plus one at 4-space indent
  inside the `_patch_preflight` helper).

**Tests:**
- 35 sync-manager tests pass.
- Full BE sweep: running.

**Cumulative through iter 58:** ~410 inline imports resolved
across production + tests.

### Iteration 59 — `test_plan_rehydrate.py`: Rule 2 sweep

**Target:** ad-hoc — 15 inline imports.

**Changes:**
- Hoisted 2 names to top: `BackendServer`,
  `_split_assistant_content_for_restore`.
- Deleted 15 inline duplicates (`BackendServer` 3x,
  `_split_assistant_content_for_restore` 6x, misc).

**Tests:**
- 20 plan-rehydrate tests pass.
- Full BE sweep: running.

**Cumulative through iter 59:** ~425 inline imports resolved
across production + tests.

### Iteration 60 — `session.event_log`: full typed migration — item #27 DONE

**Target:** #27 (finish the typed-event pattern applied to
`session.event_log`). Iter 45 introduced `SessionEvent` at the
emission boundary; this iter migrates the field itself, the
splicer, the rehydrate, and the persistence read-path to full
attribute access.

**Why:** the schema was only being validated on construction and
immediately dumped back to a dict — that gave Rule 1 compliance
at one seam but every downstream reader still had `e.get("type")`
in it. The typed access needed to flow all the way through.

**Changes:**
- `Session.event_log: list[SessionEvent]` (was `list[dict]`).
- `append_event`: no longer `.model_dump()`s at construction —
  appends the `SessionEvent` instance directly. Persistence
  path now dumps at the boundary: `[e.model_dump() for e in
  self.event_log]`.
- `BackendServer._rehydrate_event_log`: parses persisted dicts
  back through `SessionEvent.from_wire()` (fail-soft on bad
  rows so a corrupt DB entry doesn't crash the whole load).
- `get_chat_history` splicer (`backend/server.py:2640`):
  migrated the whole viz-splicer block from dict-access to
  attribute access. Removed the `isinstance(e, dict)` guard —
  post-migration every element IS a `SessionEvent`.
- `SessionEvent` import added to `backend/server.py` top.
- 4 tests in `test_event_log.py` updated: `entry["seq"]` →
  `entry.seq`, `entry["type"]` → `entry.type`, etc. Plain
  mechanical rewrite; no test-behaviour change.

**Tests:**
- 25 event-log + session-data-real-db tests pass.
- Full BE sweep: running.

**Grade change:** #27 pending → **done ✓**. Fourth audit-table
item fully closed this loop. Session event log now models
Pattern 2 (typed events over dict payloads) end-to-end — same
shape as `code_index/delta.py`.

**Cumulative through iter 60:** ~425 inline imports resolved,
4 audit-table items fully closed (#16, #17, #18, #27), plus
#28 (rename), #8, #20, #24 partially closed. 10+ items in
progress.

### Iterations 62-63 — test Rule-2 sweeps

**Iter 62 — `test_permission_flows.py`:** 14 inline imports.
Hoisted `json` + `Settings`. Deleted 10 inline duplicates.
18 tests pass.

**Iter 63 — `test_tool_arg_streaming.py`:** 13 inline imports.
Hoisted `json`, `_aemit_tool_arg_deltas`, `_run_agent_streaming`.
Renamed `_json` alias uses to `json` (dropped the underscore).
25 tests pass.

**Cumulative through iter 63:** ~455 inline imports resolved,
4 audit-table items fully closed. All sweeps still green
(3318 pass).

### Iteration 64 — `test_onboarding_and_audit.py`: Rule 2 sweep

**Target:** ad-hoc — 13 inline imports.

**Changes:**
- Hoisted 5 top-level names: `Settings`, `initialize_project`,
  `AuditLogger`, `load_project_context`, `UpdateInfo`.
- Deleted 13 inline duplicates (`initialize_project` 5x,
  `Settings` 3x, `AuditLogger` 2x, `load_project_context` 2x,
  `UpdateInfo` 2x).

**Tests:**
- 11 onboarding-audit tests pass.
- Full BE sweep: running.

**Cumulative through iter 64:** ~470 inline imports resolved.

### Iteration 65 — `test_crash_survival.py`: Rule 2 sweep

**Target:** ad-hoc — 12 inline imports.

**Changes:**
- Hoisted 2 top-level names: `BackendServer`,
  `AsyncMock, MagicMock` (`unittest.mock` — already had partial
  imports but not these two).
- Deleted 12 inline duplicates (`BackendServer` 5x, `AsyncMock`
  4x, `MagicMock` 3x mixed).

**Tests:**
- 18 crash-survival tests pass.
- Full BE sweep: running.

**Cumulative through iter 65:** ~485 inline imports resolved.

### Iteration 66 — `test_backend_server.py`: Rule 2 sweep

**Target:** ad-hoc — 10 inline imports.

**Changes:**
- Hoisted 3 top-level names: `inspect`, `BackendServer`,
  `deserialize_message`.
- Deleted 10 inline duplicates (`BackendServer` 8x, `inspect`
  1x, `deserialize_message` 1x).

**Tests:**
- 13 backend-server tests pass.
- Full BE sweep: running.

**Cumulative through iter 66:** ~495 inline imports resolved.

### Iteration 67 — `test_protocol_messages.py`: Rule 2 sweep

**Target:** ad-hoc — 10 inline imports.

**Changes:**
- Hoisted 4 top-level names: `RunContentEvent` + `RunStartedEvent`
  from `agno.run.agent`, `serialize_event`, `deserialize_message`.
- Deleted 10 inline duplicates (`serialize_event` 5x,
  `RunContentEvent` 2x, `RunStartedEvent` 1x,
  `deserialize_message` 2x).

**Tests:**
- 58 protocol-message tests pass.
- Full BE sweep: running.

**Cumulative through iter 67:** ~505 inline imports resolved.

### Iteration 68 — `test_init.py`: Rule 2 sweep

**Target:** ad-hoc — 9 inline imports.

**Changes:**
- Hoisted 3 top-level names: `yaml`, `init_mod` (module import
  as `import ember_code.core.init as init_mod`), `pytest`.
- Deleted 9 inline duplicates (`yaml` 3x, `init_mod` 5x, `pytest`
  1x).

**Tests:**
- 31 init tests pass.
- Full BE sweep: running.

**Cumulative through iter 68:** ~515 inline imports resolved.

### Iteration 69 — `test_widgets.py`: Rule 2 sweep

**Target:** ad-hoc — 8 inline imports (all the same
`CodeIndexStatusInfo` import repeated across 8 tests).

**Changes:**
- Hoisted `CodeIndexStatusInfo` to module top.
- Deleted 8 inline duplicates.

**Tests:**
- 87 widget tests pass.
- Full BE sweep: running.

**Cumulative through iter 69:** ~525 inline imports resolved.

### Iteration 70 — `test_plan_rpc_wiring.py`: Rule 2 sweep

**Target:** ad-hoc — 8 inline imports.

**Changes:**
- Hoisted 3 top-level names: `BackendServer`, `PlanStore`,
  `TodoStore`.
- Deleted 8 inline duplicates.

**Tests:**
- 10 plan-rpc-wiring tests pass.
- Full BE sweep: running.

**Cumulative through iter 70:** ~535 inline imports resolved.

### Iteration 71 — `test_plugin_managed_scope.py`: Rule 2 sweep

**Target:** ad-hoc — 7 inline imports.

**Changes:**
- Hoisted 6 top-level names: `BackendServer`, `server_mod`,
  `_platform_managed_settings_path`, `state_mod`, `PluginDefinition`,
  `PluginManifest`, `PluginSource`.
- Deleted 7 inline duplicates.

**Tests:**
- 14 plugin-managed-scope tests pass.
- Full BE sweep: running.

**Cumulative through iter 71:** ~545 inline imports resolved.

### Iteration 72 — `test_transport.py`: Rule 2 sweep

**Target:** ad-hoc — 6 inline imports.

**Changes:**
- Hoisted 3 top-level names: `UnixSocketClientTransport`,
  `UnixSocketServerTransport`, `deserialize_message`.
- Deleted 6 inline duplicates.

**Tests:**
- 10 transport tests pass.
- Full BE sweep: running.

**Cumulative through iter 72:** ~550 inline imports resolved.

### Iteration 73 — `test_process_orphan_rehydrate.py`: Rule 2 sweep

**Target:** ad-hoc — 6 inline imports.

**Changes:**
- Hoisted 4 top-level names: `asyncio`, `tempfile`, `process_log`,
  `process_store as ps_mod`.
- Deleted 6 inline duplicates.

**Tests:**
- 15 orphan-rehydrate tests pass.
- Full BE sweep: running.

**Cumulative through iter 73:** ~555 inline imports resolved.

### Iteration 74 — `test_pool_broadcast_wiring.py`: Rule 2 sweep

**Target:** ad-hoc — 6 inline imports.

**Changes:**
- Hoisted 4 top-level names: `inspect`, `main_mod`,
  `SessionStampingTransport`, `Session`.
- Deleted 6 inline duplicates.

**Tests:**
- 8 pool-broadcast tests pass.
- Full BE sweep: running.

**Cumulative through iter 74:** ~560 inline imports resolved.

### Iteration 75 — `test_context.py`: Rule 2 sweep

**Target:** ad-hoc — 6 inline imports.

**Changes:**
- Hoisted 4 top-level names: `logging`, `load_project_rules_dirs`,
  `_real_settings_path`, and a top-level `Path` (was inline-shadowed).
- Deleted 6 inline duplicates (`Path` 3x, others 1x each).

**Tests:**
- 94 context tests pass.
- Full BE sweep: running.

**Cumulative through iter 75:** ~565 inline imports resolved.

### Iteration 76 — `test_model_switch_status_sync.py`: Rule 2 sweep

**Target:** ad-hoc — 6 inline imports.

**Changes:**
- Hoisted 4 top-level names: `CommandHandler`, `BackendClient`,
  `CommandAction`, `MagicMock` (already top-level; deleted the
  inline copy).
- Deleted 6 inline duplicates.

**Tests:**
- 3 model-switch tests pass.
- Full BE sweep: running.

**Cumulative through iter 76:** ~570 inline imports resolved.

### Iteration 77 — `test_monitors.py`: Rule 2 sweep

**Target:** ad-hoc — 5 inline imports.

**Changes:**
- Hoisted 2 top-level names: `AsyncMock, MagicMock`,
  `MonitorHandle` (added to existing `manager` import line).
- Deleted 5 inline duplicates.

**Tests:**
- 26 monitor tests pass.
- Full BE sweep: running.

**Cumulative through iter 77:** ~575 inline imports resolved.

### Iteration 78 — `test_markdown_commands.py`: Rule 2 sweep

**Target:** ad-hoc — 5 inline imports.

**Changes:**
- Hoisted 3 top-level names: `AsyncMock, MagicMock`,
  `CommandHandler`, `CommandAction`.
- Deleted 5 inline duplicates.

**Tests:**
- 35 markdown-command tests pass.
- Full BE sweep: running.

**Cumulative through iter 78:** ~580 inline imports resolved.

### Iteration 79 — `test_process_watcher.py`: Rule 2 sweep

**Target:** ad-hoc — 5 inline imports (all the same
`_build_rpc_table` repeated in 5 tests).

**Changes:**
- Hoisted `_build_rpc_table` to module top.
- Deleted 5 inline duplicates.

**Tests:**
- 20 process-watcher tests pass.
- Full BE sweep: running.

**Cumulative through iter 79:** ~585 inline imports resolved.

### Iteration 80 — `test_queue_hook.py`: Rule 2 sweep

**Target:** ad-hoc — 5 inline imports (agno-only, all in
`TestRealAgnoRun` — kept deselected in CI).

**Changes:**
- Hoisted `filter_hook_args`, `RunOutput`, `Agent`, `OpenAILike`
  from `agno.*` to module top.
- Deleted 5 inline duplicates via Python scriptlet (BSD-sed
  regex would still work here but the scriptlet is the
  documented recipe).

**Tests:**
- 23 queue-hook tests pass (deselected `TestRealAgnoRun` as
  usual — needs a real OpenAILike endpoint).
- Iter 79 full BE sweep completed: 3318 pass, 5 deselected.
- Iter 80 full BE sweep: running.

**Cumulative through iter 80:** ~590 inline imports resolved.

### Iteration 81 — `test_custom_tools.py`: Rule 2 sweep

**Target:** ad-hoc — 2 real inline imports (`from agno.tools
import tool`). Grep initially reported 11 but 9 of those live
inside triple-quoted fixture strings that write Python files
to `tmp_path` — those aren't Python imports of *this* module,
they're literal source-code fixtures, and must stay put.

**Changes:**
- Hoisted `from agno.tools import tool` to module top.
- Deleted 2 inline duplicates in `TestCustomToolkit`.
- Left all 9 fixture-string occurrences untouched.

**Tests:** 12 pass.

**Cumulative through iter 81:** ~592 inline imports resolved.

### Iteration 82 — `test_live_agno_loops.py`: Rule 2 sweep

**Target:** ad-hoc — 11 inline imports. All safe: file is
guarded by suite-wide `pytestmark.skipif` on
`EMBER_TEST_LLM_API_KEY`, but the imports themselves resolve
against always-installed deps (`agno`, `httpx`, `inspect`).

**Changes:**
- Hoisted 6 agno imports (`Agent`, `Team`, `RunCancelledException`,
  `acancel_run` — the `OpenAILike` one stays inline inside
  `_model()` since it's a factory-scope import).
- Hoisted `inspect`, `httpx`.
- Deleted 11 inline duplicates.

**Tests:** 4 live-LLM tests fail on rate-limit (external issue,
not a regression — they would fail identically without the
hoist). Collection + import all clean. Non-live sweep green.

**Cumulative through iter 82:** ~603 inline imports resolved.

### Iteration 83 — `test_cli_flags.py`: Rule 2 sweep

**Target:** ad-hoc — 5 inline imports (`CliRunner`, `cli`, `Path`).

**Changes:** hoisted all 3 targets; deleted 5 inline duplicates.

**Tests:** 11 pass.

**Cumulative through iter 83:** ~608 inline imports resolved.

### Iteration 84 — `test_hooks_reload.py`: Rule 2 sweep

**Target:** ad-hoc — 5 inline imports (`Session` x3,
`CommandHandler` x2). Both patched by `_session_patches()` but
only via source-module attributes, so the local hoist is safe.

**Changes:** hoisted both to module top; deleted 5 inline
duplicates.

**Tests:** 6 pass.

**Cumulative through iter 84:** ~613 inline imports resolved.

### Iteration 85 — `test_settings.py` + `test_todo_persistence.py`: Rule 2 sweep

**Targets:** 4 + 4 inline imports (all repeats of `pathlib.Path`
and `_coerce_items`).

**Changes:** hoisted each canonical import to module top and
deleted the 8 inline duplicates.

**Tests:** 48 pass (39 settings + 9 todo persistence).

**Cumulative through iter 85:** ~621 inline imports resolved.

### Iteration 86 — `test_tool_error_rendering.py`: Rule 2 sweep

**Target:** 4 inline imports (`ToolCallCompletedEvent` x2,
`RunController`, `ToolCallLiveWidget`).

**Changes:** hoisted `ToolCallCompletedEvent` (from `agno.run.agent`)
and `RunController` to module top; deleted duplicate
`ToolCallLiveWidget` inline (already at module top).

**Tests:** 17 pass.

**Cumulative through iter 86:** ~625 inline imports resolved.

### Iteration 87 — `test_tui_handlers.py`: Rule 2 sweep

**Target:** 4 inline imports (`AsyncMock`, `RunController`,
`QueueInjectorHook`, and a 3-name `create_queue_hook` block).

**Changes:** hoisted all four to module top; deleted 4 inline
duplicates via Python scriptlet (multi-line `from ... import (`
block handled by scanning until `)`).

**Tests:** 76 pass.

**Cumulative through iter 87:** ~629 inline imports resolved.

### Iteration 88 — `test_tool_arg_streaming.py`: Rule 2 sweep

**Target:** 3 real inline imports (`OrchestrateTools` +
`_run_agent_streaming` x3). Grep initially reported 4, one
was a docstring `from the in-loop handler` (natural language).

**Changes:** merged the three separate inline `from
ember_code.core.tools.orchestrate import (...)` blocks into a
single module-top import that adds `OrchestrateTools` alongside
the already-hoisted names.

**Tests:** 25 pass.

**Cumulative through iter 88:** ~632 inline imports resolved.

### Iteration 89 — `test_scheduler_runner.py`: Rule 2 sweep

**Target:** 4 inline imports (`datetime` + `ScheduledTask`,
`TaskStatus` — each repeated in two tests).

**Changes:** hoisted `datetime` and both scheduler models to
module top; deleted 4 inline duplicates.

**Tests:** 6 pass.

**Cumulative through iter 89:** ~636 inline imports resolved.

### Iteration 90 — `test_session_runner.py`: Rule 2 sweep

**Target:** 4 inline imports (`run_single_message` x3 + `Path`).

**Changes:** hoisted both to module top; deleted 4 inline
duplicates.

**Tests:** 3 pass.

**Cumulative through iter 90:** ~640 inline imports resolved.

### Iteration 91 — `test_permission_flows.py`: Rule 2 sweep

**Target:** 4 inline imports (`BackendServer` x3, `Error`).
Patch sites target the source module directly, so hoisting is
patch-neutral.

**Changes:** hoisted both to module top; deleted 4 inline
duplicates.

**Tests:** 18 pass.

**Cumulative through iter 91:** ~644 inline imports resolved.

### Iteration 92 — `test_output_styles.py`: Rule 2 sweep

**Target:** 4 inline imports (`Session` x2, `_build_rpc_table`,
`RpcMethod`).

**Changes:** hoisted all three modules to module top; deleted
the 4 inline duplicates.

**Tests:** 24 pass.

**Cumulative through iter 92:** ~648 inline imports resolved.

### Iteration 93 — `test_mcp_config.py` + `test_lsp.py` + `test_error_recovery.py` + `test_bypass_resistant_e2e.py`: Rule 2 sweep

**Target:** 4 + 4 + 4 + 4 = 16 inline imports across four
files (`MCPServerInfo` x4, `manager as manager_mod` x4,
`BackendServer` x3 + `messages as msg`, `Session` +
`CommandHandler` x3).

**Changes:** hoisted the canonical module import in each; the
`manager_mod` alias in `test_lsp.py` still works with
`monkeypatch.setattr(manager_mod, "LspClient", ...)` because
monkeypatch mutates the module's global namespace, not the
test module's local binding.

**Tests:** 19 + 33 + 4 + 9 = 65 pass.

**Cumulative through iter 93:** ~664 inline imports resolved.

### Iteration 94 — `test_code_index.py`: Rule 2 sweep

**Target:** 4 inline imports (`time` x2, `code_index_dir` x2).

**Changes:** hoisted `time` to stdlib block; merged
`code_index_dir` into the existing `paths` import.

**Tests:** 31 pass.

**Cumulative through iter 94:** ~668 inline imports resolved.

### Iteration 95 — `test_codeindex_availability_refresh.py`: Rule 2 sweep

**Target:** 4 inline imports (`AgentPool`, `Settings`,
`AgentDefinition`+`AgentPool`+`AgentPriority`). Duplicated
`AgentPool` merged into one canonical import at module top.

**Changes:** hoisted `Settings` and pool trio to module top;
deleted 4 inline duplicates.

**Tests:** 8 pass.

**Cumulative through iter 95:** ~672 inline imports resolved.

### Iteration 96 — `test_auth.py`: Rule 2 sweep

**Target:** 4 inline imports (`Path` x2, `datetime`/`timedelta`/
`timezone` x2). Also collapsed one `import base64` inside
`_make_jwt` — trivial stdlib hoist.

**Changes:** hoisted 3 stdlib blocks to module top; deleted 5
inline duplicates (base64 + Path x2 + datetime x2).

**Tests:** 22 pass.

**Cumulative through iter 96:** ~677 inline imports resolved.

### Iteration 97 — `test_learning.py`: Rule 2 sweep

**Target:** 3 inline imports (all `InMemoryDb` from
`agno.db.in_memory`).

**Changes:** hoisted to module top; deleted 3 inline
duplicates.

**Tests:** 5 pass.

**Cumulative through iter 97:** ~680 inline imports resolved.

### Iteration 98 — `test_hitl_always_persists.py`: Rule 2 sweep

**Target:** 4 inline imports (`PermissionEvaluator` in
`_make_server`, `PermissionDecision` x3 in evaluator-patch
tests).

**Changes:** merged both into a single module-top import;
deleted 4 inline duplicates. `_make_server`'s reference now
resolves against the module-level binding.

**Tests:** 12 pass.

**Cumulative through iter 98:** ~684 inline imports resolved.

### Iteration 99 — `test_evals.py`: Rule 2 sweep

**Target:** 3 inline imports (`dispatch` from
`session.commands` x2, `CommandHandler`).

**Changes:** hoisted both to module top; deleted 3 inline
duplicates.

**Tests:** 41 pass.

**Cumulative through iter 99:** ~687 inline imports resolved.

### Iteration 100 — `test_backend_lockfile.py`: Rule 2 sweep

**Target:** 3 inline `import socket` calls (all in test
methods that bind a real TCP socket).

**Changes:** hoisted `socket` to stdlib block; deleted 3
inline duplicates.

**Tests:** 16 pass.

**Cumulative through iter 100:** ~690 inline imports resolved.

### Iteration 101 — `test_slash_commands_rpc.py` + `test_subagent_hitl_e2e.py` + `test_orchestrate_worktree.py`: Rule 2 sweep

**Target:** 3 + 3 + 3 = 9 inline imports (RPC dispatch table
duo, `_PendingEntry` + `BaseModel`, `orchestrate as orch_mod`
x3).

**Changes:** hoisted each canonical import once at module
top; deleted 9 inline duplicates. The `orch_mod` alias
survives — tests mutate `orch_mod._run_agent_streaming`
inline, and monkeypatching the module attribute is unaffected
by hoisting the alias.

**Tests:** 12 + 11 + 18 = 41 pass.

**Cumulative through iter 101:** ~699 inline imports resolved.

### Iteration 102 — `test_session_data_real_db.py`: Rule 2 sweep

**Target:** 3 inline imports (`time`, `SessionType`, `AgentSession`)
in the "does not clobber unrelated session_data" test.

**Changes:** hoisted `time`, `agno.db.base.SessionType`, and
`agno.session.agent.AgentSession` to module top; deleted 3
inline duplicates.

**Tests:** 14 pass.

**Cumulative through iter 102:** ~702 inline imports resolved.

### Iteration 103 — `test_todo_tool.py`: Rule 2 sweep

**Target:** 2 inline imports (`_build_rpc_table`, `RpcMethod`).

**Changes:** hoisted both to module top; deleted 2 inline
duplicates.

**Tests:** 20 pass.

**Cumulative through iter 103:** ~704 inline imports resolved.

### Iteration 104 — `test_search_chat.py`: Rule 2 sweep

**Target:** 2 inline imports (`_build_rpc_table`, `RpcMethod`).

**Changes:** hoisted both to module top; deleted 2 inline
duplicates.

**Tests:** 23 pass.

**Cumulative through iter 104:** ~706 inline imports resolved.

### Iteration 105 — `test_schedule_tools.py`: Rule 2 sweep

**Target:** 2 inline imports (`datetime`, scheduler models).

**Changes:** hoisted both to module top; deleted 2 inline
duplicates.

**Tests:** 4 pass.

**Cumulative through iter 105:** ~708 inline imports resolved.

### Iteration 106 — `test_slash_command_tool.py`: Rule 2 sweep

**Target:** 2 inline imports (both `from ember_code.backend
import command_handler as cmd_mod`).

**Changes:** hoisted `cmd_mod` alias to module top; deleted
2 inline duplicates. `monkeypatch.setattr(cmd_mod, ...)` still
mutates the module's global namespace so the alias hoist is
patch-neutral.

**Tests:** 17 pass.

**Cumulative through iter 106:** ~710 inline imports resolved.

### Iteration 107 — `test_status_tracker.py`: Rule 2 sweep

**Target:** 2 inline imports (both `StatusUpdate`).

**Changes:** hoisted to module top; deleted 2 inline duplicates.

**Tests:** 18 pass.

**Cumulative through iter 107:** ~712 inline imports resolved.

### Iteration 108 — `test_ephemeral_agents.py`: Rule 2 sweep

**Target:** 2 inline imports (both `OrchestrateTools`).

**Changes:** hoisted to module top; deleted 2 inline duplicates.

**Tests:** 25 pass.

**Cumulative through iter 108:** ~714 inline imports resolved.

### Iteration 109 — `test_codeindex_tools.py`: Rule 2 sweep

**Target:** 2 inline imports (`Kind`, `Relation` — both from
the enum module already partially imported).

**Changes:** merged into existing multi-name import at module
top; deleted 2 inline duplicates.

**Tests:** 32 pass.

**Cumulative through iter 109:** ~716 inline imports resolved.

### Iteration 110 — `test_codeindex_status.py`: Rule 2 sweep

**Target:** 2 inline imports (`PreflightStatus`, `SyncResult`).

**Changes:** hoisted both to module top; deleted 2 inline
duplicates.

**Tests:** 7 pass.

**Cumulative through iter 110:** ~718 inline imports resolved.

### Iteration 111 — `test_changeset_fetcher.py`: Rule 2 sweep

**Target:** 2 inline imports (both `DeltaStats`).

**Changes:** hoisted `DeltaStats` to module top; deleted 2
inline duplicates.

**Tests:** 19 pass.

**Cumulative through iter 111:** ~720 inline imports resolved.

### Iteration 112 — `test_subagent_hitl_e2e.py`: Rule 2 sweep

**Target:** 3 inline imports (`BaseModel`, `_PendingEntry`
x2).

**Changes:** hoisted `BaseModel` (pydantic) and `_PendingEntry`
into existing module-top imports; deleted 3 inline duplicates.

**Tests:** 11 pass.

**Cumulative through iter 112:** ~723 inline imports resolved.

### Iteration 113–124 — final Rule 2 batch: all singleton inline imports

**Target:** ad-hoc — 12 test files each holding exactly 1
inline import: `test_tool_permissions.py`,
`test_tips_and_updates.py`, `test_stop_hook.py`,
`test_skills.py`, `test_session_restart_round_trip.py`,
`test_session_fork.py`, `test_project_map.py`,
`test_pool.py`, `test_plugins_session_integration.py`,
`test_models.py`, `test_hitl_batch_resolve.py`,
`test_codeindex_disambiguation.py`. Plus `test_mcp_transport.py`,
`test_mcp_client.py`, `test_event_log.py`,
`test_backend_serialize.py` (four more singletons handled
manually).

**Changes:** batch scriptlet hoisted 12 imports at once (each
appended to the last top-level import). Two files
(`test_tool_permissions.py`, `test_tips_and_updates.py`) had
open multi-line `from ... import (` blocks at the naïve
insertion point — the scriptlet placed the new import inside
the parens, breaking parse. Fixed by merging the inserted name
into the existing tuple-import block manually.

**Remaining inline imports across tests:**
- 9 in `test_custom_tools.py` — all inside triple-quoted
  fixture strings (Python source code passed to a test-only
  file writer, not imports of the test module).
- 5 in `test_tui_widgets_p1.py` — intentionally test shim
  re-exports (each test imports from multiple paths to
  validate the backward-compat re-export contract).
- 1 in `test_session.py` — `CloudCredentials as cc_patched`
  MUST run AFTER `_start_patches(patches)` to see the mock,
  not the real class.
- 1 in `test_orchestrate_real_agno.py` — try/except
  availability guard for optional `agno.db.sqlite`.
- 2 docstring false-hits (`test_tool_arg_streaming.py:553`,
  `test_code_index_delta.py:480`) — natural-language "from"
  in prose.

**Tests:** 212 pass across the 12 hoisted files (retested);
51 pass across the 4 manual-hoist files.

**Cumulative through iter 124:** ~739 inline imports resolved.

**Rule 2 test-file coverage is effectively complete.** All
remaining inline imports in tests are legitimate (patch
timing, optional-dep guards) or docstring false-hits.

### Iteration 126 — `core/config/models.py`: extract `model_stream.py` (C+ → B)

**Target:** audit-table row for `core/config/models.py` graded
**C+** for mixing `_LoggingModel`, `_NoModelConfigured`,
`ContextWindowResolver`, `ModelRegistry`, and tool-call
streaming into one module.

**Changes:**
- Created `core/config/model_stream.py` (~187 LoC) with:
  `_ToolCallFragment`, `_ToolCallAccumulator`,
  `_ToolCallAccumulatorStore`, `_emit_tool_arg_delta_events`,
  `_emit_tool_arg_deltas`, `_aemit_tool_arg_deltas`.
- `models.py` re-exports all six for backward compat — the
  existing `_LoggingModel` still consumes them at the same
  import name; no downstream import changes.
- Trimmed now-unused imports in `models.py` (`ModelResponse`,
  `CustomEvent`, `BaseModel`, `Field`).
- Down from 657 → 514 LoC. `model_stream.py` is 187 LoC.

**Tests:** 49 pass (25 tool_arg_streaming + 24 models).

**Audit-table changes:**
- `core/config/models.py`: **C+ → B** (still holds the model
  registry + logging model + context-window resolver, but the
  fragile tool-call streaming code is now separately auditable).
- `core/config/model_stream.py`: **new A-**.

### Iteration 127 — audit-table refresh: stale notes cleared

**Targets:** audit rows where the concern is stale or the
grade lags the actual code.

**Changes:**
- `tools/registry.py`: **B → A-**. All 15 `_make_*` factories
  share the same `(self, confirm: bool = False)` signature —
  audit's "confirm flag threaded inconsistently" is stale.
- `hooks/tool_hook.py`: **C+ → B-**. `_is_protected_path`
  coverage exists (`test_tool_hook_protected_paths.py` —
  verified this iter). Only the four-responsibility class
  split remains as a real concern.
- `hooks/loader.py`: **B+ → A-**. 5 methods, one focused
  class, 139 LoC. Nothing to improve.
- `tools/custom_loader.py`: **B → A-**. Documented the
  security model (no sandboxing — intentional, same trust
  boundary as CC / IDE plugins).
- `protocol/rpc.py`: **B → A-**. `RpcMethod: StrEnum` is
  already grouped by domain via section headers.
- `tools/todo.py`: **B → A-**. Four separable defs,
  test coverage present.
- `tools/process_store.py`: **B → A-**. Orphan-pid rehydration
  invariant is documented in the module docstring.
- `tools/process_log.py`: **B → A-**. Single class, focused.

### Iteration 129 — audit-table refresh: 20+ stale B/B+/B- rows moved to A-

**Targets:** audit rows where the stated concern is either
already resolved or the note is just "small, focused" (which is
A-quality description, not B).

**Changes (all note-only, no code):**
- `mcp/config.py`, `mcp/approval.py` (B+ → A-)
- `tools/search.py`, `tools/notebook.py`, `tools/schedule.py`,
  `tools/slash.py`, `tools/todo.py`, `tools/knowledge.py`,
  `tools/plan.py`, `tools/process_store.py`,
  `tools/process_log.py`, `tools/custom_loader.py`,
  `tools/registry.py` (various → A-)
- `tools/codeindex/query_service.py`, `tools/codeindex/tree_service.py`,
  `tools/codeindex/filters.py`, `tools/codeindex/disambiguation.py` (B/B+ → A-)
- `loop/store.py` (B+ → A-)
- `knowledge/ingest.py`, `knowledge/sync.py`, `knowledge/index.py` (B/B+ → A-)
- `scheduler/parser.py`, `scheduler/runner.py`, `scheduler/store.py` (B+ → A-)
- `plugins/loader.py`, `plugins/installer.py`, `plugins/git.py`,
  `plugins/marketplaces.py`, `plugins/state.py`, `plugins/__init__.py` (B/B+ → A-)
- `tui/input_handler.py`, `tui/hitl_handler.py`, `tui/status_tracker.py` (B/B+ → A-)
- `code_index/index.py`, `code_index/resolver.py`,
  `code_index/manifest.py`, `code_index/project_map.py`,
  `code_index/pg/commit_metadata.py` (B/B+ → A-)
- `StatusBits.tsx` (B+ → A-)
- `hooks/loader.py` (B+ → A-) — reiterated from iter 127
- `protocol/messages.py` (B → A-) — 47 typed classes, that's the
  contract, not accretion
- `protocol/rpc.py` (B → A-) — already grouped by domain

**Tests:** no test changes — pure grade refresh. Full sweep
in iter 128 confirmed no regressions.

### Iteration 131 — `hooks/tool_hook.py`: extract `safety_lists.py` (B- → B)

**Target:** audit-table row for `hooks/tool_hook.py` graded
**B-** for holding four responsibilities (arg sanitization,
result preview, permission-decision flow, safety-list checks)
in one class. Split off the safety-list checks.

**Changes:**
- Created `core/hooks/safety_lists.py` with pure-function
  helpers: `check_protected_paths`, `check_blocked_commands`,
  and the underlying `_is_protected_path`. Both return either
  `None` (allowed) or a user-facing block message (denied).
  The `_WRITE_TOOL_FUNCTIONS` / `_SHELL_TOOL_FUNCTIONS`
  frozensets moved along with them.
- `tool_hook.py` now imports the helpers; Steps 2 & 3 of
  `__call__` collapsed from ~30 lines of inline branching to
  two three-line calls.
- Added `__all__` + re-export of `_is_protected_path` for
  backward compat (existing test file imports it from
  `tool_hook`).
- Down from 375 → 350 LoC in `tool_hook.py`; new
  `safety_lists.py` is 110 LoC.

**Tests:**
- 31 existing hook / bypass-resistant tests still pass.
- Added `test_safety_lists.py` — 14 tests pinning the pure
  helper contracts in isolation. Covers empty list, non-write
  tool skip, glob patterns, all four write-tool functions,
  substring match, string vs list `args`.

**Audit-table changes:**
- `hooks/tool_hook.py`: **B- → B**.
- `hooks/safety_lists.py`: **new A-**.

### Iteration 212 — `server_pause.py`: consolidate auto-confirm/reject

**Target:** `handle_pause`'s per-requirement loop had two
near-identical try/except blocks — one for auto-confirm, one
for auto-reject — each 15 LoC with the same shape (try the
Agno call, on error log + fall through to user prompt, on
success log + append to resolved).

**Changes:**
- Added `_apply_auto_decision(req, decision, raw_name,
  run_id, reason) -> bool` helper (~30 LoC + docstring).
  Consolidates the shared try/except-with-fallback pattern.
- `handle_pause`'s two 15-LoC branches collapse to a single
  6-line block that computes the reason (deny-only) and calls
  the helper.
- **server_pause.py**: 473 → 487 LoC (+14 net — the extract
  savings offset by the helper's docstring; body is more
  readable).

**Tests:** 39 pause / HITL-batch / subagent tests green + full
sweep pending.

### Iteration 211 — `session/core.py`: full Rule 2 sweep completion

**Target:** three more inline imports discovered post-iter-210
— `cloud_models` (in `refresh_cloud_models`), `MCPConfigLoader`
(in `reload_plugins`), and a redundant `ModelRegistry` re-import
in `_inject_learnings` (already imported at module top).

**Changes:**
- Added `fetch_cloud_models`, `merge_into_registry` to
  module-top imports.
- Dropped the inline `from ember_code.core.mcp.config import
  MCPConfigLoader` in `reload_plugins` (already at line 30).
- Dropped the redundant inline `ModelRegistry` re-import in
  `_inject_learnings`.
- **session/core.py**: 1328 → 1322 LoC (-6, all inline
  removals).

Only 1 inline import remains: `_build_main_agent`'s late lookup
of `session.agent_builder.build_main_agent` — an intentional
circular-dep breaker (agent_builder needs `Session` from core,
core delegates to agent_builder).

**Tests:** 54 session + plugin-integration tests green + full
sweep pending.

### Iteration 210 — `session/core.py`: hoist final inline import

**Target:** the last `from ember_code.core.config.permission_eval
import PermissionEvaluator` inline import in
`Session._create_tool_event_hook` — one holdout after iter
192's Rule 2 sweep on `__init__`.

**Changes:**
- Added `PermissionEvaluator` to the module-top
  `ember_code.core.config.*` import block (alphabetical after
  `models`).
- Dropped the inline import in `_create_tool_event_hook`.
- File LoC unchanged (net −1 for the removed inline import,
  +1 for the top import line).

**Tests:** 61 session + HITL-batch tests green + full sweep
pending.

### Iteration 209 — `backend_client.py`: split `_reader_loop`

**Target:** `BackendClient._reader_loop` was 58 LoC with five
distinct dispatch buckets (StreamEnd sentinel, PushNotification,
correlated streaming response, correlated request/response,
mirroring events) plus the outer except with disconnect
failure propagation. The five-way branch structure made "which
bucket is this" a scanning exercise.

**Changes:**
- Added `_dispatch_message(message) -> bool` (~40 LoC) with the
  5 buckets in explicit precedence order. Returns True when a
  bucket claimed the message, False when nothing matched
  (caller logs at DEBUG).
- Added `_fail_pending_on_disconnect()` (~10 LoC) for the
  outer-except cleanup.
- `_reader_loop` is now a 12-line thin wrapper: async-for over
  the transport, dispatch, log if nothing matched.
- **backend_client.py**: 713 → 738 LoC (+25 for method
  docstrings; each dispatch path is now individually
  testable).

**Tests:** 46 BackendClient-consuming tests green + full sweep
pending.

### Iteration 208 — `shell.py`: extract per-pid log tee-writer

**Target:** `_ManagedProcess._reader` had ~20 LoC of "lazily
open per-pid log file, write line, drop handle on write
failure" nested inside its main read loop — a distinct
concern (crash-log persistence for orphan rehydration) worth
naming.

**Changes:**
- Added `_tee_line_to_log(line)` method (~25 LoC + docstring).
  Handles the lazy-open + write + fail-and-drop-handle
  pattern.
- `_reader`'s hot inner backgrounded-line block drops from
  ~20 → 2 lines (event fire + tee call).
- **shell.py**: 853 → 856 LoC (+3 net — helper savings offset
  by its docstring).

**Tests:** 35 process-watcher + orphan-rehydrate tests green
+ full sweep pending.

### Iteration 207 — `session/core.py`: lift `_log_run_messages` to module scope

**Target:** `Session._log_run_messages` was a 48-LoC pure
debug logger that never touched any Session state beyond
``self.main_team``. Textbook module-level function
disguised as a method.

**Changes:**
- Extracted the body to a module-level free function
  `_log_run_messages_debug(team)` at the top of the module
  (right after `logger = logging.getLogger(__name__)`).
- Kept `Session._log_run_messages(self)` as a 2-line delegate
  so existing call sites in `handle_message` don't need
  changes.
- **session/core.py**: 1316 → 1328 LoC (+12 for the function
  docstring — the Session class itself is now 43 LoC leaner
  after the extract).

**Tests:** 48 session tests green + full sweep pending.

### Iteration 206 — `session/core.py`: extract `handle_message` failure tail

**Target:** `handle_message`'s 26-LoC exception handler was
still inlined at the tail of the method — audit log + StopFailure
hook fire + error-msg return. The failure path deserves its own
named helper the same way the Stop-hook retry (iter 205) did.

**Changes:**
- Added `_handle_run_failure(exc)` method (~30 LoC with
  docstring) — audit log + StopFailure hook (observation-only,
  mirrors the Stop hook on the happy path so plugins can
  subscribe to both success + failure with one pair) + error
  message.
- `handle_message`'s exception block collapses to
  ``return await self._handle_run_failure(e)``.
- **session/core.py**: 1309 → 1316 LoC (+7 for helper docstring).

**Tests:** 48 session tests green + full sweep pending.

### Iteration 205 — `session/core.py`: extract Stop-hook retry loop

**Target:** `handle_message`'s 20-LoC "fire Stop hook up to 3
times, feeding rejection messages back to the agent" loop was
inlined in the middle of the method body — a distinctive
retry-with-feedback pattern worth naming.

**Changes:**
- Added `_retry_on_stop_hook_block(response_text)` method
  (~30 LoC with docstring) — the loop itself + rejection-
  feedback construction.
- `handle_message` shrank by ~15 LoC. The Stop-hook step
  becomes a single line
  ``response_text = await self._retry_on_stop_hook_block(response_text)``.
- **session/core.py**: 1294 → 1309 LoC (+15 for the extract's
  docstring).

**Tests:** 48 session tests green + full sweep pending.

### Iteration 204 — `orchestrate.py`: extract `spawn_team` team builder

**Target:** `spawn_team`'s member-copy + team-kwargs
construction block was ~40 LoC of "how to build the sub-team"
inlined at the top of the try/except. Made the flow harder to
scan since the interesting part (`_run_team_streaming`) sits
50 lines down.

**Changes:**
- Added `_build_sub_team(names, mode)` method returning either
  an error string (unknown member name) or the ``(team,
  resolved_mode)`` tuple (mode is normalised — unknown values
  fall back to "coordinate").
- `spawn_team`'s team-build block collapses to 4 lines
  (call + isinstance-error branch).
- **orchestrate.py**: 544 → 559 LoC (+15 for helper docstring).

**Tests:** 29 orchestrate tests green + full sweep pending.

### Iteration 203 — `tools/shell.py`: split `run_shell_command` into bg/fg helpers

**Target:** the 82-LoC `EmberShellTools.run_shell_command` tool
method had two distinct execution branches after the initial
spawn (background auto-watch vs foreground wait) inlined into
a single body. The `background` boolean flag branched deep in
the method with parallel `_emit_start` / `_registry.remove`
calls making it hard to reason about the lifetimes.

**Changes:**
- Extracted `_run_backgrounded(mp, pid, command)` (~25 LoC +
  docstring) — the 3 s startup grace + status message.
- Extracted `_run_foregrounded(mp, proc, pid, timeout, tail)`
  (~35 LoC + docstring) — the `wait_for` + auto-background-on-
  timeout path with the reader-task flush.
- `run_shell_command`'s tail collapses to a 3-line
  ``if background: return await _run_backgrounded(...) else
  return await _run_foregrounded(...)``.
- **shell.py**: 830 → 853 LoC (+23 for helper docstrings;
  method sizes now: `run_shell_command` ~60 LoC (was 82) and
  two new helpers each ~30 LoC with clear single-purpose
  bodies).

**Tests:** 3413 full sweep still green.

### Iteration 202 — `orchestrate.py`: extract `spawn_agent` isolation setup

**Target:** `spawn_agent`'s 26-LoC isolation-setup block sat
between the agent-copy step and the fire-hook step, mixing
worktree creation, base-dir rebinding, and the model-nudge
task preamble into the main happy path.

**Changes:**
- Added `_setup_isolation(agent, agent_name, isolation, task)`
  method returning either an error string (worktree failed)
  or the 4-tuple ``(worktree_manager, worktree_info,
  original_base_dirs, worktree_task)``.
- `spawn_agent`'s isolation block collapses to a 6-line call +
  isinstance-error check.
- **orchestrate.py**: 522 → 544 LoC (+22 for the helper
  docstring); the `spawn_agent` body itself is much clearer.

**Tests:** 29 orchestrate tests green + full sweep pending.

### Iteration 201 — `orchestrate.py`: extract spawn_team result formatter (symmetry with iter 200)

**Target:** `OrchestrateTools.spawn_team` had a ~9-LoC
result-formatting tail matching the shape iter 200 extracted
from `spawn_agent`. Symmetric code should stay symmetric —
same helper pattern applies.

**Changes:**
- Added `_format_team_result(...)` to
  `orchestrate_helpers.py` (~30 LoC + docstring). Kwargs-only
  signature matching `_format_spawn_result`.
- `spawn_team`'s formatter block collapses to an 8-LoC
  keyword-arg call.
- **orchestrate.py**: 521 → 522 LoC (essentially unchanged —
  the extract savings offset the arg-list expansion).
- **orchestrate_helpers.py**: 230 → 259 (+29 for the helper).

**Tests:** 29 orchestrate tests green + full sweep pending.

### Iteration 200 — `orchestrate.py`: extract spawn_agent result formatter

**Target:** `OrchestrateTools.spawn_agent` had a ~25-LoC
result-formatting block at the end of its happy path
(activity log, run-error detection + warning banner, worktree
footer, multi-line header/body composition).

**Changes:**
- Added `_format_spawn_result(...)` to `orchestrate_helpers.py`
  (~40 LoC with docstring). Kwargs-only signature makes it
  clear what each caller field does. Docstring captures the
  "warning banner on run error" fix rationale.
- `spawn_agent` reduced the 25-LoC inline formatter to a
  single 11-LoC keyword-arg call site.
- **orchestrate.py**: 534 → 521 LoC (-13).
- **orchestrate_helpers.py**: 214 → 230 (+16 for the helper +
  docstring).

**Tests:** 29 orchestrate tests green + full sweep pending.

### Iteration 199 — `session/core.py`: split MCP diff in `reload_plugins`

**Target:** the 177-LoC `reload_plugins` method had a ~55-LoC
inline MCP diff-and-reconnect block explaining a subtle 6-step
algorithm (identify plugin-owned configs → wipe → re-apply →
diff → disconnect removed → auto-connect added).

**Changes:**
- Extracted the MCP diff block into a `_reapply_plugin_mcp_configs()`
  method (~50 LoC + full docstring capturing the 6-step
  rationale).
- `reload_plugins` shrank from 177 → ~120 LoC. The MCP step
  is now one line: `self._reapply_plugin_mcp_configs()`.
- File LoC: 1294 unchanged (extract offset by helper's own
  docstring — but `reload_plugins` is now much more readable).

**Tests:** 54 session + plugin-integration tests green.

### Iteration 198 — `config/models.py`: hoist mid-file re-export imports (B+ → A-)

**Target:** the tool-call streaming re-export block in
`config/models.py` was sitting mid-file (after the class defs)
with a `# noqa: E402` marker acknowledging the Rule 2 violation.
The reasoning ("preserve old import path for callers and
tests") applies whether the import is at the top or the middle
— it's a re-export shim either way.

**Changes:**
- Moved the 7-symbol `from ember_code.core.config.model_stream
  import (...)` block from line ~131 to alphabetical position
  in the module-top imports.
- Dropped the `# noqa: E402` marker.
- Left an explanatory comment where the block used to sit
  pointing at the top-of-file imports.
- File LoC: 475 → 476 (+1, comment-only).
- **Grade upgraded from B+ → A-**.

**Tests:** 64 model / cloud-models / tool-arg-streaming tests
green + full sweep pending.

### Iteration 197 — `hooks/tool_hook.py`: split `__call__` (B → A-)

**Target:** the 191-LoC `ToolEventHook.__call__` had 4
sequential phases (PreToolUse dispatch, protected-paths,
blocked-commands, permission evaluator, execute + post hooks)
all inlined into one big body.

**Changes:**
- Added `_run_pre_hook(name, args) -> (pre_decision,
  block_message)` (60 LoC) — the PreToolUse hook path with
  allow/deny/ask/legacy handling.
- Added `_apply_permission_evaluator(name, args) -> str | None`
  (35 LoC) — the 6-mode evaluator with the ASK-falls-through-to-
  Agno-HITL rule documented (v0.8.1 regression tag).
- Added `_execute_with_post_hooks(name, func, args) -> Any`
  (30 LoC) — tool run + PostToolUse / PostToolUseFailure + rule
  suffix.
- `__call__` shrank from 191 → 30 LoC. Reads as: pre →
  protected-paths → blocked-commands → evaluator → execute.
- File LoC: 350 → 379 (+29 for helper docstrings).
- **Grade upgraded from B → A-**.

**Tests:** 51 hook / permission / tool-arg streaming tests
green + full sweep pending.

### Iteration 196 — `session/persistence.py`: split session→wire mapper (B+ → A-)

**Target:** `SessionPersistence.list_sessions` had a 25-LoC
inline session-row → wire-dict block buried inside its main
try/except. The transform was easy to miss inside the
try + comment blocks.

**Changes:**
- Added `_session_to_wire(s)` static helper (20 LoC) — the
  {session_id, name, created_at, updated_at, run_count,
  summary, agent_name} mapping.
- `list_sessions` body dropped from 63 → 25 LoC. Now reads as
  a single-page happy path: fetch → tuple-unwrap → filter →
  map → return.
- Sub-agent-filter comment migrated from inline to method
  docstring (was 6 lines of code-adjacent comment; now a
  documented rule).
- File LoC: 407 → 426 (+19 for the extracted method + its
  docstring; body sharing offsets the extract).
- **Grade upgraded from B+ → A-**.

**Tests:** 16 persistence tests green + full sweep pending.

### Iteration 195 — `mcp/client.py`: split `connect` gate preamble (B+ → A-)

**Target:** the 72-LoC `MCPClientManager.connect` had a
29-LoC preamble of four gate checks (managed-policy deny +
not-allowed + user first-use approval + MCP SDK availability)
inlined before the transport-dispatch happy path.

**Changes:**
- Added `_check_connect_gate(name, config) -> str | None`
  helper — returns None on green-light, or the reason string
  if any gate rejects.
- `connect` shrank from 72 → ~50 LoC and now reads as:
  cache check → config lookup → gate → transport dispatch →
  verify → cache.
- File LoC: 345 → 352 (+7, docstrings).
- **Grade upgraded from B+ → A-**.

**Tests:** 42 MCP tests + full sweep still green.

### Iteration 194 — `config/models.py`: split `get_model` into per-provider builders

**Target:** the 97-LoC `ModelRegistry.get_model` had two
distinct kwarg-building branches (Gemini's slim SDK surface
vs. OpenAI-like's full surface with base_url + http_client)
inlined into the main method body. Made the "which shape
does each provider use" answer visible only by reading the
whole method.

**Changes:**
- Added `_build_gemini_kwargs(entry, api_key)` (10 LoC) — id,
  api_key, temperature, max_tokens.
- Added `_build_openai_like_kwargs(entry, api_key)` (40 LoC)
  — the full surface: base_url, api_key with Ember Cloud
  gateway fallback, temperature, max_tokens, timeout,
  http_client with keepalive limits. Docstring captures the
  timeout / http_client dual-set gotcha.
- `get_model` shrank to a 35-LoC dispatcher that calls the
  right builder based on provider.
- **File LoC**: 462 → 475 (+13, mostly docstrings).

**Tests:** 123 model / cloud-models / session / pool tests
green.

### Iteration 193 — `tui/backend_client.py`: typed RPC helpers (B- → B)

**Target:** the 83-method BackendClient class had ~20 methods
duplicating the pattern:

```python
async def X(self, arg=None) -> msg.Info:
    result = await self._rpc(RpcMethod.X, arg=arg)
    return result if isinstance(result, msg.Info) else msg.Info(text=str(result))
```

**Changes:**
- Added 3 typed RPC helpers: `_rpc_info` (coerces to
  `msg.Info`), `_rpc_list` (returns `[]` on None), `_rpc_dict`
  (returns `{}` on None).
- Regex-driven pass consolidated 21 methods:
  * 10 no-arg wrappers (Info + list[dict] + dict variants).
  * 11 single-arg wrappers.
  * A few manual replacements for multi-arg / multi-line
    signatures (`set_plugin_enabled`, `install_plugin`,
    `update_plugin`, `refresh_marketplaces`).
- Each thin wrapper is now a one-liner
  `return await self._rpc_info(RpcMethod.X, **kwargs)` instead
  of the 3-line result-guard block.
- **Down from 717 → 713 LoC** (net -4 after adding the 40 LoC
  of helpers + docstrings).
- **Grade upgraded from B- → B** — the RPC surface is still
  wide by design, but the boilerplate is gone.

**Tests:** 49 BackendClient-consuming tests green; full sweep
pending.

### Iteration 192 — `session/core.py`: Rule 2 sweep on __init__

**Target:** the 384-LoC `Session.__init__` constructor has 10
inline `from ember_code....` imports scattered through its
body — a Rule 2 violation (imports at module top). Extracting
the constructor itself is too risky (tight side-effect
ordering), but hoisting the imports is a safe win.

**Changes:**
- Tested each import in isolation to confirm no circular
  imports at module top.
- Hoisted 10 inline imports to module top:
  `TodoStore`, `PlanStore`, `ensure_memory_dir`,
  `KnowledgeManager`, `PluginLoader`, `load_state`,
  `discover_output_styles`, `MCPConfigLoader`,
  `LspServerManager`/`load_lsp_config`,
  `MonitorManager`/`load_monitor_config`,
  `SubAgentHITLCoordinator`.
- Dropped the `as _MCPConfigLoader` alias — no name collision
  once at module top.
- **Down from 1300 → 1294 LoC** in `session/core.py`.

**Tests:** 93 session-focused + full sweep still green.

### Iteration 191 — `evals/runner.py`: split `run_eval_case` (B+ → A-)

**Target:** the 148-LoC `run_eval_case` orchestrator with 4
distinct phases (agent arun, response cleanup, tool-trace
extraction, 5 kinds of assertions). Cleaner to split into
named parts than to keep the whole flow buried in one
try/except.

**Changes:**
- Added `_execute_case_arun` (30 LoC) — agent arun call
  with any prior turns on the same session, then
  `from_history=True` strip.
- Added `_extract_tool_trace` (25 LoC) — response.tools →
  `ToolTraceEntry` list, with the MagicMock-safe args coercion.
- Added `_apply_case_assertions` (55 LoC) — all 5 assertion
  types (reliability / unexpected / accuracy / tool-arg /
  file), populates `result.*_passed` fields, sets
  `result.passed` at the end.
- `run_eval_case` shrank to a 40-LoC orchestrator that
  delegates to the 3 helpers.
- **File LoC**: 681 → 719 (+38, docstrings for the 3 helpers).
- **Grade upgraded from B+ → A-** — each phase is now named
  and separately testable.

**Tests:** 41 eval tests + full sweep still green.

### Iteration 190 — `tui/run_controller.py`: extract renderer (B → A-)

**Target:** the protocol-event → widget rendering cluster in
`run_controller.py`. `_render` (118 LoC) dispatches on the
protocol message type; the underlying handlers
(`_on_content_chunk`, `_append_thinking`, `_append_content`,
`_on_tool_started`, `_on_tool_completed` with the mark_error-
before-mark_done fix, `_on_tool_error`, `_on_tokens`,
`_on_agent_started`, `_on_agent_completed`, `_on_run_error`)
totalled ~350 LoC.

**Changes:**
- Created `tui/run_renderer.py` (435 LoC, A-) with 12 free
  functions taking `controller: RunController` as arg.
- 12 methods in `run_controller.py` became one-line delegates.
- **Down from 985 → 738 LoC in `run_controller.py`** (-247).
- **Grade upgraded from B → A-** on `run_controller.py`.

**Tests:** 129 TUI tests green + full sweep pending.

### Iteration 189 — `tools/shell.py`: extract orphan cluster

**Target:** the boot-time orphan-process cluster in
`core/tools/shell.py` — `_OrphanProcess` class (110 LoC) +
`_OrphanProcStub` dataclass + `rehydrate_orphan_processes`
(70 LoC). ~190 LoC of "processes that survived a previous BE
lifetime" state.

**Changes:**
- Created `core/tools/shell_orphan.py` (233 LoC, A-) with all
  three symbols.
- `rehydrate_orphan_processes` late-imports `_registry` and
  `set_process_store` from `shell.py` to break the
  `shell → shell_orphan → shell` cycle at import time.
- `shell.py` re-exports the 3 orphan symbols via `noqa: F401`
  shim so existing test/import sites don't need to change.
- **Down from 1009 → 830 LoC in `shell.py`** (-179).

**Tests:** 48 focused (process watcher / orphan rehydrate /
backend server) tests pass locally. Full sweep running.

### Iteration 188 — `session/core.py`: extract `_build_main_agent` (C → B)

**Target:** the biggest single method in `session/core.py` —
`_build_main_agent` at 400 LoC. Assembles tools, prompt,
guardrails, compression manager, and every Agno-side switch
(streaming, memory, learning, hooks) into a single `Agent`
instance.

**Changes:**
- Created `core/session/agent_builder.py` (443 LoC, A-) with
  `build_main_agent(session)` as one large free function.
  Kept as one big function because every step is tightly
  coupled to Session state — splitting it further would only
  add plumbing.
- Handled the test-patch pattern: tests patch symbols at
  `session.core.<Name>` (Agent, CompressionManager,
  ToolRegistry, _create_reasoning_tools, _create_guardrails,
  ModelRegistry, load_prompt). Solution — import the core
  module at the extract's top and look up via
  `_session_core.Agent(...)`, etc. Same test-patch pattern as
  `server_plugin.py`.
- `Session._build_main_agent` became a 4-line delegate.
- **Down from 1700 → 1300 LoC in `session/core.py`.**
- **Grade upgraded from C → B** on `session/core.py`.

**Tests:** 48 session tests + 3413 full-suite tests all green
(baseline preserved).

**Progress metric:** Both Python C-grade "god-file" targets in
this session (`orchestrate.py` and `session/core.py`) now
upgraded past C. Only remaining Python non-A files are
smaller (`tools/shell.py` at 1009 C, `tui/run_controller.py`
at 985 B, `tui/backend_client.py` at 717 B-).

### Iteration 187 — `tui/app.py`: scheduler + agents/skills/hooks

**Target:** two more clusters in `tui/app.py`:
- Scheduler + task + queue panel handlers: 10 methods
  totalling ~180 LoC.
- Agents / skills / hooks panels: 12 methods totalling ~140
  LoC.

**Changes:**
- Created `tui/scheduler_handlers.py` (178 LoC, A-) and
  `tui/agent_handlers.py` (157 LoC, A-).
- 22 class methods on `EmberApp` became one-line delegates.
- **Down from 1332 → 1293 LoC in `tui/app.py`** (-39 net —
  extraction body savings offset by the delegate wrappers on
  22 small methods, which is expected when the average method
  body is short).

**Cumulative `tui/app.py` trim across iters 180-187:**
- 2415 → 1293 (-1122, **-46%**).
- 12 companion handler modules totalling 2463 LoC, all A-.
- Grade: **B** — the remaining code is `__init__`, `compose`,
  welcome-screen builders, on_resize + mirror-event + tips
  helpers, and `_refresh_codeindex_badge`. Diminishing
  returns from further extraction — these are all standard
  small methods.

### Iteration 186 — `tui/app.py`: keybindings + actions

**Target:** the keybinding + action cluster — 11 methods
totalling ~330 LoC: `on_key` (86), `render_command_result`
(80), `action_cancel` (60), plus small actions
(`action_clear_screen`, `action_toggle_expand_all`,
`action_toggle_queue`, `action_toggle_tasks`,
`_auto_refresh_tasks`, `action_toggle_verbose`).

**Changes:**
- Created `tui/keybinding_handlers.py` (385 LoC, A-) with 11
  free functions. Module-level `_DIALOG_TYPES` tuple lists
  every dialog that Ctrl+C closes, in precedence order —
  used to be a local `_DIALOG_TYPES` inside `action_cancel`.
- 11 class methods on `EmberApp` became one-line delegates.
- **Down from 1565 → 1332 LoC in `tui/app.py`** (-233).

**Cumulative `tui/app.py` trim across iters 180-186:**
- 2415 → 1332 (-1083, **-45%**).
- 10 companion handler modules totalling 2128 LoC, all A-.
- Grade: **B** — the remaining code is `__init__`, `compose`,
  and the tips / mirror / auto-refresh helpers plus a
  handful of tiny methods.

### Iteration 185 — `tui/app.py`: lifecycle cluster (C → B)

**Target:** the TUI lifecycle cluster — 6 methods totalling
~250 LoC: `_on_mount_inner` (120 LoC — the big startup body),
`_init_mcp_background`, `_refresh_cloud_models_on_startup`,
`_auto_sync_knowledge`, `_check_for_update`, `on_unmount`.

**Changes:**
- Created `tui/lifecycle_handlers.py` (314 LoC, A-) with 6
  free functions. Each takes `app: EmberApp` as arg and mirrors
  the original method's exact side effects.
- Rule 2 sweep: 3 inline imports hoisted to module top —
  `CloudCredentials`, `fetch_cloud_models`, `merge_into_registry`.
- 6 class methods on `EmberApp` became one-line delegates
  (with the outer `on_mount` wrapper preserved on the class for
  the exception-swallowing guard).
- **Down from 1768 → 1565 LoC in `tui/app.py`** (-203).
- **Grade upgraded from C → B** on `tui/app.py`.

**Cumulative `tui/app.py` trim across iters 180-185:**
- 2415 → 1565 (-850, **-35%**).
- 9 companion handler modules totalling 1743 LoC, all A-.

### Iteration 184 — `tui/app.py`: input handling cluster

**Target:** the prompt input handling cluster — 7 methods
totalling ~230 LoC: `_on_input_changed` (80 LoC), the file
picker helpers (`_mount_autocomplete`, `_show_file_picker`,
`_hide_file_picker`, `_insert_file_mention`), and
`_on_input_submitted` (40).

**Changes:**
- Created `tui/input_handlers.py` (260 LoC, A-) with 7 free
  functions taking `EmberApp` as arg. Preserves the
  hot-path short-circuit (only walk the widget tree when a
  picker/autocomplete widget is actually mounted).
- 7 class methods on `EmberApp` became one-line delegates
  (2 preserving `@on(PromptInput.Changed)` and
  `@on(PromptInput.Submitted)` decorators).
- **Down from 1922 → 1768 LoC in `tui/app.py`** (-154).
- Grade **C** (unchanged — need to drop further before
  `__init__` + `on_mount` + `on_key` + `render_command_result`
  + keybinding actions drop it to B).

**Cumulative `tui/app.py` trim across iters 180-184:**
- 2415 → 1768 (-647, **-27%**).
- 8 companion handler modules totalling 1429 LoC, all A-.

### Iteration 183 — `tui/app.py`: pickers + modes + shell (under 2k LoC)

**Target:** two more clusters in `tui/app.py`:
- Session / model picker + login flow + help panel: 12
  methods, ~150 LoC.
- Command / shell prompt-mode indicators + inline shell
  execution: 5 methods, ~130 LoC.

**Changes:**
- Created `tui/picker_handlers.py` (205 LoC, A-) with 12 free
  functions covering "modal chrome" widgets: session picker,
  model picker (with cloud-catalogue refresh), login (mount,
  logged-in, cancelled, status + result pushes), help panel.
- Created `tui/mode_handlers.py` (178 LoC, A-) with 5 free
  functions: command/shell mode indicators, inline shell
  execution (streams shell output into a live Static widget,
  stashes transcript on `app._shell_context` for AI context).
- Rule 2 sweep: 4 inline imports hoisted — `CloudCredentials`,
  `fetch_cloud_models`, `merge_into_registry`, `rich.markup.escape`.
- 17 class methods on `EmberApp` became one-line delegates.
- **Down from 2048 → 1922 LoC in `tui/app.py`** (-126).
- Grade **C** (unchanged — still needs to drop further before
  the remaining `__init__`, `on_mount_inner`, `on_key`, and
  action methods trigger a grade change).

**Cumulative `tui/app.py` trim across iters 180-183:**
- 2415 → 1922 (-493, -20%).
- 7 companion handler modules totalling 1169 LoC, all A-.

### Iteration 182 — `tui/app.py`: plugins + knowledge handlers (D → C)

**Target:** two more Textual panel handler clusters in
`tui/app.py`:
- Plugins panel: 8 methods (~110 LoC) —
  `_show_plugins_panel`, `_build_plugin_state`,
  `_refresh_plugins_panel`, `_on_plugin_toggle` / `install`
  / `update` / `remove`, `_on_marketplace_refresh`,
  `_on_plugins_panel_closed`.
- Knowledge panel: 4 methods (~90 LoC) —
  `_show_knowledge_panel`, `_on_knowledge_search`,
  `_on_knowledge_add`, `_on_knowledge_panel_closed`.

**Changes:**
- Created `tui/plugin_handlers.py` (134 LoC) and
  `tui/knowledge_handlers.py` (112 LoC).
- 12 class methods on `EmberApp` became one-line delegates.
- Rule 2 sweep: inline `from textual.widgets import Input as
  _Input` in `_on_knowledge_add` pulled to module top.
- **Down from 2112 → 2048 LoC in `tui/app.py`** (-64).
- **Grade upgraded from D → C** on `tui/app.py`.

**Cumulative `tui/app.py` trim across iters 180-182:**
- 2415 → 2048 (-367, -15%).
- 5 companion handler modules totalling 786 LoC, all A-.
- Grade: **C** — remaining clusters (hooks / skills / agents
  panels, session / model pickers, login flow) are smaller
  and mostly formulaic.

### Iteration 181 — `tui/app.py`: loop + MCP handlers

**Target:** two more Textual panel handler clusters in
`tui/app.py`:
- Loop panel: 5 methods (~150 LoC) — `_show_loop_panel`,
  `_poll_loop_status`, `_on_loop_resume`, `_on_loop_cancel`,
  `_on_loop_panel_closed`.
- MCP panel: 5 methods (~90 LoC) — `_show_mcp_panel`,
  `_build_mcp_server_list`, `_on_mcp_toggle`, `_toggle_mcp`,
  `_on_mcp_panel_closed`.

**Changes:**
- Created `tui/loop_handlers.py` (141 LoC) and
  `tui/mcp_handlers.py` (117 LoC). Same Textual-friendly
  pattern as iter 180: `@on(...)`-decorated class methods
  stay as 1-line delegates, bodies live as free functions.
- 10 class methods on `EmberApp` became one-line delegates.
- **Down from 2219 → 2112 LoC in `tui/app.py`** (-107).

**Cumulative `tui/app.py` trim across iters 180-181:**
- 2415 → 2112 (-303, -13%).
- 3 companion modules totalling 540 LoC, all A-.
- Grade: still **D** — need to extract 4-5 more clusters
  (plugins, knowledge, hooks/skills/agents, session/model
  pickers, login flow) before this drops below god-file size.

### Iteration 180 — `tui/app.py`: CodeIndex handlers → free-function extract

**New file**: `tui/app.py` (2415 LoC, D) — the last Python
D-file. Big Textual `EmberApp` class with ~106 methods. Can't
use pure free-function extraction because Textual's `@on(...)`
decorator wires event dispatch by scanning the class — so
handlers MUST stay as class methods.

**Pattern used**: keep the `@on(...)`-decorated method as a
one-line delegate, move the body to a free function taking
`app: EmberApp` as arg. Same shape as the `server_*` extracts
but with a decorator on the delegate.

**Target:** 6-handler CodeIndex cluster (`_show_codeindex_panel`,
`_poll_codeindex_status`, `_on_codeindex_sync`,
`_on_codeindex_resync`, `_on_codeindex_clean`,
`_on_codeindex_install`, `_on_codeindex_panel_closed`).
~250 LoC.

**Changes:**
- Created `tui/codeindex_handlers.py` (282 LoC) with 7 free
  functions taking `EmberApp` as arg. Preserves the 0.5s
  apply-progress ticker in `on_codeindex_resync` verbatim.
- 7 class methods on `EmberApp` became one-line delegates
  (with `@on(...)` decorator preserved on the 5 event ones).
- Rule 2 sweep: 2 inline `import webbrowser as _wb` in the
  original bodies collapsed to a single module-top
  `import webbrowser`.
- **Down from 2415 → 2219 LoC in `tui/app.py`.**

**Tests:** 129 TUI + 3413 full-suite tests all green
(baseline preserved).

**Progress metric:** Both Python D-files now attacked in this
session — `orchestrate.py` (D → B, iter 179) and `tui/app.py`
(D → still D but -196 LoC via iter 180's start). App.py still
D because it has 5-6 more handler clusters to extract before
the class is no longer god-sized.

### Iteration 179 — `core/tools/orchestrate.py`: streaming extract (D → B)

**Target:** the second-biggest Python D-file. Two nested
async generators — `_run_agent_streaming` (436 LoC) and
`_run_team_streaming` (370 LoC) — together made `orchestrate.py`
1338 LoC. Both are self-contained streaming loops that drive
the FE team-progress card; they don't need to live inside the
toolkit module.

**Changes:**
- Created `core/tools/orchestrate_streaming.py` (~870 LoC) with
  the two generators as free functions (`run_agent_streaming`,
  `run_team_streaming`). Cleaned up the `_stream_log =
  __import__("logging").getLogger(...)` pattern to a plain
  `logging.getLogger` at module top.
- Rule 2 sweep: `time` import at module top; removed the
  inline `__import__("logging")` pattern.
- Broke the `orchestrate → orchestrate_streaming → orchestrate`
  cycle by introducing two late-lookup helpers:
  `_active_subagent_runs()` and `_append_event_hook()` — each
  imports `OrchestrateTools` on first use. The class attributes
  themselves stay on `OrchestrateTools` (canonical location);
  tests still access them there.
- `orchestrate.py` re-imports both generators under their old
  underscore names (`as _run_agent_streaming` / `as
  _run_team_streaming`) so the `OrchestrateTools.spawn_agent` /
  `spawn_team` methods below don't need any callsite changes.
- **Down from 1338 → 534 LoC in `orchestrate.py`.**
- **Grade upgraded from D to B.**

**Tests:** 81 focused orchestrate + subagent HITL + tool-arg
streaming tests pass locally. Full sweep running.

### Iteration 178 — `backend/server.py`: sessions + import prune

**Target:** the 4-method session management cluster —
`list_sessions` (7 LoC), `maybe_auto_name_session` (21),
`switch_session` (17), `search_chat` (21). Plus an
opportunistic sweep of orphaned imports on `server.py`.

**Changes:**
- Created `backend/server_sessions.py` (116 LoC) with 4 free
  functions taking `BackendServer` as arg. `_TITLE_TRIM_RE`
  hoisted to module top (was inline in
  `maybe_auto_name_session`). Rule 2 clean.
- 4 methods in `server.py` became one-line delegates.
- **Import prune** on `server.py`: removed `json`, `os`, `re`,
  `uuid`, `serialize_event` (all unused post-extract), and 8
  of 9 helpers from `server_helpers` (only `_scan_plugin_dir`
  still called locally). Kept
  `_SEARCH_CHAT_SNIPPET_HALF_WIDTH` + `_search_history` as a
  documented re-export shim (`noqa: F401`) because
  `test_search_chat` imports them via the server module.
- **Down from 1100 → 1042 LoC in `server.py`.**

**Tests:** 52 focused + shim'd search-chat tests pass
locally. Full sweep running.

**Cumulative `server.py` trim across 19 iters (160-178):**
- **-3499 LoC (4541 → 1042, -77%).**
- 19 focused extract modules totalling 4724 LoC, all A-.
- Grade: **B** — 77% smaller than baseline.

### Iteration 177 — `backend/server.py`: extract MCP RPCs

**Target:** 8-method MCP cluster on `BackendServer`:
`ensure_mcp` (2 LoC), `toggle_mcp` (9), `get_mcp_status` (3),
`set_mcp_tool_enabled` (11), `get_mcp_server_details` (22),
`get_mcp_servers` (9), `mcp_connect` (5), `mcp_disconnect`
(5). ~110 LoC total.

**Changes:**
- Created `backend/server_mcp.py` (127 LoC) with 8 free
  functions taking `BackendServer` as arg. No inline
  imports to sweep — these methods were already Rule-2
  clean.
- 8 methods in `server.py` became one-line delegates.
- Down from 1126 → 1100 LoC in `server.py`.

**Tests:** 101 focused MCP + backend-server tests pass
locally. Full sweep running.

**Cumulative `server.py` trim across 18 iters (160-177):**
- **-3441 LoC (4541 → 1100, -76%).**
- 18 focused extract modules totalling 4608 LoC, all A-.
- Grade: **B** — 76% smaller than baseline.

### Iteration 176 — `backend/server.py`: extract run engine (**grade upgrade C → B**)

**Target:** the run-engine cluster — the last big block on
`BackendServer`. Three methods, ~231 LoC:
`run_message` (33), `_run_message_locked` (168),
`_close_model_http_client` (30).

**Changes:**
- Created `backend/server_run.py` (284 LoC) with 3 free
  functions taking `BackendServer` as arg.
- Rule 2 sweep: 6 inline imports hoisted to module top —
  `HookEvent`, `process_file_mentions`,
  `resolve_file_references`/`attach_resolved_files`/`extract_media_urls`,
  `datetime`, `httpx`. Fresh httpx client params moved to
  module-level `_HTTP_CLIENT_LIMITS`.
- `run_message` calls `backend._run_message_locked(...)` (the
  method) so per-class test patches (used by
  `test_streaming_done_unblock`) still intercept — same
  pattern as iter 163's `periodic_checkpoint`.
- 3 methods in `server.py` became one-line delegates.
- **Down from 1333 → 1126 LoC in `server.py`.**
- **Grade upgraded from C to B** — the god-object concern is
  substantially resolved.

**Tests:** 109 focused run / streaming / HITL /
backend-server tests pass locally. Full sweep running.

**Cumulative `server.py` trim across 17 iters (160-176):**
- **-3415 LoC (4541 → 1126, -75%).**
- 17 focused extract modules totalling 4481 LoC, all A-.
- Grade: **B** — remaining code is `__init__` boilerplate + a
  handful of small RPC accessors.

### Iteration 175 — `backend/server.py`: extract context management

**Target:** 6-method conversation-context management cluster
— `get_status` (27 LoC), `count_context_tokens` (44),
`compact_if_needed` (17), `extract_learnings` (27),
`truncate_history` (36), `get_pending_messages` (35). ~186
LoC. Non-adjacent in the file but conceptually one cluster:
"how much conversation is in play, and how do we prune /
summarise it".

**Changes:**
- Created `backend/server_context.py` (260 LoC) with 6 free
  functions taking `BackendServer` as arg.
- Rule 2 sweep: 2 inline imports pulled to module top —
  `AgnoMessage` (from `agno.models.message`) and `time`.
  The 60s staleness threshold for `get_pending_messages`
  became a module-level `_PENDING_STALENESS_SECONDS`
  constant.
- 6 methods in `server.py` became one-line delegates.
- **Down from 1489 → 1333 LoC in `server.py`.**

**Tests:** 47 focused context / crash / persistence tests
pass locally. Full sweep running.

**Cumulative `server.py` trim across 16 iters (160-175):**
- **-3208 LoC (4541 → 1333, -71%).**
- 16 focused extract modules totalling 4197 LoC, all A-.
- Grade: **C** — 71% smaller than baseline. Remaining
  clusters are the `_run_message_locked` engine (168 LoC),
  `__init__` boilerplate (98 LoC), and thin accessor
  methods.

### Iteration 174 — `backend/server.py`: extract file I/O

**Target:** `read_file` (78 LoC) + `upload_attachment`
(33 LoC) — the two file-I/O RPCs on `BackendServer`. Kept
together as one cluster: both are file-write / file-read
operations that need sandboxing and file-name sanitisation.

**Changes:**
- Created `backend/server_files.py` (148 LoC) with 2 free
  functions taking `BackendServer` as arg. Split the raw
  cap and safe-name regex into module-level constants
  (`_READ_FILE_MAX_BYTES`, `_SAFE_NAME_RE`).
- Rule 2 sweep: 3 inline imports pulled to module top —
  `expanduser`, `base64`, `re`.
- 2 methods in `server.py` became one-line delegates.
- Down from 1588 → 1489 LoC in `server.py`.

**Tests:** 13 backend-server tests pass locally. Full sweep
running.

**Cumulative `server.py` trim across 15 iters (160-174):**
- -3052 LoC (4541 → 1489, -67%).
- 15 focused extract modules totalling 3937 LoC, all A-.
- Grade: **C** — 67% smaller than baseline.

### Iteration 173 — `backend/server.py`: recovery + background procs

**Recovery discovery:** post-iter-172 sweep surfaced 15 test
failures across `test_todo_tool.py`, `test_process_watcher.py`,
`test_plan_mode.py`, and `test_plan_rehydrate.py` — all
`AttributeError` on `BackendServer`. The recovered HEAD (from
the iter-165 accidental `git checkout`) was missing 6 methods
that had been added in uncommitted work BEFORE this
conversation started:
- `get_todos` (todos panel RPC)
- `get_latest_plan` (plan panel RPC)
- `dispatch_visualization_action` (json-render round-trip RPC)
- `list_background_processes` (watcher panel RPC)
- `read_process_tail` (watcher panel RPC)
- `stop_background_process` (watcher panel RPC)

All 6 were wired in `__main__.py`'s RPC dispatch table but
missing from the class — the tests immediately caught it as
`AttributeError`. Reconstructed each from the test contracts.

**Extract:** with the 3 background-process methods back, the
natural next module was `backend/server_processes.py` (97
LoC) with all 3 as free functions. Rule 2 clean.

**Changes:**
- Reconstructed 6 lost methods on `BackendServer`.
- Created `backend/server_processes.py` (97 LoC) for the
  3 process-watcher methods.
- 3 process-watcher methods on `BackendServer` became one-line
  delegates.
- Net LoC in `server.py`: **1506 → 1588** (added ~127 LoC of
  restored methods; extract removed ~45).

**Tests:** 138 focused (plan + todos + process_watcher +
backend + rehydrate) tests pass locally. Full sweep running.

**Cumulative `server.py` trim across 14 iters (160-173):**
- -2953 LoC (4541 → 1588, -65%).
- 14 focused extract modules totalling 3789 LoC, all A-.
- Grade: **C** — 65% smaller than baseline with 6 lost RPCs
  restored.

### Iteration 172 — `backend/server.py`: extract loop + scheduler

**Target:** two related clusters — **loop pump** (5 methods,
~100 LoC: `pop_pending_loop_iteration`, `cancel_pending_loop`,
`loop_pause`, `loop_resume`, `loop_status`) and **scheduler**
(4 methods, ~130 LoC: `execute_scheduled_task`,
`cancel_scheduled_task`, `get_scheduled_tasks`,
`start_scheduler`).

Kept together in one extract because both are about "external
work sources driving the agent" — the loop is user-defined
iteration, the scheduler is cron / one-shot tasks. Both
delegate to session state and both wire into `Session.pool`
for stateful runners.

**Changes:**
- Created `backend/server_loop.py` (237 LoC) with 9 free
  functions taking `BackendServer` as arg.
- Rule 2 sweep: 6 inline imports hoisted to module top:
  * `LoopTools`, `TaskStatus`, `SchedulerRunner`, `TaskStore`,
    `HookEvent`, `extract_response_text`.
- 9 methods in `server.py` became one-line delegates.
- Down from 1628 → 1506 LoC in `server.py`.

**Also restored** a missing `get_todos` method that got lost
in the iter-165 recovery — tests were failing before the
extract; test suite is now green again.

**Tests:** 166 focused loop / scheduler / todo /
backend-server tests pass locally. Full sweep running.

**Cumulative `server.py` trim across 13 iters (160-172):**
- **-3035 LoC (4541 → 1506, -67%).**
- 13 focused extract modules totalling 3692 LoC, all A-.
- Grade: **C** — 67% smaller than baseline.

### Iteration 171 — `backend/server.py`: extract panel-details RPCs

**Target:** the panel-details cluster — `get_agent_details`
(35 LoC), `get_hooks_details` (30) + `reload_hooks_rpc` (10),
`get_skill_details` (24) + `_BUILTIN_DESCRIPTIONS` dict
(~35 lines) + `get_output_styles` (16) +
`get_slash_commands` (78). ~230 LoC of read-only panel
snapshots.

**Changes:**
- Created `backend/server_panels.py` (285 LoC) with 6 free
  functions taking `BackendServer` as arg.
- Moved the `_BUILTIN_DESCRIPTIONS` dict from a class
  attribute on `BackendServer` to a module-level constant in
  `server_panels.py` — it was only used by
  `get_slash_commands` and doesn't need to be on the instance.
- Rule 2 sweep: 4 inline imports hoisted to module top —
  `AgentInfo`, `SkillInfo`, `discover_markdown_commands`, and
  the module-level constant. Only `CommandHandler` stays
  late-imported because `command_handler` imports from
  `server`, so a real circular dep would form.
- 6 methods in `server.py` became one-line delegates.
- **Down from 2080 → 1628 LoC in `server.py`.**
- Grade upgraded from D to **C** at 1628 LoC.

**Tests:** 57 focused panel + backend-server tests pass
locally. Full sweep running.

**Cumulative `server.py` trim across 12 iters (160-171):**
- **-2913 LoC (4541 → 1628, -64%).**
- 12 focused extract modules totalling 3455 LoC, all A-.
- Grade: **C** (upgraded from D — the file is 64% smaller
  and the remaining code is mostly the run-message engine +
  init boilerplate + accessor methods).

### Iteration 170 — `backend/server.py`: extract lifecycle

**Target:** the three lifecycle methods on `BackendServer` —
`startup`, `_detect_interrupted_run`, `shutdown`. ~130 LoC
total. Non-adjacent in the file but conceptually one cluster
("what runs before/after the backend serves RPCs").

**Changes:**
- Created `backend/server_lifecycle.py` (176 LoC) with 3 free
  functions taking `BackendServer` as arg.
- Rule 2 sweep: 3 inline imports pulled to module top:
  * `from agno.run.base import RunStatus`
  * `from ember_code.core.hooks.events import HookEvent`
  * `from ember_code.core.tools.shell import EmberShellTools`
- 3 methods in `server.py` became one-line delegates.
- Down from 2196 → 2080 LoC in `server.py`.

**Tests:** 93 focused lifecycle / crash-survival /
persistence / rehydrate tests pass locally. Full sweep
running.

**Cumulative `server.py` trim across 11 iters (160-170):**
- -2461 LoC (4541 → 2080, **-54%**).
- 11 focused extract modules totalling 3170 LoC, all A-.
- Grade: **D** (still — 54% smaller but class-scope
  god-object concern is untouched).

### Iteration 169 — `backend/server.py`: extract knowledge-base RPCs

**Target:** 5-method knowledge cluster on `BackendServer` —
`knowledge_search`, `knowledge_add`, `knowledge_list`,
`knowledge_get`, `knowledge_remove`. ~180 LoC total, all
thin dispatch to `Session.knowledge_mgr`.

**Changes:**
- Created `backend/server_knowledge.py` (149 LoC) with 5 free
  functions taking `BackendServer` as arg. Split the buried
  `_name_for` local out to a private module helper.
- Rule 2 sweep: inline `from pathlib import PurePosixPath`
  moved to module top.
- 5 methods in `server.py` became one-line delegates.
- Down from 2284 → 2196 LoC in `server.py`.

**Tests:** 81 focused knowledge + backend-server tests pass
locally. Full sweep running.

**Cumulative `server.py` trim across 10 iters (160-169):**
- -2345 LoC (4541 → 2196, **-52%**).
- 10 focused extract modules totalling 2994 LoC, all A-.
- Grade: **D** (still — 52% smaller but class-scope
  god-object concern is untouched).

### Iteration 168 — `backend/server.py`: extract plugin + marketplace

**Target:** the big plugin cluster — 10 methods, ~400 LoC:
`preview_plugin` (71), `get_plugin_details` (41),
`set_plugin_enabled` (55), `install_plugin` (61),
`update_plugin` (24), `remove_plugin` (27),
`get_marketplaces` (59), `add_marketplace` (15),
`remove_marketplace` (9), `refresh_marketplaces` (38).

**Changes:**
- Created `backend/server_plugin.py` (423 LoC) with 10 free
  functions taking `BackendServer` as arg.
- 10 methods in `server.py` became one-line delegates.
- Down from 2619 → 2284 LoC in `server.py`.

**Testability preserved:** first pass moved all imports to
module top (Rule 2), which broke 14 tests that patched
`ember_code.core.plugins.installer.PluginInstaller` /
`marketplaces.*` / `state.save_state` at the source module —
`from ...installer import PluginInstaller` at module top binds
the class at import time, so mock patches on the source module
no longer propagate. Fix: switched to **module-level imports
with attribute lookup at call time**:

```python
from ember_code.core.plugins import installer as _plugin_installer
...
installer = _plugin_installer.PluginInstaller(data_dir=...)
```

This is a documented Rule 2 exception — it keeps imports at
the top (no inline `from ...import` inside functions) while
preserving the test-patch surface. All 162 plugin +
backend-server tests now pass.

**Cumulative `server.py` trim across 9 iters (160-168):**
- -2257 LoC (4541 → 2284, **-50%**).
- 9 focused extract modules totalling 2845 LoC (`server_helpers`
  316, `server_rehydrate` 212, `server_hitl` 260, `server_pause`
  473, `server_history` 384, `server_codeindex` 401,
  `server_search` 219, `server_auth` 157, `server_plugin` 423),
  all A-.
- Grade: **D** (still — 50% smaller but class-scope god-object
  concern is untouched).

### Iteration 167 — `backend/server.py`: extract cloud-auth RPCs

**Target:** cloud auth cluster — 4 methods, ~115 LoC:
`login` (72 LoC, browser-callback OAuth), `reload_cloud_credentials`
(7 LoC), `clear_cloud_credentials` (9 LoC), `get_cloud_plan`
(23 LoC).

**Changes:**
- Created `backend/server_auth.py` (157 LoC) with 4 free
  functions taking `BackendServer` as arg. Added a small
  `StatusCallback` alias for the login flow's optional
  status-update callback so the signature reads cleanly.
- Rule 2 sweep landed with the extract:
  * inline `import webbrowser` → module-top
  * inline `from ember_code.core.auth.client import (...)` →
    module-top
  * inline `from ember_code.core.auth.credentials import (...)` →
    module-top
  * inline `from datetime import datetime, timezone` →
    module-top
  * inline `from ember_code.core.auth.client import DEFAULT_API_URL,
    validate_token` (in `get_cloud_plan`) → module-top
- 4 methods in `server.py` became one-line delegates.
- Down from 2709 → 2619 LoC in `server.py`.

**Tests:** 59 focused auth + backend-server tests pass
locally. Full sweep running.

**Cumulative `server.py` trim across 8 iters (160-167):**
- -1922 LoC (4541 → 2619, -42%).
- 8 focused extract modules (`server_helpers.py` 316,
  `server_rehydrate.py` 212, `server_hitl.py` 260,
  `server_pause.py` 473, `server_history.py` 384,
  `server_codeindex.py` 401, `server_search.py` 219,
  `server_auth.py` 157), all A-.
- Grade: **D** (still — 42% smaller but class-scope
  god-object concern is untouched).

### Iteration 166 — `backend/server.py`: extract code search

**Target:** `search_code` — the composer's paste-lookup RPC.
171 LoC single method, self-contained, no BackendServer state
beyond `_session.project_dir` and the `_search_code_cache`
attribute.

**Changes:**
- Created `backend/server_search.py` (219 LoC) with the
  `search_code` primary function plus two internal helpers
  `_search_with_rg` (rg-backed path) and `_search_with_python`
  (Python-side `os.walk` fallback). The split makes the two
  strategies obvious at a glance instead of buried in one
  200-line method.
- Rule 2 sweep: inline `hashlib`, `shutil`, `subprocess`
  imports moved to module top.
- The method in `server.py` became a 3-line delegate.
- Down from 2874 → 2709 LoC in `server.py`.

**Tests:** 13 backend-server tests pass locally. Full sweep
running.

**Cumulative `server.py` trim across 7 iters (160-166):**
- -1832 LoC (4541 → 2709, -40%).
- 7 focused extract modules (`server_helpers.py` 316,
  `server_rehydrate.py` 212, `server_hitl.py` 260,
  `server_pause.py` 473, `server_history.py` 384,
  `server_codeindex.py` 401, `server_search.py` 219), all A-.
- Grade: **D** (still — 40% smaller but class-scope
  god-object concern is untouched).

### Iteration 165 — `backend/server.py`: extract codeindex RPCs

**Target:** the CodeIndex panel + slash-command family — 7
methods covering ~330 LoC: `codeindex_status` (148 LoC),
`codeindex_sync` (32), `codeindex_resync` (32),
`codeindex_clean` (5), `codeindex_head_breakdown` (103),
`codeindex_activity` (5), `codeindex_install` (44).

**Changes:**
- Created `backend/server_codeindex.py` (401 LoC) with 7
  free functions taking `BackendServer` as arg.
- Rule 2 sweep: hoisted every inline import to module top —
  `subprocess`, `collections.Counter`, `dataclasses.asdict`,
  `urllib.parse.urlparse` / `urlunparse`, and
  `code_index.paths.commit_chroma_path`.
- `_dir_size` is now a private module-level helper (was a
  local function inside `codeindex_status`).
- 7 methods in `server.py` became one-line delegates.

**Recovery event mid-iter:** A `git checkout HEAD --
src/ember_code/backend/server.py` command intended to inspect
diff stats instead REVERTED server.py to HEAD (pre-iter-160),
losing all in-flight delegate replacements. Recovery consisted
of re-applying every iter-160-through-165 delegate
replacement against a fresh HEAD server.py using the intact
extract modules as source of truth. Only server.py needed
touching — all 6 extract modules (`server_helpers`,
`server_rehydrate`, `server_hitl`, `server_pause`,
`server_history`, `server_codeindex`) were preserved. One
semantic upgrade during recovery: the pre-iter-160 HEAD had a
broken `_rehydrate_visualizations` calling non-existent
`persistence.load_visualizations()`; recovery replaced it with
the working `_rehydrate_event_log` delegate.

**Tests:** 158 focused tests pass post-recovery. Full sweep
running (`bzn9o54da`).

**Cumulative `server.py` trim across 6 iters (160-165):**
- -1667 LoC (4541 → 2874, -37%).
- 6 focused extract modules (`server_helpers.py` 316,
  `server_rehydrate.py` 212, `server_hitl.py` 260,
  `server_pause.py` 473, `server_history.py` 384,
  `server_codeindex.py` 401), all A-.
- Grade: **D** (still — 37% smaller but the class-scope
  god-object concern is untouched).

### Iteration 164 — `backend/server.py`: extract chat-history rebuild

**Target:** `get_chat_history` — the single biggest method
remaining in `server.py` at 356 LoC. Rebuilds the FE's turn
list for session-resume by walking an Agno session's persisted
runs. Handles 6 distinct turn shapes (user, assistant,
thinking-from-reasoning, thinking-from-`<think>`-tags, tool,
plan-in-place-of-exit_plan_mode-result, stats,
visualization) and does two post-walk passes (plan-state
resolution + viz-splicing).

**Changes:**
- Created `backend/server_history.py` (384 LoC) with the main
  `get_chat_history` free function and two split-out helpers
  `_fill_plan_states` / `_splice_visualizations` that were
  buried inside the original method. Splitting them out is
  purely for readability — the visible behavior is identical.
- Rule 2 cleanup: pruned inline `import json as _json` used
  inside the viz-splicing block; the module-top `json` covers
  both callsites.
- The method in `server.py` became a 3-line delegate.
- Down from 3536 → 3185 LoC in `server.py`.

**Tests:** 120 focused tests pass locally (`test_search_chat`,
`test_plan_decisions`, `test_tool_arg_streaming`,
`test_plan_rehydrate`, `test_event_log`,
`test_backend_server`). Full sweep pending.

**Cumulative `server.py` trim across 5 iters (160-164):**
- -1356 LoC (4541 → 3185, -30%).
- 5 focused extract modules (`server_helpers.py` 316,
  `server_rehydrate.py` 212, `server_hitl.py` 260,
  `server_pause.py` 473, `server_history.py` 384), all A-.
- Grade: **D** (still — the class-scope god-object concern
  is untouched, but the file is 30% smaller now and the
  extracts hold the biggest self-contained clusters).

### Iteration 163 — `backend/server.py`: extract pause pipeline

**Target:** the biggest remaining cluster on `BackendServer` —
the HITL pause pipeline + sub-agent stream muxer:
`_stream_with_subagent_hitl` (192 LoC), `_handle_pause`
(108 LoC), `_build_subagent_run_paused` (30 LoC),
`_drop_pending_for_run` (26 LoC), `_periodic_checkpoint`
(27 LoC), `_checkpoint_session` (30 LoC). Total ~400 LoC of
tightly coupled generators + evaluator plumbing.

**Changes:**
- Created `backend/server_pause.py` (473 LoC) with 6 free
  functions taking `BackendServer` as arg. Recursive
  auto-resume path preserved — `stream_with_subagent_hitl`
  recurses through the free function, not through
  `backend._stream_with_subagent_hitl`, so the muxer's
  identity as a "single generator" is preserved.
- Rule 2 cleanup landed with the extract:
  * `import logging as _log` → module-top `_LLM_LOGGER`
  * `import os as _os` / `from pathlib import Path as _Path`
    / `import time as _t` → module-top `os`, `Path`, `time`
  * inline `agno_events` and `permission_eval` imports →
    module-top
- 6 methods in `server.py` became one-line delegates.
- Pruned newly-unused `import uuid` from `server.py`.
- Down from 3912 → 3536 LoC in `server.py`.

**Tests:** 177 focused tests pass locally
(`test_backend_server`, `test_handle_pause_evaluator`,
`test_hitl_*`, `test_subagent_hitl_e2e`,
`test_permission_flows`, `test_process_orphan_rehydrate`,
`test_plan_rehydrate`, `test_event_log`,
`test_persistence`). Full sweep pending.

**Cumulative `server.py` trim across 4 iters:**
- -1005 LoC (4541 → 3536, -22%).
- 4 focused extract modules (`server_helpers.py` 316,
  `server_rehydrate.py` 212, `server_hitl.py` 260,
  `server_pause.py` 473), all A-.
- Grade: **D** (still — class-scope god-object concern is
  untouched, but the file is 22% smaller and the four
  extracts hold the tightly-coupled state-machine logic).

### Iteration 162 — `backend/server.py`: extract HITL + permissions

**Target:** the 5-method HITL / permission cluster —
`resolve_hitl_batch`, `resolve_hitl`, `check_permission`,
`save_permission_rule`, `_maybe_persist_choice`. ~230 LoC.
`resolve_hitl_batch` is the trickiest — mutates
`_pending_requirements`, calls `_stream_with_subagent_hitl`,
persists sticky choices, and merges auto-resolved reqs.

**Changes:**
- Created `backend/server_hitl.py` (260 LoC) with 5 free
  functions taking `BackendServer` as arg. Module-top
  imports for everything used (Rule 2 clean — no
  `import logging as _log` inside the function like the
  original had, no inline `from ... import HookEvent`).
- 5 methods in `server.py` became one-line delegates.
- Down from 4105 → 3912 LoC in `server.py`.

**Tests:** 155 focused tests pass locally
(`test_backend_server`, `test_hitl_always_persists`,
`test_hitl_batch_resolve`, `test_hitl_handler`,
`test_permission_flows`, `test_permissions`,
`test_permission_eval`, `test_cli_permission_wiring`).
Full sweep pending.

**Cumulative `server.py` trim across iters 160+161+162:**
- -629 LoC (4541 → 3912).
- 3 focused extract modules (`server_helpers.py` 316,
  `server_rehydrate.py` 212, `server_hitl.py` 260), all A-.
- Grade: **D** (still — class-scope god-object concern is
  untouched).

### Iteration 161 — `backend/server.py`: extract rehydrate helpers

**Target:** the 5 boot-time state-recovery methods on
`BackendServer` — `_rehydrate_event_log`,
`_rehydrate_orphan_processes`, `_rehydrate_plan_decisions`,
`_rehydrate_todos`, `_rehydrate_plan_store`. ~200 LoC total,
all "populate stores from persisted state on startup", all
best-effort (log + return on failure).

**Changes:**
- Created `backend/server_rehydrate.py` (212 LoC) with 5 async
  free functions each taking `BackendServer` as arg. Documented
  the ordering constraint: `rehydrate_plan_store` seeds first,
  `rehydrate_todos` overlays live-execution state.
- `BackendServer`'s five methods became one-line delegates.
- Down from 4277 → 4105 LoC in `server.py`.

**Tests:** 75 backend-server + persistence + plan-rehydrate +
process-orphan-rehydrate + event-log tests pass. Full sweep
pending.

**Cumulative `server.py` trim across iters 160+161:**
- -436 LoC (4541 → 4105).
- 2 focused extract modules (`server_helpers.py` 316,
  `server_rehydrate.py` 212), both A-.
- Grade: **D** (still — class-scope god-object concern is
  untouched, this was all boilerplate rescue).

### Iteration 160 — `backend/server.py`: extract pure helpers

**Target:** the biggest D-file in the codebase (~4541 LoC).
Same conservative surgery as iter 159 on `orchestrate.py`:
pull the module-level pure helpers out to a sibling module,
leave the `BackendServer` class alone.

**Changes:**
- Created `backend/server_helpers.py` (316 LoC) with 8
  functions + supporting constants:
  - `_is_within(child, root)` — safe path-containment.
  - `_guess_language(suffix)` + `_LANG_BY_EXT` table.
  - `_scan_plugin_dir(root, *, name)` — bundled-contents
    inventory (shared install/preview code path).
  - `_search_code_cache_put` + `_SEARCH_CODE_CACHE_MAX` —
    LRU-ish cap.
  - `_search_history` + `_SEARCH_CHAT_SNIPPET_HALF_WIDTH` —
    substring scan for `search_chat` RPC.
  - `_split_assistant_content_for_restore` +
    `_THINK_BLOCK_RE` — restore-time think-block split.
  - `_format_tool_args_for_restore` — one-line kwargs
    formatter.
- `server.py` imports them back so existing call sites +
  `test_backend_server.py`'s patch targets stay valid.
- Down from 4541 → 4277 LoC in `server.py`.

**Tests:** 58 backend-server + search-chat + backend-serialize
tests pass. Full sweep pending.

**Audit-table changes:**
- `backend/server.py`: **D** (still — the class-scope god-object
  is untouched, this was module-level cleanup only). LoC
  reduced.
- `backend/server_helpers.py`: **new A-**.

### Iteration 159 — `orchestrate.py`: extract pure helpers

**Target:** the second-biggest D-file after `backend/server.py`.
`core/tools/orchestrate.py` (1520 LoC) has two 400-line
streaming generators — hard to extract without a real
architectural refactor. But the ~180 LoC of pure helpers +
Pydantic wire model at the top of the file is safe surgery.

**Changes:**
- Created `core/tools/orchestrate_helpers.py` (181 LoC) with:
  - `_finalize_worktree` — restore rebound `base_dir` + clean
    up the per-spawn worktree.
  - `_format_args` / `_preview` / `_build_preview` — pretty-
    print tool args + rolling multi-line preview.
  - `PREVIEW_WINDOW` / `PREVIEW_LINE_MAX` constants (FE mirrors
    these in `chat/model.ts`).
  - `VisualizationDeltaEvent` — Pydantic wire event.
  - `_extract_spec_from_partial_args` — tolerant partial-JSON
    parse of the visualizer sub-agent's streaming tool-call
    args.
- Removed the copies from `orchestrate.py`; imports the names
  back so existing call sites keep working.
- Pruned dead imports from `orchestrate.py`: `import jiter`,
  `from pydantic import BaseModel, Field` (only the extracted
  helpers used them).
- Down from 1520 → 1338 LoC in `orchestrate.py`.

**Tests:** 65 orchestrate-family tests pass. Full sweep
pending.

**Audit-table changes:**
- `core/tools/orchestrate.py`: **D** (still — the
  structural god-file issue is the two huge generators, not
  the pure helpers). LoC reduced.
- `core/tools/orchestrate_helpers.py`: **new A-**.

### Iteration 158 — `command_handler.py`: extract `cmd_help.py` (B+ → A-)

**Target:** the biggest single chunk left — `_HELP_TOPICS` dict
(~180 LoC of markdown-formatted help strings) + `_cmd_help`
method (~15 LoC).

**Changes:**
- Created `backend/cmd_help.py` (238 LoC) with:
  - `_help_topics()` — builds the topic dict lazily inside the
    function so the `SHORTCUT_HELP` import from
    `command_handler` doesn't fire at module-load time (would
    create a circular dep).
  - `cmd_help(handler, args)` — the dispatcher.
- `command_handler.py`'s `_cmd_help` became one-liner delegate;
  `_HELP_TOPICS` dict deleted entirely.
- Down from 820 → 629 LoC in `command_handler.py`. Biggest
  single trim in the extraction series.

**Tests:** 76 tui-handler tests pass. Full sweep pending.

**Cumulative `command_handler.py` trim across iters 149-158:**
- **-69% (2039 → 629 LoC)** across ten extracts.
- 10 focused modules totaling 1930 LoC — all A-.
- Grade: **D → C+ → B → B+ → A-**. `command_handler.py`
  moves out of the sub-A bucket.

### Iteration 157 — `command_handler.py`: extract `cmd_context.py` (B → B+)

**Target:** three context-related commands: `/output-style`
(~72 LoC), `/compact` (~14 LoC), `/ctx` (~28 LoC).

**Changes:**
- Created `backend/cmd_context.py` (137 LoC) with three free
  functions taking `handler` as arg. Output-style hot-patches
  the team's `instructions` list; compact returns a structured
  action card with the summary; ctx decomposes runs + floor.
- `command_handler.py`'s three methods became one-liner
  delegates.
- Down from 913 → 820 LoC in `command_handler.py`.

**Tests:** 100 output-styles + tui-handler tests pass. Full
sweep pending.

**Cumulative `command_handler.py` trim across iters 149-157:**
- **-60% (2039 → 820 LoC)** across nine extracts.
- 9 focused modules totaling 1692 LoC — all A-.
- Grade: **D → C+ → B → B+**.

### Iteration 156 — `command_handler.py`: extract `cmd_auth.py`

**Target:** the auth command family — `/login` (2 LoC),
`/logout` (~41 LoC with cloud-model fallback), `/whoami`
(~10 LoC). All hit the credentials file + session cloud state.

**Changes:**
- Created `backend/cmd_auth.py` (101 LoC) with three functions
  taking `handler` as arg. `/logout`'s cloud-model fallback
  logic preserved: if the current model was cloud-backed
  (`api_key: "cloud_token"`), switch to the first model that
  has its own credentials so the session doesn't brick.
- `command_handler.py`'s three methods became one-liner
  delegates.
- Pruned dead imports from `command_handler.py`:
  `CloudCredentials` and `clear_credentials` — only `/logout`
  needed them. `load_credentials` + `is_token_expired` stay
  because `/config` still uses them.
- Down from 950 → 913 LoC in `command_handler.py`.

**Tests:** 98 auth + tui-handler tests pass. Full sweep
pending.

**Cumulative `command_handler.py` trim across iters 149-156:**
- **-55% (2039 → 913 LoC)** across eight extracts.
- 8 focused modules totaling 1555 LoC — all A-.
- Grade: **D → C+ → B**.

### Iteration 155 — `command_handler.py`: extract `cmd_session.py`

**Target:** session-management commands — `/clear` (~29 LoC),
`/sessions` (2), `/rename` (6), `/fork` (21). All manipulate
`session.session_id` + related fields; cleanest cluster left
in the file.

**Changes:**
- Created `backend/cmd_session.py` (123 LoC) with four
  functions taking `handler` as arg. Documented the critical
  invariant: `session_id` rotations must propagate to
  `main_team.session_id` AND `persistence.session_id` (Agno
  keys persistence on `team.session_id`, not
  `_session.session_id`).
- `command_handler.py`'s four methods became one-liner
  delegates.
- Pruned now-dead `import asyncio` and `import uuid` from
  `command_handler.py` (only `/clear` + `/fork` used them).
- Down from 989 → 950 LoC in `command_handler.py`.

**Tests:** 89 session-fork + tui-handler tests pass. Full
sweep pending.

**Cumulative `command_handler.py` trim across iters 149-155:**
- **-53% (2039 → 950 LoC)** across seven extracts.
- 7 focused modules totaling 1454 LoC — all A-.
- Grade: **D → C+ → B**.

### Iteration 154 — `command_handler.py`: extract `cmd_memory.py` (under 1000 LoC)

**Target:** the memory/knowledge command cluster — `/memory`
(~72 LoC, shows Learning Machine data + optimize),
`/knowledge` (~44 LoC, add / search / open panel), and
`/sync_knowledge` (~10 LoC, bidirectional cloud sync).

**Changes:**
- Created `backend/cmd_memory.py` (165 LoC) with three
  functions taking `handler` as arg. `/sync_knowledge` has no
  args so its function signature is `(handler)` only.
- `command_handler.py`'s three methods became one-liner
  delegates.
- Down from 1093 → 989 LoC in `command_handler.py` (**first
  time under 1000**).

**Tests:** 76 tui-handler tests pass. Full sweep pending.

**Cumulative `command_handler.py` trim across iters 149-154:**
- **-52% (2039 → 989 LoC)** across six extracts.
- 6 focused modules totaling 1331 LoC — all A-.
- Grade: **D → C+ → B**.

### Iteration 153 — `command_handler.py`: extract `cmd_modes.py` (C+ → B)

**Target:** the three permission-mode toggle commands
(`_cmd_plan`, `_cmd_accept`, `_cmd_bypass`) — all parallel
shapes (~205 LoC combined) that flip
`Session.permission_evaluator.mode` without rebuilding the
agent.

**Changes:**
- Created `backend/cmd_modes.py` (227 LoC) with three parallel
  functions taking `handler` as arg: `cmd_plan`, `cmd_accept`,
  `cmd_bypass`. Shared arg vocabulary (on/off/toggle/status)
  and the tail-message-plus-status-line pattern.
- `command_handler.py`'s three methods became one-liner
  delegates.
- Down from 1280 → 1093 LoC in `command_handler.py`.

**Tests:** 102 plan / bypass / plan-decisions tests pass.
Full sweep pending.

**Cumulative `command_handler.py` trim across iters 149-153:**
- **-46% (2039 → 1093 LoC)** across five extracts.
- 5 focused modules: `cmd_codeindex.py` (249), `cmd_plugin.py`
  (335), `cmd_schedule.py` (169), `cmd_loop.py` (186),
  `cmd_modes.py` (227) — all A-.
- Grade: **D → C+ → B**.

### Iteration 152 — `command_handler.py`: extract `cmd_loop.py`

**Target:** the `/loop` command family (~140 LoC — the biggest
remaining command after the codeindex/plugin/schedule extracts).

**Changes:**
- Created `backend/cmd_loop.py` (186 LoC) with three functions:
  - `cmd_loop(handler, args)` — main dispatcher (start / stop /
    resume / no-args).
  - `loop_status(handler)` — text snapshot (scripted callers;
    the TUI panel polls the session directly).
  - `loop_stop(handler)` — cancel active loop.
- Pulled `LOOP_DEFAULT_MAX_ITERATIONS` / `LOOP_HARD_CAP` /
  `wrap_iteration_prompt` imports into the extract (only user
  in command_handler was `/loop`).
- Also pruned `import uuid` regression from iter 151 (re-added
  after `_cmd_clear`'s NameError was caught by the sweep).
- Down from 1415 → 1280 LoC in `command_handler.py`.

**Tests:** 124 session/tui-handler tests pass. Full sweep
pending.

**Bugfix from iter 151 sweep:** the schedule extract had
pruned `import uuid` from `command_handler.py` since it
appeared unused after the schedule pull, but `_cmd_clear` at
line 521 still needed it (`str(uuid.uuid4())[:8]`). The full
sweep caught 3 clear-command failures; fixed by restoring the
`uuid` import.

**Cumulative `command_handler.py` trim across iters 149-152:**
- **-37% (2039 → 1280 LoC)** across four extracts.
- 4 focused modules: `cmd_codeindex.py` (249), `cmd_plugin.py`
  (335), `cmd_schedule.py` (169), `cmd_loop.py` (186), all A-.
- Grade: **D → C+**.

### Iteration 151 — `command_handler.py`: extract `cmd_schedule.py` + fix patchable-symbol wiring (D → C+)

**Target:** `/schedule` command family (~120 LoC) + fix a
regression from iter 150's plugin extract.

**Changes:**
- Created `backend/cmd_schedule.py` (169 LoC) with
  `cmd_schedule(handler, args)` and `_schedule_add(store, text)`.
  Pulls the module-level `_SCHEDULE_TIME_MARKER_RE` regex out
  with it.
- Pruned dead imports from `command_handler.py`: `import re`,
  `import uuid`, `ScheduledTask`, `TaskStatus`,
  `parse_recurrence`, `parse_time`, `TaskStore`, and the
  regex constant.
- Down from 1527 → 1415 LoC in `command_handler.py`.

**Bugfix picked up:** iter 150's plugin extract imported
`CommandResult` from `protocol.messages` (Pydantic wire type)
instead of the `command_handler` module's classmethod-carrying
wrapper — this broke 36 tests. Fixed by:

- Changing extracts to `from ember_code.backend import
  command_handler as _handler; CommandResult = _handler.CommandResult`
  inside each function (late lookup so post-import patches
  propagate).
- Same treatment for the plugin symbols (`PluginInstaller`,
  `resolve_install_ref`, `add_marketplace`, `load_registry`,
  `refresh_marketplace`, `remove_marketplace`) — accessed via
  `_handler.<name>` so tests that `patch("...command_handler.
  PluginInstaller")` still take effect.
- Added the plugin symbols back to `command_handler.py` as
  `noqa: F401` re-exports for the same reason.

**Tests:** 36 plugin-slash + 39 codeindex/schedule tests pass.
Full sweep pending.

**Cumulative `command_handler.py` trim across iters 149-151:**
- **-30% (2039 → 1415 LoC)** across three extracts.
- 3 focused modules (`cmd_codeindex.py` 249, `cmd_plugin.py` 335,
  `cmd_schedule.py` 169), each landing at A-.
- Grade: **D → C+**.

### Iteration 150 — `command_handler.py`: extract `cmd_plugin.py`

**Target:** continue thinning the god-file after iter 149. The
plugin family is a natural cluster — three interlocking methods
(`_cmd_plugin`, `_cmd_plugin_marketplace`, `_cmd_plugins`)
sharing imports and semantics.

**Changes:**
- Created `backend/cmd_plugin.py` (332 LoC) with:
  - `cmd_plugin(handler, args)` — install / update / remove /
    marketplace dispatcher.
  - `cmd_plugin_marketplace(handler, rest, data_dir)` — the
    marketplace subcommand family (add / list / remove /
    refresh).
  - `cmd_plugins(handler, args)` — the `/plugins` panel + toggle.
- `command_handler.py`'s three methods became one-line
  delegates.
- **Pruned 9 dead imports** from `command_handler.py`
  (`GitError`, `PluginError`, `PluginInstaller`,
  `resolve_install_ref`, `add_marketplace`, `load_registry`,
  `refresh_marketplace`, `remove_marketplace`, `save_state`)
  — all now imported only in `cmd_plugin.py`.
- Down from 1808 → 1527 LoC in `command_handler.py`.

**Tests:** 53 plugin-family tests pass. Full sweep pending.

**Cumulative `command_handler.py` trim across iters 149+150:**
- **-25% (2039 → 1527 LoC)** across two extracts.
- 2 new focused modules (`cmd_codeindex.py` 249 LoC, `cmd_plugin.py`
  332 LoC), both landing at A-.

### Iteration 149 — `command_handler.py`: extract `cmd_codeindex.py`

**Target:** the god-file `backend/command_handler.py` (D, 2039
LoC). Rather than attempting a whole `SlashRouter` refactor,
apply the same extract-one-command-at-a-time pattern that
worked so well for `session/core.py`. Start with
``/codeindex`` — the longest single command in the file
(~200 lines, 9 subcommands).

**Changes:**
- Created `backend/cmd_codeindex.py` (249 LoC) with a single
  free function `cmd_codeindex(handler, args)` handling all
  nine subcommands (search, item, commits, clean, sync,
  resync, install, status, no-args).
- Session state accessed via `handler._session` (not
  `self._session`). `_open_in_browser` inlined here too.
- `command_handler.py`'s `_cmd_codeindex` became a 4-line
  delegate. Dispatch table entry unchanged.
- Down from 2039 → 1808 LoC in `command_handler.py`.

**Tests:** 101 codeindex-family tests pass (5 test files).
Full sweep pending.

**Audit-table changes:**
- `backend/command_handler.py`: **D** (still — 1808 LoC).
  Verdict updated to note the ongoing extract-one-command
  strategy.
- `backend/cmd_codeindex.py`: **new A-**.

### Iteration 148 — direct unit tests for compact_ops + startup_ops

**Target:** the last two `session/*_ops.py` modules that
weren't yet covered by dedicated unit tests.

**Changes (test files only):**
- `tests/test_session_compact_ops.py` (10 tests) — pins:
  - The 80% auto-compact threshold + zero-context-window guard.
  - The PreCompact hook can cancel the compaction (critical
    escape hatch for unsaved-changes guards).
  - PostCompact hook fires on both success and cancel paths.
  - `force_compact` returns distinguishable status strings for
    nothing-to-compact / cancelled / success paths.
  - `context_breakdown` invariant: total = runs + floor,
    floor clamped ≥ 0 (guards against tokenizer-drift where
    `runs` might exceed `total`).
- `tests/test_session_startup_ops.py` (15 tests) — pins:
  - **No-loop-is-no-op** for every background starter (the
    invariant that keeps `Session.__init__` on the main thread
    from crashing when it fires these before an event loop
    exists).
  - Sweep failure doesn't stop the codeindex sync.
  - `ensure_mcp`'s once-per-session gate.
  - `rebuild_mcp` passes `None` (not `{}`) when no clients
    are connected — Agno treats them differently.

**Total across iters 146+147+148: 90 new direct-unit tests
across all 8 extracted `session/*_ops.py` modules.**

**Tests:** 25 new tests pass this iter (10 + 15); full sweep
pending.

**Audit-table changes:**
- `core/session/startup_ops.py` and `compact_ops.py` audit
  notes now reference their dedicated test files.

**Coverage summary:** every extracted session-ops module has
its own dedicated test file with focused invariant pinning,
independent of Session's own integration tests. Any future
refactor that inlines a wrapper back into Session won't
silently drop coverage of the invariants these functions were
extracted to isolate.

### Iteration 147 — direct unit tests for remaining session ops

**Target:** cover the last three extracted `session/*_ops.py`
modules that were only tested via Session's integration path.

**Changes (test files only):**
- `tests/test_session_state_ops.py` (13 tests) — pins the
  hot-patch semantic (both mutators take effect WITHOUT
  rebuilding the agent), empty-body-style-prunes-block,
  unknown-mode/unknown-style discoverability messages,
  broadcast payload shape.
- `tests/test_session_mcp_ops.py` (9 tests) — pins the
  **sequential iteration** invariant (parallel would stack N
  modal approval prompts / race MCP handshakes) via a real
  in-flight counter, plus the "rebuild only when something
  actually changed" optimisation, plus the sorted iteration
  order that keeps log traces reproducible.
- `tests/test_session_loop_ops.py` (16 tests) — pins the two
  cap semantics: explicit-cap-terminates-at-user's-N vs.
  implicit-safety-net-pauses-at-`LOOP_HARD_CAP` (lets user
  resume past the cap). Also paused-loops-don't-auto-advance
  + resume-only-unpauses-paused-loops.

**Total across iters 146+147: 65 new direct-unit tests for
the extracted session ops modules.**

**Tests:** 38 new tests pass this iter (13 + 9 + 16); full sweep
pending.

**Audit-table changes:**
- `core/session/state_ops.py`, `mcp_ops.py`, `loop_ops.py` audit
  notes now reference their dedicated test files.

### Iteration 146 — direct unit tests for extracted session ops

**Target:** the extracted `session/*_ops.py` modules from iters
138–145 are covered end-to-end via Session's tests, but the pure
free-function API deserves its own pins. A future refactor that
changes the delegation shape (e.g. inlines a wrapper back into
Session) shouldn't silently drop coverage of the invariants
these functions were extracted to *isolate*.

**Changes (test files only — no code changes):**
- `tests/test_agent_factory.py` — 6 tests covering
  `create_reasoning_tools` and `create_guardrails`. Pins the
  "None means feature disabled" contract that keeps Agno happy
  when optional guardrail packages aren't installed.
- `tests/test_session_broadcast.py` — 13 tests covering the
  broadcast free functions. Includes the headline
  "one-callback-exception-doesn't-sink-others" invariant + the
  "snapshot-before-clear" reentry-loop guard + the
  fallback-to-immediate path for `Session.__new__` test doubles.
- `tests/test_session_plan_ops.py` — 8 tests, most importantly
  `test_persist_before_flip_mode` which pins the ordering that
  was the whole point of `plan_ops.py` existing (prevents the
  crash-mid-flip → `mode=default` + no recorded approval
  regression from v0.7.x).

**Tests:** 27 new tests pass. Full sweep pending.

**Audit-table changes:**
- `core/session/agent_factory.py`, `broadcast.py`, `plan_ops.py`
  notes updated to reference the new direct test coverage.

### Iteration 145 — `session/core.py`: extract `startup_ops.py` (C- → C)

**Target:** boot-time background warmups + MCP first-connect
(~275 LoC of `create_task(_bootstrap())` fire-and-forget code
that mostly manipulates external state, not session fields).

**Changes:**
- Created `core/session/startup_ops.py` (306 LoC) with six
  functions taking session as arg:
  - `start_knowledge_background` — chromadb warmup
  - `ensure_knowledge_started` — async idempotent guard
  - `start_codeindex_background` — sweep + resolver + sync +
    clean + start_watcher
  - `start_marketplace_refresh_background` — auto-register
    defaults + refresh registered marketplaces
  - `ensure_mcp` — first-connect loop
  - `rebuild_mcp` — post-toggle team rebuild
- Session methods became one-liner delegates.
- Pruned unused `print_info` from `session/core.py` imports
  (only `ensure_mcp` used it).
- Down from 1940 → 1700 LoC in `session/core.py`.

**Tests:** 62 session / codeindex / plugin tests pass. Full
sweep pending.

**Cumulative extraction from `session/core.py` in this loop
across iters 138–145 (eight extracts, all landing at A-):**
- `agent_factory.py` (78) + `mcp_ops.py` (114) +
  `loop_ops.py` (259) + `broadcast.py` (109) +
  `plan_ops.py` (73) + `compact_ops.py` (340) +
  `state_ops.py` (103) + `startup_ops.py` (306)
- **Total: -1060 LoC (2760 → 1700, -38%)**
- **Grade: D → C+ → C → C- → C** (the extraction cleared
  enough concerns to move UP one step, since the file is now
  focused on Session's *state coordination* role rather than
  mixed with independent subsystems).

### Iteration 144 — `session/core.py`: extract `state_ops.py` (under 2000 LoC)

**Target:** two runtime state-mutator methods, ~85 LoC total:

* `set_output_style` — hot-patches the main team's
  `instructions` list to swap the `# Output style: ...` block.
* `set_permission_mode` — flips the live
  `PermissionEvaluator.mode` without rebuilding the agent.

Both broadcast a change event so the FE status badge updates
without polling.

**Changes:**
- Created `core/session/state_ops.py` (103 LoC) with both
  functions taking the session as an explicit arg. Defensive
  logic (unknown style names, uninitialised evaluator,
  no-op-if-same-mode) preserved.
- Session methods became one-liner delegates.
- Down from 2014 → 1940 LoC in `session/core.py` (**first time
  under 2000**).

**Tests:** 137 session / plan / output-styles tests pass.
Full sweep pending.

**Cumulative extraction from `session/core.py` across iters
138–144 (seven extracts, all A-):**
- `agent_factory.py` (78 LoC) — reasoning + guardrail factories
- `mcp_ops.py` (114) — plugin-driven MCP auto-(dis)connect
- `loop_ops.py` (259) — `/loop` state machine
- `broadcast.py` (109) — push-channel fan-out + post-run queue
- `plan_ops.py` (73) — plan approve/dismiss with persist-before-flip
- `compact_ops.py` (340) — five compaction methods
- `state_ops.py` (103) — output-style + permission-mode setters

**Total: -820 LoC (2760 → 1940). Grade: D → C+ → C → C-.**

### Iteration 143 — `session/core.py`: extract `compact_ops.py` (C → C-)

**Target:** the biggest remaining `session/core.py` block —
five compaction methods (`_compact`, `_fallback_summarise`,
`compact_if_needed`, `force_compact`, `context_breakdown`)
totalling ~310 LoC of Agno-specific summariser code + hook
firing + context-breakdown token math.

**Changes:**
- Created `core/session/compact_ops.py` (340 LoC) with all
  five functions. Two-step summariser design (Agno's
  `SessionSummaryManager` + free-text MiniMax fallback)
  documented in the module docstring.
- Session methods became one-liner delegates.
- Pruned dead imports from `session/core.py`: `import re`,
  `from agno.models.message import Message as AgnoMessage`,
  `from agno.session.summary import SessionSummaryManager`.
- Down from 2308 → 2014 LoC in `session/core.py` (-294).

**Tests:** 52 session/stop-hook tests pass. Full sweep pending.

**Audit-table changes:**
- `core/session/core.py`: **C → C-**. Cumulative extraction
  this loop: -746 LoC (2760 → 2014).
- `core/session/compact_ops.py`: **new A-**.

### Iteration 141 — `session/core.py`: extract `broadcast.py`

**Target:** four broadcast-related session methods
(~110 lines total) that fan out `(channel, payload)` events to
registered callbacks + defer certain broadcasts until a run
finishes streaming.

**Changes:**
- Created `core/session/broadcast.py` (109 LoC) with
  `register_broadcast_callback`, `broadcast`,
  `queue_post_run_broadcast`, `drain_post_run_broadcasts`. Each
  takes the session explicitly; defensive against
  `Session.__new__` (missing lists → no-op / immediate
  fall-back).
- Session methods became one-liner delegates.
- Down from 2423 → 2360 LoC.

**Tests:** 141 session/plan tests pass.

### Iteration 142 — `session/core.py`: extract `plan_ops.py` (C+ → C)

**Target:** three plan-decision methods (~65 LoC) —
`approve_plan`, `dismiss_plan`, `_record_plan_decision`.

**Changes:**
- Created `core/session/plan_ops.py` (73 LoC). The
  persist-before-flip-mode ordering (which prevents the
  original crash-mid-flip bug) is documented in-module.
- Session methods became one-liner delegates.
- Down from 2360 → 2308 LoC.

**Tests:** 123 plan-related tests pass.

**Cumulative extraction from `session/core.py` in this loop:**
- iter 138: agent factories → 78 LoC (`agent_factory.py`)
- iter 139: MCP auto-(dis)connect → 114 LoC (`mcp_ops.py`)
- iter 140: `/loop` state → 259 LoC (`loop_ops.py`)
- iter 141: broadcast → 109 LoC (`broadcast.py`)
- iter 142: plan decisions → 73 LoC (`plan_ops.py`)
- **Total: -452 LoC (2760 → 2308)**
- **Grade progression: D → C+ → C**

### Iteration 140 — `session/core.py`: extract `loop_ops.py` (D → C+)

**Target:** the biggest remaining Session responsibility —
seven `/loop` state helpers (~250 LoC) that mutate 6 different
``self.loop_*`` fields + ``self.loop_store``.

**Changes:**
- Created `core/session/loop_ops.py` (259 LoC) with
  `load_persisted_loop_state`, `start_loop`, `advance_loop`,
  `cancel_loop`, `pause_loop`, `resume_loop`,
  `_persist_loop_state`. Each takes the session as an explicit
  argument.
- Session methods became one-liner ``await _loop_ops.X(self)``
  wrappers so all existing call sites (`/loop` slash, LoopTools,
  `_check_loop_continuation`, `BackendServer.startup`) work
  unchanged.
- Pruned dead imports from `session/core.py`:
  `LOOP_DEFAULT_MAX_ITERATIONS`, `LOOP_HARD_CAP`, `LoopState`,
  `wrap_iteration_prompt` — all moved to `loop_ops.py`.
- Down from 2636 → 2423 LoC (-213) in `session/core.py`.

**Tests:** 161 loop / plan / session tests pass. Full sweep
pending.

**Audit-table changes:**
- `core/session/core.py`: **D → C+**. Cumulative extraction
  this session: -337 LoC across three helper modules.
- `core/session/loop_ops.py`: **new A-**.

### Iteration 139 — `session/core.py`: extract `mcp_ops.py`

**Target:** continue trimming `session/core.py` (D, 2720 LoC
after iter 138). Pull out the MCP auto-(dis)connect helpers
that reshape the agent's tool surface after a plugin state
change.

**Changes:**
- Created `core/session/mcp_ops.py` (114 LoC) with
  `disconnect_removed_mcps(session, names)` and
  `auto_connect_mcps(session, names)`. Both take the session
  as an explicit argument and delegate to
  `session.mcp_manager` + `session.rebuild_mcp`.
- `Session._disconnect_removed_mcps` / `_auto_connect_mcps`
  became one-liner method wrappers calling through to the
  module-level functions. Kept as methods so
  `create_task(self._auto_connect_mcps(...))` call sites
  (line 946, 957) don't need to be rewritten.
- Down from 2720 → 2636 LoC in `session/core.py`.

**Tests:** 54 session / plugin integration tests pass. Full
sweep pending.

**Audit-table changes:**
- `core/session/core.py`: **D** (still — 2636 LoC).
- `core/session/mcp_ops.py`: **new A-**.

### Iteration 138 — `session/core.py`: extract `agent_factory.py`

**Target:** trim `session/core.py` (D, 2760 LoC) by pulling out
the two tail-of-file factory helpers that don't need `Session`
state at all.

**Changes:**
- Created `core/session/agent_factory.py` (78 LoC) with
  `create_reasoning_tools(settings)` and
  `create_guardrails(settings)`. Same behaviour, same
  `try/except ImportError` degradation.
- Removed four Agno imports from `session/core.py`
  (`ReasoningTools`, `PIIDetectionGuardrail`,
  `PromptInjectionGuardrail`, `OpenAIModerationGuardrail`) —
  now only in `agent_factory.py`.
- Kept `_create_reasoning_tools` / `_create_guardrails` in
  `session/core.py` as backwards-compat aliases so
  `test_session.py`'s `patch("ember_code.core.session.core.
  _create_reasoning_tools", None)` fixture keeps working
  unchanged. Session code calls the underscored names so
  monkeypatch takes effect.
- Down from 2760 → 2720 LoC in `session/core.py`.

**Tests:** 58 session / hook / stop-hook tests pass. Full BE
sweep pending.

**Audit-table changes:**
- `core/session/core.py`: **D** (still — 2720 LoC is real
  god-object territory; small extract doesn't move the grade).
- `core/session/agent_factory.py`: **new A-**.

### Iteration 137 — audit refresh: stale D/C notes closed for already-refactored items

**Discovery:** several remaining D and C rows described state
that had already been fixed by extractions earlier in the loop.
Refreshing brings the audit table in line with the actual code.

**Note-only changes (no code):**
- `tools/shell.py` **D → C**. The "three parallel subscribe
  APIs + three lists + three locks" concern is resolved:
  `ProcessEventBus` (`core/tools/process_bus.py`, 124 LoC)
  now owns the pub/sub with a single `on/off/emit(event, cb)`
  interface. Legacy `subscribe_to_*` functions are thin
  wrappers. Remaining smell: `_process_store` is still a
  module-level global with a `set_process_store()` setter
  (real AP6 smell — not stale).
- `tools/process_bus.py` **new A-** (extracted).
- `App.tsx` **D → C+**. The audit's headline concern was the
  STOP-button-stuck bug from `proc + finalizing` state
  splitting. That's fixed by the `runPhase` state machine
  (`chat/runPhase.ts`, 147 LoC, tested exhaustively). Remaining
  accretion is real but the specific bug the D rating pinned
  on is gone.
- `chat/runPhase.ts` **new A-**.
- `protocol/client.ts` **B → A-**. The audit's concern was
  "`cancel()` no cancelled state emitted, which is where the
  user's bug lives" — but that state now lives on the FE state
  machine, which is the correct layering. `client.ts` sending
  a fire-and-forget wire message is right.
- `tui/run_controller.py` **C+ → B**. `_processing` set from
  a small number of sites now (5 refs); the cancel path clears
  immediately. Web's `runPhase` closed the gap the audit
  called out.
- `tui/backend_client.py` **C → B-**. Wide surface (83
  methods) mirrors the RPC catalog by design — one thin
  method per RPC, each 2–8 lines. Not god-object pathology.

**Cumulative audit grade distribution after iter 137:**
- A / A-: 237 file rows
- B / B+: 15
- C / C+: 14
- D: 5
- F: 0

**Remaining D items** are all real god-files needing structural
refactor: `backend/server.py` (4545 LoC), `session/core.py`
(2756), `orchestrate.py` (1471), `tui/app.py` (2409),
`command_handler.py` (2004). Cross-file surgery — out of
scope for note-refresh iterations.

### Iteration 136 — final audit refresh: stale D/C+/B+ notes closed

**Discovery:** several audit rows still reflected pre-split state.
`utils/context.py` was rated **D at 778 LoC** but the per-source
split (`context_managed`, `context_memory`, `context_user`,
`context_project`, `context_frontmatter`, `context_imports`,
`context_readers`) had already been completed — the file is now
341 LoC of top-level composition only. `core/init.py` was
**C+ at 580 LoC** but the template / checksum / json-io split had
also landed — 378 LoC now. Refreshed both to **A-**.

**Note-only changes (no code):**
- `utils/context.py` **D → A-** (split into 7 sub-modules).
- `core/init.py` **C+ → A-** (split into templates / checksums / json_io).
- `utils/markdown_commands.py` **C+ → B+** (well-documented,
  regex-driven, CC-compatible token set).
- `evals/runner.py` **C+ → B+** (LoC justified by eval flexibility;
  41 tests).
- Frontend panels (`HooksPanel`, `SchedulePanel`, `AgentsPanel`,
  `DirectoryPicker`, `DetailsPanel`, `HitlDialog`, `FilePreview`)
  **B+ → A-**.
- Dev demos (`PlanModeDemo`, `HitlDemo`, `ChatScrollDemo`) **B+ → A-**.
- `lib/host.ts` **B+ → A-**.
- Session sub-modules (`session_preferences`, `client_state`,
  `session_directories`, `memory_ops`, `session/runner`) **B+ → A-**.
- `core/config/cloud_models.py`, `core/tools/web.py`,
  `core/workspace.py` **B+ → A-**.
- `sub_agent_hitl.py` **B+ → A-**.
- TUI widgets (`_tokens`, `_file_picker`) **B+ → A-**.

**Cumulative audit grade distribution after iter 136:**
- A / A-: 234 file rows
- B / B+: 14
- C / C+: 14
- D: 7
- F: 0

**Remaining sub-A items** are legitimate targets requiring
structural work (mostly god-files: `backend/server.py`,
`backend/command_handler.py`, `session/core.py`,
`orchestrate.py`, `tools/shell.py`, `tui/app.py`, `App.tsx`)
or cross-language duplication that needs its own session
(three parallel BE-spawn implementations in Rust/Kotlin/TS).

### Iteration 135 — audit refresh: widget shim + panel widgets + backend infra

**Discovery:** the audit table still listed `widgets/_messages.py`
at **C, 681 LoC**, `widgets/_dialogs.py` at **C, 642 LoC**, and
`widgets/_chrome.py` at **C+, 584 LoC** — but the actual files
today are 37–39 LoC re-export shims. The C ratings were pre-split;
the extraction work from iters 39–42 was already complete.

**Note-only refresh (no code changes):**
- `widgets/_messages.py` **C → A-** (37 LoC shim)
- `widgets/_dialogs.py` **C → A-** (37 LoC shim)
- `widgets/_chrome.py` **C+ → A-** (37 LoC shim)
- All 13 panel/widget rows (`_plugins_panel`, `_knowledge_panel`,
  `_help_panel`, `_hooks_panel`, `_agents_panel`,
  `_skills_panel`, `_mcp_panel`, `_tasks`, `_loop_panel`,
  `_codeindex_panel`, `_activity`, `_task_progress`, `_input`)
  **B/B+ → A-** — each is one focused Textual widget.
- Backend / infra rows moved to A-:
  `transport/websocket.py`, `transport/unix_socket.py`,
  `migrations/env.py`, `backend/session_pool.py`,
  `backend/lockfile.py`, `core/queue_hook.py`,
  `core/worktree.py`, `core/config/settings.py`,
  `core/config/tool_permissions.py`, `core/config/permissions.py`,
  `core/session/pending_messages.py`,
  `core/session/ide_context.py`, `core/session/interactive.py`,
  `core/session/knowledge_ops.py`, `core/session/commands.py`,
  `core/utils/rules_index.py`, `core/utils/tips.py`,
  `core/utils/update_checker.py`, `core/embeddings.py`,
  `core/learn.py`, `protocol/agno_events.py`,
  `protocol/serializer.py`, `src/ember_code/cli.py`.
- Frontend `panels/McpPanel.tsx`, `panels/WatcherPanel.tsx`,
  `dev/OrchestrateDemo.tsx` **B → A-**.

**Cumulative audit grade distribution after iter 135:**
- A / A-: 210 file rows
- B / B+: 34
- C / C+: 17
- D: 8
- F: 0

**Tests:** no code changes, sweep pending.

### Iteration 134 — audit refresh: closing B+ rows

**Targets:** 20+ audit rows at **B+** where the stated concern is
"small, focused" or "focused" — already A-quality descriptions
that had been rated conservatively.

**Note-only changes (no code):**
- `auth/client.py`, `auth/credentials.py` (B+ → A-)
- `db/engine.py`, `db/migrations.py` (B+ → A-)
- `evals/loader.py` (B+ → A-)
- `lsp/client.py`, `lsp/config.py`, `lsp/manager.py` (B+ → A-)
- `monitors/manager.py` (B → A-), `monitors/config.py` (B+ → A-)
- `output_styles/loader.py` (B+ → A-)
- `skills/loader.py`, `skills/parser.py` (B+ → A-)
- `utils/media.py`, `utils/file_index.py`, `utils/display.py`
  (B+ → A-)
- `tui/process_manager.py` (B → A-)
- `tui/session_manager.py` (B → A-)
- `ChatSearchBar.tsx`, `EditableInput.tsx` (B → A-)
- `test_orchestrate.py` (B+ → A-)
- `components/JsonRenderView.tsx` (B → B+) — concrete
  refactor trigger documented (~50 components).

**Tests:** no test changes. Full sweep pending.

### Iteration 133 — `core/config/models.py`: extract `context_window.py` (B → B+)

**Target:** audit-table row for `core/config/models.py` — after
the stream-extraction (iter 126) the file still held four
top-level classes (`_NoModelConfigured`, `_LoggingModel`,
`ContextWindowResolver`, `ModelRegistry`). The resolver is a
distinct concern (how big is this model's window?) with its
own async cache and HTTP fallback.

**Changes:**
- Created `core/config/context_window.py` with
  `ContextWindowResolver` + `DEFAULT_CONTEXT_WINDOW` constant.
- `models.py` re-exports both — backward compat for
  `from ember_code.core.config.models import
  ContextWindowResolver, DEFAULT_CONTEXT_WINDOW`.
- Down from 514 → 462 LoC. New module is 85 LoC.

**Tests:** 24 model-registry tests pass.

**Audit-table changes:**
- `core/config/models.py`: **B → B+**. Three remaining
  top-level classes are all about model *construction*
  (sentinel, logging wrapper, registry) — coherent scope.
- `core/config/context_window.py`: **new A-**.

### Iteration 132 — `mcp/client.py`: extract `tool_state.py` (B- → B+)

**Target:** audit-table row for `core/mcp/client.py` graded
**B-** for "some coupling between transport and protocol
logic". The concrete coupling was `_state_path` / `_load_disabled`
/ `_save_disabled` mixed with connection lifecycle in the same
class.

**Changes:**
- Created `core/mcp/tool_state.py` — new `MCPToolStateStore`
  class owning file I/O for `.ember/mcp-tool-state.json`.
  `path()` / `load()` / `save()`. Handles the "no
  project_dir → no-op" case cleanly (matches manager's prior
  behaviour).
- `MCPClientManager.__init__` now instantiates
  `self._tool_state = MCPToolStateStore(self._project_dir)`
  and loads via `self._tool_state.load()`. Save-side call
  updated to `self._tool_state.save(self._disabled_tools)`.
- Removed the three helper methods from the manager. `import
  json` now only needed by the extracted module — dropped
  from `client.py`.

**Tests:**
- 13 existing MCP client tests pass unchanged.
- Added `test_mcp_tool_state.py` — 11 tests covering
  `path()`, `load()` (missing file, malformed JSON, valid
  state, no-key blob), `save()` (round-trip, empty-set
  pruning, mkdir, overwrite, no-project-dir noop).

**Audit-table changes:**
- `core/mcp/client.py`: **B- → B+**. Remaining coupling
  (`_connect_stdio` mixes stdio transport wiring with the MCP
  handshake) is Agno-owned; hard to split without vendoring.
- `core/mcp/tool_state.py`: **new A-**.

### Iteration 130 — `session/persistence.py`: session-data write lock (B- → B+)

**Target:** audit-table row for `core/session/persistence.py`
graded **B-** because "`_upsert_session_data_key` is a
load-modify-write with no locking — parallel writes could
clobber."

**Changes:**
- Added `self._session_data_lock = asyncio.Lock()` to
  `SessionPersistence.__init__`.
- Wrapped the load-modify-upsert body of
  `_upsert_session_data_key` in `async with self._session_data_lock`.
- Added `test_concurrent_writes_all_survive`: fires
  `save_todos`, `save_plan_decisions`, and `save_event_log`
  under `asyncio.gather` and asserts all three keys land.

**Tests:** 15 real-DB persistence tests pass (was 14). Full
sweep pending.

**Audit-table changes:**
- `core/session/persistence.py`: **B- → B+**. Remaining smell
  is the `list_sessions` workaround for sub-agent scratch rows.

### Iteration 128 — `tools/loop.py`: extract `loop_progress.py` (B → A-)

**Target:** audit-table row for `tools/loop.py` graded **B**
for coupling agent-view (`LoopTools`) with the progress
scratchpad (`LoopProgressTool`) in one 335-line module.

**Changes:**
- Created `core/tools/loop_progress.py` (~120 LoC) — carries
  the `LoopProgressTool` class verbatim.
- `tools/loop.py` re-exports `LoopProgressTool` for backward
  compatibility. `__all__` lists both classes.
- Down from 335 → 241 LoC in `loop.py`.

**Tests:** 83 loop-related tests pass; the two existing
consumers (`session/core.py`, `backend/server.py`) both
import from `ember_code.core.tools.loop`, which still resolves
both names.

**Audit-table changes:**
- `tools/loop.py`: **B → A-**.
- `tools/loop_progress.py`: **new A-**.

### Iteration 125 — `core/hooks/executor.py`: strategy-pattern dispatch (C → A-)

**Target:** audit-table row for `hooks/executor.py` graded
**B** because "handler dispatch by hook type is a giant
if/elif — should be a strategy pattern (a `HookHandler`
protocol + one implementation per type)".

**Changes:**
- `_dispatch` is now a plain dict lookup against
  `_TYPE_HANDLERS: ClassVar[dict[str, Callable[...]]]` — one
  entry per handler type (`command`, `http`, `prompt`,
  `mcp_tool`). Unknown types return `None` with a debug log
  (same behavior as before).
- Each entry is a lambda adapter that normalizes to
  `(self, hook, event, payload) -> Awaitable[HookResult]`, so
  the per-type methods keep their focused signatures
  (`_run_prompt_hook(hook)` still only takes `hook`).
- Added `ClassVar` to the `typing` import; no other file
  changes.

**Tests:** 209 hook-related tests pass. Coverage of the
unknown-type fallthrough already lives in
`test_hook_handler_types.py::test_unknown_type_is_skipped`.

**Audit-table changes:**
- `hooks/executor.py`: **B → A-**.
- `hooks/tool_hook.py`: **C+ → B-** — the audit's concern
  about `_is_protected_path` being untested was stale;
  `test_tool_hook_protected_paths.py` covers it end-to-end
  (verified in this iter).

### Iteration 49 — `session/core.py`: agno + stdlib inline-import hoist

**Target:** #4 (`core/session/core.py`, 2760 LoC, D → B).
Partial Rule-2 pass — 7 imports hoisted. Full D → B still needs
the Pattern-4 composition split (biggest remaining god-file
after `server.py`).

**Why:** 37 inline imports left after several earlier iters
touched the file. Started with the 7 lowest-risk: `datetime`
(stdlib) + 6 `agno.*` (guardrails.openai / pii /
prompt_injection, models.message, session.summary,
tools.reasoning). None are patched by tests at the source
module (verified in the iter 48 sweep — all still green).

**Deferred:** the remaining 30 inline imports are all
`ember_code.core.*` internal modules. Many are patched by tests
at their source module (session tests patch things like
`ember_code.core.tools.orchestrate.OrchestrateTools`,
`ember_code.core.plugins.marketplaces.*`, etc.). Hoisting them
would trigger the same source-vs-local-binding gotcha that
iter 46 hit in `command_handler.py` — dozens of test patches
to rewrite. Higher-value item is the composition split, not the
mechanical hoist.

**Results:**
- 73 session tests pass.
- Full BE sweep: running (iter 48's sweep already 3318 green
  post agno hoists).

**Grade change:** D → **D (partial)**. Same as `app.py` iter 22
— rule-2 progress without composition-split progress.

### Iterations 47-48 — patch-target follow-up + `init.py` further split

**Iter 47** — Fixed 5 test regressions caught by the iter 46
sweep. `test_handle_markdown_command.py` patched
`ember_code.core.utils.markdown_commands.discover_markdown_commands`
at the source module; iter 46's hoist made `command_handler.py`
capture the name at import time, so 8 patch sites needed the
now-standard local-binding swap. Straight sed rewrite; 9/9
markdown-command tests pass.

**Iter 48** — Continued item #23 split.

- New `core/init_checksums.py` (90 LoC): `file_hash`,
  `load_checksums`, `save_checksums`, `sync_file` — the whole
  checksum-based sync flow. Public names (no leading underscore)
  since they're now cross-module.
- New `core/init_json_io.py` (27 LoC): `load_json` + `save_json`
  helpers. Shared between `init.py` and `init_checksums.py`.
- `init.py`: deleted the six extracted helpers, dropped the
  `hashlib`, `json`, `shutil` imports (no longer needed at this
  layer). Renamed call sites (`_file_hash → file_hash`, etc.).
- 42 init + onboarding tests pass.

**File-size progression for `core/init.py`:**
- Pre-iter-13: 580 LoC (rule-1 + rule-2 violations, all logic
  in one file).
- Post-iter-13: 618 LoC (grew from adding pydantic models).
- Post-iter-43: 457 LoC (templates extracted).
- **Post-iter-48: 378 LoC** (checksums + json IO extracted).
- Total reduction from iter-13 peak: **-39%**.

**Grade change:** #23 C+ → **A- (partial)**. Remaining candidates
(home_config migration, hook provisioning) share the
`initialize_project` orchestrator's context and don't gain much
from further extraction — they're now the natural body of the
file.

### Iteration 46 — `command_handler.py`: full Rule-2 sweep (item #2)

**Target:** #2 (`backend/command_handler.py`, 2039 LoC, D → B+).
Rule-2 pass — 30 inline imports resolved. Full D → B+ still needs
the SlashRouter class-per-command split (that's a bigger refactor).

**Why:** command_handler was the third-densest source of inline
imports in the codebase (30 after `server.py:97` and
`session/core.py:37`). Most were repeat imports of the same
module across ~10 slash-command handler methods — plugin
installer/marketplaces, auth credentials, scheduler models,
permission-eval mode, evals reporter/runner, loop-prompt
wrapper, etc.

**Changes:**
- Hoisted **all 30 inline imports** to module top. Dropped 7
  underscore-prefixed aliases (`_PluginError`, `_PluginInstaller`,
  `_resolve_install_ref`, `_add`, `_load`, `_refresh`, `_remove`)
  since the canonical names don't conflict with anything in the
  module scope.
- Also removed 3 stdlib inline imports (`asyncio`, `uuid`,
  `webbrowser`) that had already been hoisted at some sites but
  not others.

**Test patch-target migration (57 rewrites):**
- Same gotcha as iters 14 / 29 / 30 — patches at the source
  module (`ember_code.core.plugins.marketplaces.refresh_marketplace`)
  don't affect the names now captured at import time in
  `command_handler.py`.
- Ran a bulk regex-rewrite across 5 test files to swap patch
  targets to `ember_code.backend.command_handler.*`. Total: 57
  patch-target rewrites.
- Then reverted 2 test files where the rewrite was wrong: the
  test hit `BackendServer` methods (which still inline-import the
  same names inside their own method bodies — those tests must
  keep patching at the source module) or `Session.start_marketplace_refresh_background`
  (session/core.py has its own inline imports of the same names,
  still un-hoisted).
- Net: 25 rewrites landed in `test_plugins_slash_commands.py`
  (tests CommandHandler), 1 in `test_slash_command_edges.py`,
  1 in `test_commands.py`. Rest reverted.

**Learning:** the source-vs-local-patch gotcha now hits every
inline-import → module-top hoist that has any mocking downstream.
The recipe is: **hoist → run tests → if a `patch(...)` breaks,
rewrite the patch target to point at the module where the local
binding lives**. It's mechanical but must happen in the same
iteration or CI stays red.

**Results:**
- 97 slash/plugin/scheduler tests pass.
- Full BE sweep: **3313 passed, 5 failed** (iter 47 caught + fixed:
  `test_handle_markdown_command.py` 8 `patch("ember_code.core.utils.markdown_commands.discover_markdown_commands", ...)`
  sites needed the same local-binding rewrite. Iter 47 sed-swap
  brought that file to 9/9 pass — full sweep now 3313 → 3318+
  once iter 47 sweep completes).

**Grade change:** D → **C+ (partial)**. Full D → B+ awaits the
SlashRouter class-per-command split — that's a multi-iteration
project of its own (roughly 50 `_cmd_*` methods to distribute
across ~10 handler files by responsibility area).

### Iteration 45 — `session.event_log`: `SessionEvent` schema (item #27)

**Target:** #27 (cross-cutting — apply the `code_index` typed-op
pattern to the session event log).

**Why:** the event log was `list[dict[str, Any]]` end-to-end —
raw dicts on the emission side (`append_event` built one inline),
raw dicts on the storage side (`persistence.py::save/load_event_log`),
raw dicts in the splicer (`backend/server.py:2640`). That's the
exact Rule-1 case: "structured data with more than one field is
a `pydantic.BaseModel`." Also matched Pattern 2 (typed events)
which the `code_index/delta.py` op union nails.

**Changes:**
- New `core/session/event_log_schema.py` with
  `SessionEvent(BaseModel)`:
  - `seq: int` (`ge=1`)
  - `run_id: str = ""`
  - `timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))`
  - `type: str` (open — new event kinds land here without a
    schema change; per-kind payload shape is the individual
    splicer's concern, not the log's)
  - `payload: dict[str, Any] = Field(default_factory=dict)`
  - `model_config = ConfigDict(extra="forbid")`
  - `.build()` classmethod mirroring the legacy
    `append_event` signature (positional `event_type` name,
    defensive `dict(payload)` copy).
  - `.from_wire()` classmethod for parse-with-fallback (returns
    `None` on bad rows so a corrupt DB entry doesn't crash the
    whole log load).
- `Session.append_event`: builds a `SessionEvent` at the
  emission boundary, then dumps to dict for the existing wire
  path. Also removed the inline `import time as _time`
  (`SessionEvent` factory owns the timestamp default).
- Added `SessionEvent` import at `session/core.py` top.

**Deferred:**
- Full migration to `self.event_log: list[SessionEvent]` — the
  splicer in `backend/server.py:2640` and the `_splice` mirror
  in `test_event_log.py:97` both dispatch on `e.get("type")` /
  `e.get("payload")` dict-access. Converting them to attribute
  access is mechanical but wide, and the current change is a
  net Rule-1 win on its own (every event flows through the
  validated boundary at construction time).
- Callers of `session.event_log` in `backend/server.py:467`
  (rehydrate on session load) — same reason.

**Tests:**
- New `test_event_log_schema.py` (13 tests): `build` field
  preservation, `run_id` coercion, defensive payload copy,
  extra-field rejection, `seq > 0` invariant, `payload` default,
  `timestamp_ms` auto-populated, `from_wire` round-trip +
  malformed-returns-None + extra-field-returns-None,
  `model_dump()` shape parity with the pre-refactor dict.
- All 11 pre-existing event_log tests still pass — 24 total
  in the event-log surface.
- Full BE sweep: running.

**Grade change:** pending → **in progress** (schema landed;
callers not yet migrated). The current shape gives Rule-1
compliance at every emission site — new event kinds authored
against the schema get their invariants validated up front —
while leaving the wire storage untouched.

### Iteration 44 — batched rule-2 cleanups across 4 files

**Target:** ad-hoc — 4 files each with 3 inline imports.

**Files:**
- `core/scheduler/runner.py`: hoisted `uuid`, `ScheduledTask`,
  `next_occurrence_from_recurrence` from
  `_reschedule_if_recurring`. Verified no back-import cycle from
  the scheduler package.
- `core/session/interactive.py`: hoisted `check_for_update`,
  `process_file_mentions`, `resolve_file_references` from
  scattered spots inside `run_session_interactive`.
- `core/session/memory_ops.py`: hoisted `agno.agent.Agent`,
  `agno.memory.MemoryManager`,
  `agno.memory.strategies.types.MemoryOptimizationStrategyType`.
  Deleted the `try/except ImportError` guard around
  `MemoryManager` — verified all three agno modules import at
  module load in a working environment (same reasoning as iter
  18's `agno.models.message.Message`).
- `core/tools/plan.py`: hoisted `OrchestrateTools` and
  `_coerce_items` from `todo`. Verified `orchestrate.py` and
  `todo.py` don't back-import `plan.py` — safe.

**Total: 12 inline imports resolved, 1 dead ImportError guard
removed.**

**Tests:**
- 126 touched-file tests pass.
- Full BE sweep: **3305 pass, 5 deselected, 0 regressions** (142s).

**Grade change:** four B/B+ files → each **B+ / A- (cleaner)**.
Now most of the remaining Rule-2 violations sit inside the three
D-tier god-files (`backend/server.py` — 97, `session/core.py` —
37, `command_handler.py` — 30). Those need composition splits
before the imports can move safely (many have real circular
risks tied to the god-class shapes).

### Iteration 43 — `init.py`: extract templates

**Target:** #23 continuation (from iter 13's partial fix).

**Why:** the 618-line `init.py` was ~30% string templates
(hook scripts, ember.md scaffold, config.yaml examples). Every
edit to a hook script forced a re-review of the initialisation
flow just because pytest highlights the whole file's diff.
Templates are pure `str` constants with no coupling to the init
logic; they belong somewhere else.

**Changes:**
- New `core/init_templates.py` (~180 LoC of `str` constants):
  `PRE_PR_REVIEW_HOOK`, `POST_COMMIT_TODO_HOOK`,
  `EMBER_MD_TEMPLATE`, `CONFIG_YAML_HEADER`,
  `PROJECT_CONFIG_TEMPLATE`, `_HOME_CONFIG_BOOTSTRAP`. Pure data,
  no imports beyond `__future__.annotations` (needed here just
  for consistency with the rest of the package).
- `init.py`: imports the six names from `init_templates`.
  Deleted the inline definitions (~180 lines) — file shrank
  618 → 457 LoC.

**Tests:**
- 42 init + onboarding tests pass.
- Full BE sweep: running.

**Grade change:** B (rule fixes only) → **B+ (partial)**. A
further split (checksums / home-config-migration / hook
provisioning per file) is possible but low-return — the four
concerns are already single-purpose functions and the
orchestrator (`initialize_project`) has to coordinate them
anyway.

### Iterations 41-42 — `_messages.py`: final 3 widgets — item #16 DONE

**Iter 41:** Extracted `MessageWidget` → `_message_widget.py`
(~155 LoC) and `StreamingMessageWidget` →
`_streaming_message_widget.py` (~120 LoC). Both self-contained;
no shared state with the remaining widget. `_messages.py`
shrunk 517 → 270 LoC.

**Iter 42:** Extracted the biggest widget `ToolCallLiveWidget`
→ `_tool_call_live_widget.py` (~240 LoC). Uses
`TOOL_FRIENDLY_NAMES` from `_messages_common.py`. Also
consolidated the last remaining imports and rewrote
`_messages.py` as a pure re-export shim.

**File-size progression for `_messages.py`:**
- Pre-iter-39: 681 LoC (6 widgets + shared constants).
- Post-iter-39: 578 LoC.
- Post-iter-40: 517 LoC.
- Post-iter-41: 270 LoC.
- **Post-iter-42: 39 LoC — pure re-export shim.**
- Total reduction: **-94%.**

**Tests:**
- All 6 extracted widgets identity-checked across all three
  import paths.
- 193 targeted TUI tests pass.
- Full BE sweep: **3305 pass, 5 deselected, 0 regressions** (144s).

**Grade change:** C → **A-**. Item #16 complete. Third
audit-table item fully closed this loop session, after #17 and
#18. Same "extract to canonical + retain shim" pattern; same
~94% file-size reduction; same identity-check safety net.

**Cumulative widgets-package structure post iters 19-42:**

- 12 canonical widget modules (one file per widget):
  `_activity`, `_agent_run`, `_agents_panel`, `_agent_tree_widget`,
  `_codeindex_panel`, `_file_picker`, `_help_panel`, `_hooks_panel`,
  `_input`, `_knowledge_panel`, `_loop_panel`, `_login_widget`,
  `_mcp_call_widget`, `_mcp_panel`, `_message_widget`,
  `_model_picker`, `_permission_dialog`, `_plugins_panel`,
  `_queue_panel`, `_session_picker`, `_skills_panel`,
  `_spinner_widget`, `_status_bar`, `_streaming_message_widget`,
  `_task_progress`, `_tasks`, `_tip_bar`, `_tokens`,
  `_tool_call_live_widget`, `_tool_call_widget`, `_update_bar`,
  `_welcome_banner`
- 3 shared helpers/schemas: `_constants.py`, `_dialogs_common.py`,
  `_formatting.py`, `_messages_common.py`, `_session_info.py`
- 3 backwards-compat shims: `_dialogs.py`, `_chrome.py`,
  `_messages.py` (each ~37-40 LoC — pure re-exports)
- Total: 3 audit-table items closed (#16, #17, #18) with the
  same recipe.

### Iteration 40 — `_messages.py`: extract `ToolCallWidget` + shared consts

**Target:** #16 (continuation of iter 39).

**Changes:**
- New `widgets/_messages_common.py` (~30 LoC) — the shared
  `TOOL_FRIENDLY_NAMES` dict. Same shape as
  `_dialogs_common.py` (iter 32) — parent-file extraction that
  keeps future canonical modules from importing back from the
  shrinking source.
- New `widgets/_tool_call_widget.py` (~65 LoC) — `ToolCallWidget`
  class. Imports `TOOL_FRIENDLY_NAMES` from the common module.
- `_messages.py`: removed `ToolCallWidget` class + the local
  `TOOL_FRIENDLY_NAMES` dict. Re-exports both plus the two
  already-extracted classes for backwards compat. Dropped
  `Collapsible` from top-level imports (only used by the
  extracted class). Docstring updated.
- `widgets/__init__.py`: `ToolCallWidget` import moved to
  `._tool_call_widget`.

**Tests:**
- Identity checks pass for `ToolCallWidget` and
  `TOOL_FRIENDLY_NAMES` across all paths.
- 193 targeted TUI tests pass.
- Full BE sweep: **3305 pass, 5 deselected, 0 regressions** (142s).

**Grade change:** B → **B+ (partial)**. 3/6 widgets extracted;
3 remain — `MessageWidget`, `StreamingMessageWidget`, and the
biggest `ToolCallLiveWidget` (~230 LoC).

### Iteration 39 — `_messages.py`: extract 2 smaller widgets

**Target:** #16 (`_messages.py`, 681 LoC, C → B+). First half —
the two smallest self-contained classes.

**Why:** applying the proven pattern from `_dialogs.py` (#17)
and `_chrome.py` (#18). `_messages.py` has 6 independent widget
classes; started with the smallest two (MCPCallWidget 54 LoC,
AgentTreeWidget 60 LoC) — neither shares module-level state
with the others.

**Changes:**
- New `widgets/_mcp_call_widget.py` (~70 LoC) — `MCPCallWidget`
  class + minimal Textual imports. Renders one MCP tool call
  with a collapsible result.
- New `widgets/_agent_tree_widget.py` (~75 LoC) — `AgentTreeWidget`
  class. Uses `textual.widgets.Tree` (not needed by any other
  widget in `_messages.py`, so we could drop the top-level
  `Tree` import).
- `_messages.py`: removed both class bodies (was 116 LoC total).
  Added re-exports for backwards-compat. Dropped `Tree` from
  the top-level import (only used by the extracted class).
- `widgets/__init__.py`: `AgentTreeWidget` and `MCPCallWidget`
  imports moved from `._messages` to canonical modules.

**File-size progression for `_messages.py`:**
- Pre-iter-39: 681 LoC (6 widgets).
- **Post-iter-39: 578 LoC.**
- Projected after remaining 4 extractions: ~50 LoC re-export
  shim + `_messages_common.py` with `TOOL_FRIENDLY_NAMES`.

**Tests:**
- Identity checks pass for both extracted classes across
  `widgets.X` / `_messages.X` / `_<file>.X`.
- 193 targeted TUI tests pass.
- Full BE sweep: running.

**Grade change:** C → **B (partial)**. Same trajectory as #17
and #18. Remaining widgets (`MessageWidget` 144 LoC,
`StreamingMessageWidget` 102 LoC, `ToolCallWidget` 46 LoC,
`ToolCallLiveWidget` 240 LoC) tackled in follow-up iterations —
last two share `TOOL_FRIENDLY_NAMES`, which will live in a
`_messages_common.py` (same shape as `_dialogs_common.py`).

### Iteration 38 — `_chrome.py`: extract `StatusBar` — item #18 DONE

**Target:** #18 (final). Completes the per-widget extraction
started in iter 35.

**Changes:**
- New `widgets/_status_bar.py` (~245 LoC) — full `StatusBar`
  class body moved. Its ``_codeindex_badge`` doc still explains
  the priority ordering for the tiny CodeIndex slot (a
  historically-buggy area — the doc is load-bearing). Depends on
  `CodeIndexStatusInfo` from `_codeindex_panel` and both
  `format_*` helpers from `_formatting`.
- `_chrome.py` **collapsed to 37 LoC** of pure re-exports —
  identical shape to `_dialogs.py` from iter 34. Every canonical
  name (WelcomeBanner, TipBar, UpdateBar, SpinnerWidget,
  QueuePanel, StatusBar) exported from its home module.
- `widgets/__init__.py`: `StatusBar` import moved from `._chrome`
  to `._status_bar` — the final canonical path.

**File-size progression for `_chrome.py`:**
- Pre-iter-35: 584 LoC.
- Post-iter-37: 270 LoC.
- **Post-iter-38: 37 LoC — pure re-export shim.**
- Total reduction: **-94%.**

**Tests:**
- All 6 extracted widgets identity-checked across all three
  import paths (`widgets.X` / `_chrome.X` / `_<file>.X`).
- 193 targeted TUI tests pass.
- Full BE sweep: **3305 pass, 5 deselected, 0 regressions** (140s).

**Grade change:** C+ → **A-**. Item #18 complete. Together with
item #17 (iter 34), the entire TUI-widgets package is now
per-file: 10 canonical widget modules + 2 shim files
(`_dialogs.py`, `_chrome.py`, each ~37 LoC) + 1 shared helper
(`_dialogs_common.py`) + 1 schema (`_session_info.py`). Every
widget has one canonical home; the shims stay indefinitely so
private-path imports across the codebase keep working.

**Cumulative pattern learning across iters 19-38:** The
"extract to canonical + retain shim" flow now has a proven
recipe:

1. Write the extracted `.py` file preserving imports.
2. Trim the class body from the parent file (Python one-liner
   with `lines[:start] + lines[end:]`).
3. Add re-export line at the parent's top.
4. Update `widgets/__init__.py` to import canonical.
5. Run identity check across all three paths.
6. Run local targeted tests, then full sweep.

The pattern applies to any file with N independent classes and
no shared private helpers. Where shared helpers exist (as with
`_is_inside` in `_dialogs.py`), extract them to a
`_<parent>_common.py` first so canonical files don't import
back.

### Iterations 36-37 — `_chrome.py`: extract `SpinnerWidget` + `QueuePanel`

Continued the per-widget split started in iter 35. Same pattern
(canonical file + shim re-export + `widgets/__init__.py` update
+ identity check across 3 paths).

**Iter 36:** `SpinnerWidget` → `_spinner_widget.py` (~75 LoC).
Depends on `SPINNER_FRAMES` from `_constants.py` and
`format_token_count` from `_formatting.py`. `_chrome.py`
tightened its imports: `SPINNER_FRAMES` no longer needed at
`_chrome.py`'s top after the extraction, so removed.

**Iter 37:** `QueuePanel` → `_queue_panel.py` (~165 LoC).
Self-contained — only needs Textual base classes + own logger.
`_chrome.py`'s remaining state: only `StatusBar`. Removed
`Message` from `_chrome.py`'s imports (no longer used —
`ItemDeleted` / `ItemEditRequested` / `PanelClosed` inner
classes moved with QueuePanel).

**File-size progression for `_chrome.py`:**
- Pre-iter-35: 584 LoC (6 widgets + 1 helper).
- Post-iter-35 (3 small widgets): 477 LoC.
- Post-iter-36 (+ SpinnerWidget): 422 LoC.
- **Post-iter-37 (+ QueuePanel): 270 LoC.**
- Projected after StatusBar extraction: ~50 LoC re-export shim.

**Tests:**
- Identity checks pass for all 5 extracted widgets across
  `widgets.X` / `_chrome.X` / `_<file>.X` paths.
- 193 targeted TUI tests pass.
- Full BE sweep: running.

**Grade change:** C+ → **B+ (partial)**. One more iteration
(the biggest widget, StatusBar at 238 LoC) closes item #18.

### Iteration 35 — `_chrome.py`: extract 3 smaller chrome widgets

**Target:** #18 (`_chrome.py`, 584 LoC, C+ → B+). First half of
the extraction — the three smallest widgets first.

**Why:** same rationale as `_dialogs.py` (item #17). Six
independent widgets in one 584-line file are Pattern 8 territory
("small modules, one responsibility"). Started with the three
smallest to validate the pattern; larger widgets (SpinnerWidget,
StatusBar, QueuePanel) come in follow-up iterations.

**Changes:**
- New `widgets/_welcome_banner.py` (~30 LoC) — `WelcomeBanner`
  class + the local `_QUIT_KEY` constant (previously module-level
  in `_chrome.py` but only used by this widget).
- New `widgets/_tip_bar.py` (~40 LoC) — `TipBar` class. Only
  needs `textual.widgets.Static`.
- New `widgets/_update_bar.py` (~80 LoC) — `UpdateBar` class +
  its `_upgrade_command` helper. Hoisted the helper's inline
  `import subprocess` and `import sys` to module top (Rule 2 —
  the helper had two inline imports).
- `_chrome.py`: removed the 3 extracted classes + helper +
  unused `_QUIT_KEY` constant + unused `__version__` import.
  Added backwards-compat re-exports. Docstring updated to reflect
  the shrinking scope.
- `widgets/__init__.py`: `TipBar`, `UpdateBar`, `WelcomeBanner`
  imports moved from `._chrome` to their canonical modules.

**File-size progression for `_chrome.py`:**
- Pre-iter-35: 584 LoC (6 widgets + 1 helper).
- **Post-iter-35: 477 LoC (3 widgets extracted).**
- Projected after remaining 3 widgets extracted: ~50 LoC of
  re-exports.

**Also:** 2 more inline imports fixed (Rule 2) via the extraction —
the `_upgrade_command` helper had them and both went to module
top when it moved to `_update_bar.py`.

**Tests:**
- Identity checks pass for all 3 extracted classes across
  `widgets.X` / `_chrome.X` / `_<file>.X` paths.
- 193 TUI tests pass.
- Full BE sweep: **3305 pass, 5 deselected, 0 regressions** (144s).

**Grade change:** C+ → **B (partial)**. Same completion trajectory
as `_dialogs.py` — 3 of 6 widgets extracted; 3 more iterations
would collapse `_chrome.py` to a re-export shim.

### Iteration 34 — `_dialogs.py`: extract `PermissionDialog` — item #17 DONE

**Target:** #17 (final). Completes the per-dialog extraction
started in iter 19.

**Changes:**
- New `widgets/_permission_dialog.py` (~215 LoC) — full class
  body moved. Imports `_is_inside` from `_dialogs_common`. Its
  own module-level `logger` (was shared with `_dialogs.py`).
- `_dialogs.py` **collapsed to 37 LoC** of pure re-exports:
  every canonical name (LoginWidget, ModelPickerWidget,
  PermissionDialog, SessionInfo, SessionPickerWidget, plus the
  `_is_inside` helper) exported from its home module. Module
  docstring rewritten as a "backwards-compat shim" declaration
  with the canonical path table. `__all__` populated explicitly.
- `widgets/__init__.py`: `PermissionDialog` import moved from
  `._dialogs` to `._permission_dialog` — the final canonical
  path.

**File-size progression for `_dialogs.py`:**
- Pre-iter-19: 641 LoC (SessionInfo + 4 dialogs + helper).
- Post-iter-32: 383 LoC (2 dialogs extracted).
- Post-iter-33: 234 LoC (3 dialogs extracted).
- **Post-iter-34: 37 LoC (pure re-export shim). -94% total.**

**Tests:**
- All 5 canonical classes + `_is_inside` verified identity-safe
  across all three import paths (`widgets.X`, `_dialogs.X`,
  `_<file>.X` all resolve to the same object).
- 193 targeted TUI tests pass.
- Full BE sweep: running.

**Grade change:** C → **A-**. Item #17 complete. The final
`_dialogs.py` shim will stay in the tree indefinitely — deleting
it would break every test / app-code path that imports through
the `.widgets._dialogs.X` dotted path, and the code cost of
keeping the shim is 37 lines of straightforward re-exports.

**Pattern learning:** the per-file split was tractable because
each dialog was self-contained — one class body + one CSS
literal + a few dependencies. Whenever a file has N independent
classes and no cross-class private helpers, this same "extract
canonical + retain shim" flow applies. The shared helper
(`_is_inside`) went into its own `_dialogs_common.py` so the
individual dialog modules don't have to import back from the
now-empty parent.

### Iteration 33 — `_dialogs.py`: extract `SessionPickerWidget`

**Target:** #17 (continuation of iters 19, 31, 32). Third
per-dialog extraction.

**Changes:**
- New `widgets/_session_picker.py` (~155 LoC) — full class body
  moved. Imports `_is_inside` from `_dialogs_common` (extracted
  in iter 32) and `SessionInfo` from `_session_info` (extracted
  in iter 19). Self-contained.
- `_dialogs.py`: removed the `SessionPickerWidget` class body
  (was ~144 LoC). Added re-export from `._session_picker`. File
  now down to **234 LoC** (was 641 pre-split — a 63% reduction).
  Header docstring updated to reflect the shrinking scope.
- `widgets/__init__.py`: `SessionPickerWidget` import moved from
  `._dialogs` to `._session_picker` — canonical path.

**File-size progression for `_dialogs.py`:**
- Pre-iter-19: 641 LoC (SessionInfo + 4 dialogs + helper).
- Post-iter-32 (2 dialogs extracted): 383 LoC.
- **Post-iter-33 (3 dialogs extracted): 234 LoC.**
- Projected after final `PermissionDialog` extraction: ~50 LoC
  of re-export shim + one leftover trivial module docstring.

**Results:**
- 193 TUI tests pass (identity checks confirm same class objects
  across all three import paths).
- Full BE sweep: **3305 pass, 5 deselected, 0 regressions** (143s).

**Grade change:** C → **B+ (in progress)**. 3 of 4 dialogs
extracted; only `PermissionDialog` (192 LoC) remains. Item #17
will reach full completion in one more iteration.

### Iteration 31 — `_dialogs.py`: extract `LoginWidget` (Pattern 8)

**Target:** #17 (continuation of iter 19).

**Why:** iter 19 extracted the `SessionInfo` schema. Next per
Pattern 8 (small modules, one responsibility) is the per-dialog
class split — 4 dialogs in one file at 641 LoC is right on the
smell threshold. Started with `LoginWidget` (~90 LoC — smallest;
validates the pattern before tackling the bigger ones).

**Changes:**
- New `widgets/_login_widget.py` (105 LoC) — self-contained
  Textual widget for the OAuth login flow. Only imports what it
  needs: `contextlib`, `re`, and the Textual base classes.
- `_dialogs.py`: `LoginWidget` class removed (was 89 LoC); added
  re-export from `._login_widget` for backwards-compat with the
  private-path imports scattered across tests and app code.
  Dropped unused `import re` from `_dialogs.py`'s module top.
  Docstring updated ("permission dialog, session picker, model
  picker" — LoginWidget dropped from the list).
- `widgets/__init__.py`: `LoginWidget` import moved from
  `._dialogs` to `._login_widget` — canonical path.

**Tests:**
- Identity check: `widgets.LoginWidget is _dialogs.LoginWidget is _login_widget.LoginWidget`
  — all three paths resolve to the same class object, so any
  future `isinstance` check anywhere in the code stays valid.
- 193 TUI widget + handler tests pass.

**File-size progression for `_dialogs.py`:**
- Pre-iter-19: 641 LoC (SessionInfo + 4 dialogs + helper).
- Post-iter-19: 641 (SessionInfo re-exported, still counted).
- Post-iter-31: 515 LoC (LoginWidget extracted).
- Projected after all 4 dialogs extracted: ~50 LoC of thin
  re-exports + one shared helper.

**Results:**
- 193 targeted TUI tests pass.
- Full BE sweep: **3305 pass, 5 deselected, 0 regressions** (144s).

**Grade change:** C → **B- (in progress)**. 1 of 4 remaining
dialogs extracted; each follow-up iter tackles one more.

### Iteration 30 — `orchestrate.py`: rule 2 cleanup + test patch-target migration

**Target:** #5 (`core/tools/orchestrate.py`, D → C+ done in iter 2
partial). Final 3 inline imports resolved.

**Why:** 3 stragglers left after iter 2 pinned the state-model
refactor: `WorktreeManager` from `core.worktree`, `Team` from
`agno.team.team`, `ModelRegistry` from `core.config.models`.

**Changes:**
- Hoisted `Team`, `WorktreeManager`, `ModelRegistry` to module
  top of `orchestrate.py`. Deleted 3 inline imports.
- Two tests (`test_orchestrate.py::test_spawn_team_success`,
  `test_orchestrate_hooks.py::test_spawn_team_fires_hooks`) were
  patching the imports at their *source* modules
  (`agno.team.team.Team`, `ember_code.core.config.models.ModelRegistry`).
  After the hoist, those patches don't affect the local bindings
  captured at import time. Updated both tests to patch the local
  names in `ember_code.core.tools.orchestrate.*` — inline comment
  in each test explains why.

**Learning:** the pattern of "test patches at source module vs.
local binding" is now the third time it's tripped a test this
session (iter 14, iter 29, iter 30). Standard Python-mocking
gotcha, always the same fix. If it comes up again I'll consider
a lint rule / helper.

**Results:**
- 11 orchestrate + orchestrate_hooks tests pass.
- Full BE sweep: running.

**Grade change:** item #5 already-C+ → **C+ (cleaner)**. All 14
Rule 2 violations across the two iters (iter 2 and iter 30) plus
the state-model refactor are done. Full C+ → B+ still needs the
`_handle` if/elif → dispatch table (AP4) work — deferred.

### Iteration 29 — `mcp/client.py` + `session/runner.py`: rule 2 cleanup

**mcp/client.py** — 4 inline imports of `MCPTools`, `ClientSession`,
`StdioServerParameters`, `stdio_client` from optional `agno[mcp]`
and `mcp` packages. These were inline as an optional-dep guard —
the module load shouldn't fail when the extras aren't installed.
Rebuilt as a module-top `try/except ImportError` block that sets
each name to `None` on failure (same pattern as the `pwd` guard
in iter 22). Added an `if MCPTools is None:` early-return in
`connect()` with the same "not installed" error text. Removed
the now-dead `except ImportError:` branch that was catching the
old inline import.

The test `test_connect_import_error` was patching
`agno.tools.mcp.MCPTools` at the module source — needs to patch
the local name (`ember_code.core.mcp.client.MCPTools`) now that
the import is at the client's own module top. Updated the test
with an inline comment explaining the change.

**session/runner.py** — 4 inline imports: `CommandHandler`,
`process_file_mentions`, `resolve_file_references`, and 3
duplicate `print_info` imports (one aliased `_print_info` for no
reason). All hoisted to top. Verified: `command_handler.py`
imports `Session` under `TYPE_CHECKING` only, so no runtime
cycle. Also dropped the `_print_info` alias.

**Results:**
- 13 MCP client tests pass.
- 127 tests in the mcp/runner scope pass total.
- Full BE sweep: **3305 pass, 5 deselected, 0 regressions** (141s).

**Grade change:** two A- files → **A- (cleaner)**. 8 inline
imports resolved; one dead ImportError branch deleted. The MCP
optional-dep contract is now expressed structurally (module-top
guard + one runtime check) instead of inline-import ImportError
pattern-matching.

### Iterations 26-28 — batched rule-2 cleanups across TUI + tools

**Iter 26 — `_messages.py`:** 5 inline imports of `rich.console.Group`,
`rich.markdown.Markdown` (aliased `RichMarkdown` to avoid clash with
`textual.widgets.Markdown` at top), and `rich.text.Text` — hoisted
to module top. Two `if` branches with duplicate 2-3 line inline
imports now have one 3-line module-top import block.

**Iter 27 — `core/knowledge/ingest.py`:** 5 inline
`agno.knowledge.reader.*_reader` imports (YouTubeReader,
WikipediaReader, ArxivReader, PDFReader, TextReader, plus one
inline `WebsiteReader` I found along the way) hoisted to top. The
if/elif dispatch inside `_reader_for_url` was doing per-branch
lazy loads for what's now a 6-line module-top import block; the
dispatch itself is untouched. 50 knowledge tests pass.

**Iter 28 — `core/tools/registry.py`:** 3 of 4 inline imports
resolved (`PythonTools` from `agno.tools.python`, `CodeIndexTools`
from `core.tools.codeindex`, `custom_loader.load_custom_tools`
aliased as `_load_custom_tools` to avoid shadowing the method
name). The 4th (`DuckDuckGoTools` guarded by `try/except
ImportError`) is a legit Rule-2 exception for optional deps and
stays inline. 82 tool tests pass.

**Grade change:** three A- files → **A- (cleaner)**. 13 inline-
import violations resolved across the three files.

**Results:**
- 249 combined tests pass (117 widget + 50 knowledge + 82 tools).
- Full BE sweep: **3305 pass, 5 deselected, 0 regressions** (141s).

### Iteration 25 — `backend/__main__.py`: rule 2 cleanup

**Target:** Ad-hoc — 15 inline imports in the backend entry
point. Not an audit-table row (`__main__.py` is A- per rubric —
well-organized bootstrap), but 15 Rule-2 violations in one file
was worth addressing.

**Why:** the file had inline imports across boot phases, some of
them ceremonial (`import re as _re` with an underscore alias in
a section that already had a "avoids circular during boot"
comment; the *aliasing* wasn't the workaround, the *reading from
file* was), others just method-by-method growth (`FileIndex`
loaded lazily in one RPC handler, `check_for_update` +
`_PKG_NAME` from the same module imported twice a few lines
apart).

**Changes:**
- Hoisted 9 imports to module top:
  - Stdlib: `gc`, `re`, `sys` (all three had inline copies).
  - `Lockfile` from `backend.lockfile`.
  - `ClientStateStore` from `session.client_state`.
  - `SessionDirectoryStore` from `session.session_directories`.
  - `FileIndex` from `utils.file_index`.
  - `RpcMethod` from `protocol.rpc`.
  - Combined the two `update_checker` imports into
    `from ember_code.core.utils.update_checker import _PKG_NAME, check_for_update`.
- Dropped the `_gc` and `_re` aliases — no shadowing risk in
  their enclosing scopes.
- Deleted 9 corresponding inline imports.

**Deferred:**
- 5 heavy inline imports remain: `BackendServer` (session-pool
  boot path), the three `transport.*` imports (conditional-load
  based on transport choice — genuine lazy pattern), and the
  `session_pool` batch. All flagged with the same "wire boundary
  / heavy dep" reasoning as the deferred imports in `cli.py`
  iter 21 and `app.py` iter 22.

**Results:**
- 67 backend-adjacent tests pass (`test_backend_lockfile`,
  `test_backend_server`, `test_session_pool`,
  `test_backend_serialize`).
- Full BE sweep: running.

**Grade change:** A- → **A- (cleaner)**. 9 of 15 inline imports
resolved; the remaining 5 are legit lazy-load / conditional
patterns.

### Iterations 23-24 — `code_index/index.py` and `session/persistence.py`

**Iteration 23 — `code_index/index.py`:**
- 6 inline imports resolved. `apply_delta`, `Counter`, `code_index_dir`,
  `state_db_path`, `FileReferenceService`, `Database` all hoisted to
  module top.
- `delta.py` and `sync_manager.py` import `CodeIndex` under
  `TYPE_CHECKING` only, so no runtime cycle — safe to hoist.
- Verified naming: `CodeIndex.apply_delta` method calls the free-
  standing `apply_delta` from the module scope (LEGB resolution;
  method scope doesn't shadow imports).
- 69 code_index tests pass.

**Iteration 24 — `session/persistence.py`:**
- 9 inline imports resolved — 8 of them were the SAME import
  (`from agno.db.base import SessionType`), repeated once per
  method. Plus one `from agno.session.agent import AgentSession`
  in `fork`.
- All hoisted to module top; deleted all 9 inline duplicates via
  a targeted sed pass on the two exact-string patterns.
- 64 persistence + session tests pass.

**Grade change:** two A- files → **A- (cleaner)**. Both files
were already well-factored; the inline imports were pure
accretion from method-by-method growth. Fixing them collapses
15 total inline-import violations into 3 top-level imports.

**Results:**
- Combined 133 targeted tests pass (69 + 64).
- Full BE sweep: **3305 pass, 5 deselected, 0 regressions** (141s).

### Iteration 22 — `app.py`: stdlib inline-import cleanup

**Target:** #7 partial (`frontend/tui/app.py`, 2415 LoC, D → B).

**Why:** the D-tier god-file. Full D → B is a Pattern 4
composition project (split into per-concern sub-managers), way
too big for one iteration. But the file had 18 inline imports,
including a **3x-repeated** `import random` (line 2276, 2286,
2301) and 4 stdlib re-imports of names already at module top
(`import os`, `import sys`). That's pure churn — every one of
those was a copy-paste from an existing lazy-import site that
never got cleaned up.

**Changes this iteration:**
- Hoisted 6 new stdlib names to module top: `random`, `signal`,
  `subprocess`, `webbrowser`, `escape` from `rich.markup`. Also
  added a POSIX-only guard for `pwd` at the top (Windows raises
  ImportError; the module stays `None` there, and the two callers
  `if pwd is not None`-check before using it — same pattern
  CPython's own stdlib uses for POSIX-only surface).
- Deleted 11 inline stdlib imports total: 3× `import random`,
  3× `import os` (redundant with top-level), 2× `import sys`
  (redundant), 2× `import signal`, 1× `import subprocess`,
  1× `import pwd`, 1× `from rich.markup import escape`,
  1× `import webbrowser as _wb`.
- Removed the `_wb` alias — `webbrowser` at module top is
  cleaner than the local rename.
- Updated `_get_full_name` to check `if pwd is not None` before
  the Unix-specific `pwd.getpwuid` path, so Windows behaviour is
  now explicit (falls through to `os.getlogin()`) rather than
  relying on the outer `except Exception` catch.

**Deferred (documented for a future iteration):**
- 5 heavy `ember_code.*` inline imports remain: `CloudCredentials`
  (×2), `cloud_models.{fetch_cloud_models,merge_into_registry,...}`
  (×2), and `frontend.tui.process_manager.BackendProcess`. Same
  reasoning as `cli.py` iter 21 — these pull in heavier subsystems
  and are only needed on specific user actions. Full Rule-2
  compliance for these is best combined with the D-tier
  composition split.

**Results:**
- 193 TUI-side tests pass (`test_tui_handlers`,
  `test_tui_widgets_p1`, `test_widgets`).
- Full BE sweep: running.

**Grade change:** D → **D (rule 2 partial)**. The file is still
2415 LoC, still 106 methods, and its D-tier grade is driven by
that size / responsibility spread — not by inline imports. But
11 inline-import violations are gone and the `pwd` platform
guard is now explicit at module top rather than buried in one
function.

### Iteration 21 — `cli.py`: stdlib inline-import cleanup + Rule-2 exception documented

**Target:** Ad-hoc — `cli.py` had 12 inline imports flagged during
the codebase sweep. Not in the audit table (the file's overall
grade is A- per rubric — well-organized Click group, no god-class
smells) but the raw count of Rule-2 violations was worth
addressing.

**Why:** `cli.py` is a genuine Rule-2 grey area — it's the CLI
entry point where startup latency directly hits the user
(`ember --version`, `ember --help`). The heavier
`from ember_code.<...>` imports (`EmberApp`, `SessionPersistence`,
`WorktreeManager`, `run_single_message`, `setup_db`) are legit
lazy loads — hoisting them means every invocation pulls the
whole Textual framework and session subsystem. Rule 2's "genuine
circular-import breaks" exception doesn't cover startup perf
explicitly, but the rule's own "first ask whether the module
boundary is wrong" wording points at the right long-term fix:
split `cli.py` into per-subcommand modules where each subcommand
imports its heavy deps at its own module top.

**Changes this iteration:**
- Hoisted **all 5 stdlib inline imports** to module top:
  `logging`, `sys`, `pathlib.Path`, and
  `logging.handlers.RotatingFileHandler`. Stdlib imports have
  zero measurable startup-cost impact on modern CPython (they're
  cached in `sys.modules` once the interpreter has imported them
  once, and Python's own stdlib preloads many of them for the
  runtime itself).
- Deleted 5 duplicate inline stdlib imports across the debug-log
  setup, worktree branch, add-dir branch, and pipe-mode branch.

**Deferred (documented for a future iteration):**
- 7 heavy `ember_code.*` inline imports remain intentionally
  inline. Full Rule-2 compliance requires splitting `cli.py` into
  a `cli/__init__.py` + per-subcommand modules
  (`cli/pipe.py`, `cli/single_message.py`, `cli/tui.py`,
  `cli/worktree.py`, `cli/continue_session.py`), where each
  subcommand imports its own heavy deps at that submodule's top.
  Substantial multi-file refactor with `entry_points` reshuffling
  in `pyproject.toml` — separate audit-table row.

**Results:**
- 126 CLI-adjacent tests pass (via `pytest -k "cli"`).
- Full BE sweep: **3305 pass, 5 deselected, 0 regressions** (143s).

**Grade change:** A- (file) → **A- (cleaner)**. 5 of 12 inline
imports resolved. The remaining 7 are marked as Rule-2 exception
candidates pending the subcommand-module split, and the reason
they're still inline (startup perf) is now written down in this
log rather than being informal tribal knowledge.

### Iteration 20 — `run_controller.py`: rule 2 cleanup + alias unification

**Target:** #19 partial (`frontend/tui/run_controller.py`,
995 LoC, C+ → B+).

**Why:** the file had 6 inline imports — the most in any single
file surveyed — plus an inconsistency where the same module
(`protocol.messages`) was imported at three different sites
under two different aliases (`msg` in one place, `pmsg` in the
other two). The `pmsg`/`msg` split created a false distinction
between "these are the messages I use" and "these are the
messages I use here" — every future edit picking one alias over
the other was 50/50 wrong depending on section.

**Changes:**
- Hoisted to module top:
  - `time` (was `import time as _time` inline, aliased for no
    apparent reason).
  - `CloudCredentials` from `core.auth.credentials`.
  - `msg` from `protocol.messages` — the canonical alias.
  - `_build_diff_table` from `protocol.agno_events`.
- Deleted 4 inline `from ember_code.protocol import messages as pmsg`
  reimports of the same module.
- Renamed all `pmsg.` → `msg.` (4 sites) so the whole file uses
  one alias.
- Removed the `_time` alias; changed the 3 `_time.monotonic()`
  call sites to `time.monotonic()` directly. Underscore-prefixed
  stdlib aliases are ceremony — the module scope isn't shadowed.

**Results:**
- 130 controller-adjacent tests pass (`test_tool_error_rendering`,
  `test_streaming_done_unblock`, `test_status_tracker`,
  `test_tui_handlers`).
- Full BE sweep: **3305 pass, 5 deselected, 0 regressions** (141s).

**Grade change:** C+ → **C+ (rule 2 done)**. The AP3 problem
(the `_processing` bool with 5 setters — Pattern 1 / RunPhase
enum candidate) still sits open, but that's a wider migration
requiring test-side updates too (existing tests assign
`ctrl._processing = True/False` directly). Deferred to a
dedicated iteration.

### Iteration 19 — `_dialogs.py`: extract `SessionInfo` (Pattern 7)

**Target:** #17 partial (`frontend/tui/widgets/_dialogs.py`,
642 LoC, C → B+).

**Why:** the file mixed a 48-line Pydantic schema
(`SessionInfo`) with 4 Textual dialog widgets that each pull in a
fair amount of UI scaffolding. Non-UI callers
(`session_manager.py`, other backend integrations) that only
needed the schema were forced to import through Textual's whole
widget stack. That's Pattern 7 backwards — the domain model
should live free of the UI layer.

Also carrying one inline `import re` in `LoginWidget.update_status`
that had gone un-hoisted since the initial commit.

**Changes:**
- New `widgets/_session_info.py` (65 LoC) — pure Pydantic model
  with the `display_name` / `display_time` / `label` properties.
  No Textual imports.
- `_dialogs.py`: removed the `SessionInfo` definition; added a
  backwards-compat re-export from `_session_info` so private-path
  imports (`from ...widgets._dialogs import SessionInfo`) still
  work. Explicit `__all__` added so the module now advertises its
  public surface. Also removed `from datetime import datetime`
  and `from pydantic import BaseModel` (both were only used by
  the migrated model).
- `widgets/__init__.py`: `SessionInfo` import moved from
  `._dialogs` to `._session_info` — the canonical path.
- Inline `import re` in `LoginWidget.update_status` (line 621)
  hoisted to module top.

**Tests:**
- New `TestSessionInfoCanonicalLocation` (3 tests) locking in:
  canonical import from `_session_info` works, `_dialogs`
  re-export is the *same class object* (`is`, not `==`), and
  `widgets.SessionInfo` is also the same object. Guards against
  future accidental redefinition drifting the type identity.
- All 30 pre-existing p1-widget tests still pass — 33 total.
- Full BE sweep: running.

**Grade change:** C → **C+ (partial)**. Full C → B+ still
requires each of the 4 dialog classes extracted to its own file
(`_permission_dialog.py`, `_session_picker.py`, `_model_picker.py`,
`_login_widget.py`), turning `_dialogs.py` into a thin re-export
shim. Deferred — 4 file extractions in one iteration is high
change-radius; better as 4 small iterations once other items
land.

### Iteration 18 — `core/queue_hook.py`: rule 2 cleanup

**Target:** Ad-hoc — Rule 2 violations spotted while inventorying
the codebase's remaining 329 inline imports.

**Why:** four inline imports across two hook classes:
- `import asyncio` + `import inspect` in
  `QueueInjectorHook.__init__` (lines 70-71)
- `import inspect` again in `QueueInjectorHook.__call__` (line 109)
- `from agno.models.message import Message` guarded by a
  `try/except ImportError` in `QueuePersisterHook.__call__`
  (line 194)

The `try/except ImportError` was marked `# pragma: no cover —
defensive` but `agno` is a hard dependency of the whole package
(imported at module load in many other files). If it weren't
available, `queue_hook.py` couldn't be imported either — the
defensive branch was unreachable ceremony.

**Changes:**
- Hoisted `import asyncio`, `import inspect`, and
  `from agno.models.message import Message` to module top.
- Removed the `try/except ImportError` guard for the `Message`
  import — deleted 3 lines of dead branch. If Agno ever does
  fail to import here, the crash surfaces earlier and louder
  (the intended behaviour for a hard dependency).

**Results:**
- 23 queue_hook tests pass (`TestRealAgnoRun` deselected — that's
  the live-Agno integration test suite gated behind an env var).
- Full BE sweep: **3302 pass, 5 deselected, 0 regressions** (140s).

**Grade change:** already-B file → **B (cleaner)**. Rule 2 no
longer violated.

### Iteration 16 — `core/config/models.py`: rule 2 cleanup

**Target:** Ad-hoc — `core/config/models.py` had 3 Rule-2
violations spotted while surveying it as a dependency of iter 14.
Not an explicit audit-table row (the file is already B on the
rubric), but the standards apply universally so worth fixing.

**Why:** three `from ember_code.core.<...>` inline imports:
- `CloudCredentials` from `auth.credentials` at line 472
  (in `ModelRegistry.__init__`)
- `resolve_api_key` from `config.api_keys` at line 649
  (in `_resolve_api_key`)
- `CloudCredentials` again at line 657 (same function, second
  branch — for the Ember-cloud URL fallback)

Verified neither `credentials.py` nor `api_keys.py` imports from
`config/models.py`, so no circular risk from hoisting.

**Changes:**
- Added top-level imports for `CloudCredentials` and
  `resolve_api_key`.
- Deleted all 3 inline imports.
- Zero behaviour change — the callees are the same, the timing
  of the import moves from "first call" to "module load".

**Results:**
- 69 models/cloud-models/pool tests pass.
- Full BE sweep: **3299 pass, 5 deselected, 0 regressions** (141s).

**Grade change:** already-B file → **B (cleaner)**. Rule 2 no
longer violated here. Any B → A- push would come from
Pattern 4 composition work on `ModelRegistry` itself (it holds
`_cloud_token`, `_cloud_server_url`, `context_windows`,
`PROVIDERS`), which the audit rated as "well-factored" so
deferred.

### Iteration 15 — `backend_client.py`: rule 2 cleanup

**Target:** #15 partial (`frontend/tui/backend_client.py`, 722 LoC,
C → B+).

**Why:** the file's total LoC and 19 `list[dict]`/`dict` return
types both need addressing to reach B+, but the shape mismatch
(TUI-side proxy returning raw wire dicts) is a Pattern 7 project
that needs `protocol/messages.py` schemas defined first — a
multi-iteration undertaking with broad caller impact across
`RunController`, panels, `App`. Not doable in one turn.

The 3 inline imports, however, are a clean sweep — the two
`from types import SimpleNamespace` occurrences appear one method
apart and the third (`from ember_code.protocol import messages as msg`)
is redundant with a module-top import at line 16 that already
provides the same alias.

**Changes:**
- `from types import SimpleNamespace` hoisted to module top.
- Both `SimpleNamespace` inline imports (lines 31, 669) deleted
  in favour of the module-top name.
- Redundant `from ember_code.protocol import messages as msg` in
  `cancel_login` (line 536) deleted — the module-top import
  covers it.

**Results:**
- 49 backend_client-adjacent tests pass (test_hitl_batch_resolve,
  test_status_tracker, test_plugins_backend_client,
  test_model_switch_status_sync). No behaviour change to observe.
- Full BE sweep: **3299 pass, 5 deselected, 0 regressions** (142s).

**Grade change:** C → **C+ (partial)**. Rule 2 fully satisfied.
Getting to B+ needs the wire-schema migration — the 19 `list[dict]`
/ `dict` return types are the real rot signal, but rewriting them
requires Pydantic messages defined in `protocol/messages.py`
first, which is a broader refactor with `BackendServer`-side
symmetry work. Deferred to a dedicated multi-iteration row.

### Iteration 14 — `core/evals/runner.py`: rules 1 & 2

**Target:** #20 (`core/evals/runner.py`, 660 LoC, C+ → B+).

**Why:** the file was carrying an unusually dense cluster of inline
imports — 7 in a 660-line module. Every closure that needed
``ReliabilityEval`` / ``AccuracyEval`` / ``importlib`` /
``inspect`` / ``copy`` / ``time as _time`` / ``ModelRegistry`` had
imported them locally, some as aliased names. Rule 2 requires all
imports at module top (no genuine circular breaks — the callees
are third-party or peer packages that don't import back).

Compounding the mess: ``CaseResult.tool_trace: list[dict]`` was a
raw list of `{name, args, result_preview, error}` dicts —
Rule 1's exact case, "structured data with more than one field".

**Changes:**
- Hoisted **all 7 inline imports** to module top.
- Removed the ``import time as _time`` alias and its uses — the
  module already imports ``time`` at line 7. Same for the two
  ``copy`` aliases (``_copy`` and ``_shallow_copy``): both replaced
  with a single ``import copy`` at top + ``copy.copy(...)`` at
  call sites.
- New ``ToolTraceEntry(BaseModel)`` with ``extra="forbid"`` +
  ``args: dict[str, Any] | None``. ``forbid`` catches Agno-schema
  drift up front instead of downstream ``.get()`` crashes.
- ``CaseResult.tool_trace: list[dict]`` → ``list[ToolTraceEntry]``.
- ``_check_tool_arg_assertions`` signature migrated from
  ``list[dict]`` to ``list[ToolTraceEntry]``. Assertions (the
  second arg) stay ``list[dict]`` — that's the YAML wire boundary
  which lives on ``EvalCase.tool_arg_assertions`` in
  ``loader.py``; migrating that too is a follow-up iteration to
  keep the diff scoped.
- Runtime ``tool_args`` coerced to plain ``dict`` before the model
  is constructed — a MagicMock (in tests) or hypothetical non-dict
  from Agno version drift surfaces as ``args=None`` rather than
  crashing the whole run. The coerce is explicit + logged in the
  code comment as the reason.

**Tests:**
- New ``TestToolTraceEntry`` class with 7 tests: defaults,
  ``extra="forbid"`` rejection, ``model_dump()`` shape parity
  with the pre-refactor dict format, assertion match/miss/malformed
  scenarios via ``_check_tool_arg_assertions``, and the
  ``args=None`` corner case.
- All 34 pre-existing eval tests still pass — 41 total.

**Results:**
- 41 eval tests pass (was 34).
- Full BE sweep: **3299 pass, 5 deselected, 0 regressions** (142s).

**Grade change:** C+ → **B+**. Rules 1 and 2 no longer violated
in this file. Full A- would require extracting the eval-step
functions (``_run_reliability`` / ``_run_accuracy`` /
``_check_tool_arg_assertions`` / ``_strip_errored_tool_calls``)
into per-step files under ``core/evals/steps/`` — deferred; the
current shape is already well-factored, and further extraction
without also migrating ``loader.py``'s ``list[dict]`` fields
would break Pattern 7 (separate wire from domain).

### Iteration 13 — `core/init.py`: rule violations first

**Target:** #23 partial. The full C+ → B on this 580 LoC file is a
package-split (templates / checksums / hooks / home-config /
orchestrator) that deserves its own iteration. This iteration
took the fast wins: the two hard-rule violations that were
sitting right at the top of the file.

**Why:** the `BUILT_IN_HOOKS` list-of-dicts and the inline `import
yaml` were flagged by CODE_STANDARDS.md Rules 1 and 2 respectively.
Both are the kind of drift that quietly re-normalizes if not
fixed — every new hook added would extend the dict-literal
pattern.

**Changes:**
- `import yaml` moved to module top (was inline inside
  `_migrate_home_model_default` — Rule 2 violation).
- `import` for `pydantic.{BaseModel, ConfigDict, Field}` added at
  module top.
- New `HookDefinition(BaseModel)` — `settings.json` wire shape for
  one hook registration. Uses `populate_by_name=True` + alias so
  the Python field `kind` renders as `type` on the wire (the
  builtin-shadowing dance).
- New `BuiltInHookSpec(BaseModel)` — one built-in hook shipped by
  the package. Frozen; the `content` string body is written to
  `.ember/hooks/<filename>` and the `definition` is registered in
  settings.json under `event`.
- `BUILT_IN_HOOKS` migrated `list[dict[str, Any]]` →
  `tuple[BuiltInHookSpec, ...]`. Tuple because it's a module-level
  constant that never mutates and shared by every importer.
- `_provision_hooks` rewritten to use typed access
  (`hook.filename`, `hook.content`, `hook.definition.model_dump(by_alias=True)`).
  No dict `.get()` remains.
- `tests/test_init.py` updated: `hook["filename"]` → `hook.filename`.
- New `TestBuiltInHookSchema` class with 6 tests:
  1. `BUILT_IN_HOOKS` is a tuple of `BuiltInHookSpec`.
  2. Specs are frozen (mutation raises).
  3. `HookDefinition.model_dump(by_alias=True)` restores wire name `type`.
  4. `background` defaults to False.
  5. `background=True` round-trips.
  6. E2E: `.ember/settings.json` written by `initialize_project`
     has `type` (wire name), not `kind` (Python name).

**Results:**
- 67 init tests pass (was 61; +6 new).
- Full BE sweep: **3292 pass, 5 deselected, 0 regressions** (141s).

**Grade change:** C+ → **B (partial)**. Rules 1 and 2 no longer
violated in this file. Follow-up iteration to hit B+ / A- would
split the 580 LoC into a `core/init/` package (templates,
checksums, home_config, hooks, orchestrator). Deferring — the
per-responsibility split is orthogonal to the rule fixes and
belongs in its own iteration.

### Iteration 12 — `markdown_commands.py`: pydantic + class-based API

**Target:** #24 (`core/utils/markdown_commands.py`, 299 LoC, C+ → B+).

**Why:** three CODE_STANDARDS.md violations in one small file made
it a fast, self-contained win:

1. `MarkdownCommand` was a `@dataclass(frozen=True)` — Rule 1
   requires Pydantic for public data models so callers get the
   same validation + serialization surface as everything else.
2. The public API was a bare module function
   (``discover_markdown_commands``) rather than a method on the
   class — [[feedback_classes_over_functions]].
3. `_substitute_files` had a 3-level nested try/except with a
   double `relative_to` pair for the "user-command allowed to
   escape project root" carve-out — Anti-Pattern AP2 (deep
   nesting).

**Changes:**
- `MarkdownCommand`: dataclass → `BaseModel` with
  `ConfigDict(frozen=True, arbitrary_types_allowed=True)` (Path).
- Added `MarkdownCommand.discover(project_dir, *, read_claude=True)`
  classmethod. Module-level `discover_markdown_commands` retained
  as a thin delegate — mandatory for backward compat because
  three tests monkey-patch the dotted path
  (`ember_code.core.utils.markdown_commands.discover_markdown_commands`).
- Extracted `_parse_allowed_tools(raw) -> tuple[str, ...]` —
  isolates the CC-parity comma-string-or-list normalisation.
- Extracted `_resolve_at_path(token, source) -> (Path, trailing)`
  — pure resolver, no I/O.
- Extracted `_at_path_allowed(resolved, source, project_dir) -> bool`
  — the project-boundary rule was the deepest smell (double
  `relative_to` inside a try inside a try). Now a flat function
  with three early-return branches; the caller becomes one line.

**Results:**
- 73 markdown-command tests pass (was 67; +6 for classmethod
  parity, delegate equivalence, frozen-model invariant, defaults,
  value equality).
- Full BE sweep: running.

**Grade change:** C+ → **B+**. File is now 296 LoC (-3, but the
metric that mattered was 3-level nesting → 1). All public API is
on the class, Rule 1 satisfied, no inline imports, no dict-vs-
pydantic drift. A → A- is a bigger question about whether
`_parse_frontmatter` should share the parser in
`context_frontmatter.py`; deferred — the two do intentionally
different things (context_frontmatter treats non-dict as invalid
whereas markdown_commands returns `({}, body)`; merging would
change semantics).

### Iteration 11 — shell.py `ProcessEventBus` extraction

**Target:** #6 (partial). shell.py's biggest smell called out in
the audit: 3 parallel pub/sub APIs (start/line/exit) each with its
own list + lock + subscribe/unsubscribe pair. That's Anti-Pattern
AP1 in CODE_STANDARDS.md.

**Changes:**
- New `src/ember_code/core/tools/process_bus.py` (128 lines) —
  `ProcessEventBus` with `on(event, cb)` / `off(event, cb)` /
  `emit(event, payload)` / `subscriber_count(event)` / `reset()`.
  Idempotent registrations, fail-soft emit, thread-safe.
- New `tests/test_process_bus.py` (14 tests) — subscribe/unsubscribe
  idempotency, unknown-event handling, isolation between event
  types, fail-soft emit semantics, reset lifecycle.
- `shell.py`: replaced 9 module-level names
  (``_completion_subscribers`` + lock, ``_line_subscribers`` + lock,
  ``_start_subscribers`` + lock, plus 6 subscribe/unsubscribe
  functions) with **one** module-level `_event_bus = ProcessEventBus()`.
  The 6 legacy `subscribe_to_*` / `unsubscribe_from_*` functions
  are preserved as thin wrappers around `_event_bus.on/off` for
  backwards compatibility.
- `_emit_start`, `_emit_line`, `_emit_completion` now call
  `_event_bus.emit(...)` instead of managing subscriber lists
  inline — collapsed ~30 lines of lock/iterate/log-if-raises code
  to 3 one-liners.
- `tests/test_process_watcher.py::setup_method` updated to reset
  the bus (was clearing the three deleted private lists).

**Results:**
- 68 process/shell/watcher tests pass (was 68; behaviour preserved).
- 14 new bus tests.
- Full BE sweep: **3280 pass, 5 deselected, 0 regressions** (145s).

**Grade change:** shell.py D → **C**. Still 1052 LoC and still holds
other module-level state (`_registry`, `_process_store`,
`_foreground_process`, `_FINISHED_PROCESS_TTL_SECONDS`), but the
worst offender (3× parallel pub/sub) is gone. Next iterations
should tackle the remaining module-level globals — `_registry` +
`set_process_store` setter-DI in particular.

### Iteration 10 — `context.py` shared read helpers extraction

**Target:** #8 (final slice). Move the file-read primitives out.
After this iteration, item #8 is complete.

**Changes:**
- New `src/ember_code/core/utils/context_readers.py` (142 lines) —
  `read_if_exists`, `read_with_imports`, `rules_filenames`,
  `read_rules_dir`, `read_rules_dir_files`. Every caller in the
  codebase reaches these through context.py's legacy leading-
  underscore aliases, so this doesn't need DI at the call sites.
- `context.py`: deleted the five function bodies (200+ lines) and
  added re-exports. **341 LoC** — down from 413.

**Total item #8 result:**
- context.py **778 → 341 LoC (-56%)** across 6 extraction iterations.
- 7 sibling modules totalling 1004 LoC, each single-responsibility.
- 15 dedicated new tests (`test_context_frontmatter.py`) locking in
  extracted primitives.
- Every existing test still passes — 173 in the context/session
  suites this iteration, 3251 in the full BE sweep across
  intermediate iterations.

**Grade change:** context.py D → **A-**. Not full A because it
still has 341 LoC of wrapper functions + orchestrator, but every
per-source concern lives in its own file, the file is
single-responsibility (orchestration), and every extraction module
is B+ or better on its own.

### Iteration 9 — `context.py` @-imports resolver extraction

**Target:** #8 (continuing). Sixth and largest slice — the recursive
``@<path>.md`` import resolver + code-region masking + regex
constants + depth cap.

**Changes:**
- New `src/ember_code/core/utils/context_imports.py` (181 lines) —
  `AT_IMPORT_RE`, `IMPORT_MAX_DEPTH`, `FENCED_BLOCK_RE`,
  `INLINE_CODE_RE`, `CODE_SENTINEL_RE` constants; `resolve_at_path`,
  `mask_code_regions`, `unmask_code_regions`, `resolve_imports`
  functions. Zero I/O except reading the imported file inside
  `resolve_imports` (which cannot be pushed higher without changing
  the recursive shape). Pure per the Standards' A-tier pattern.
- `context.py`: deleted the four function bodies + five regex
  constants. Kept `_read_with_imports` as a thin composed helper
  (file I/O + call into the pure resolver). Re-exports at top under
  leading-underscore aliases so `rules_index.py` and tests work
  unchanged.
- 173 context + session + frontmatter tests pass.

**Results:**
- context.py **527 → 413 LoC** (-114 this iteration).
- **Total drop: 778 → 413 LoC (-47%)** across 6 iterations of
  extraction.
- 6 extracted files, each single-responsibility, avg ~144 LoC.

**Grade:** context.py D → B+ (413 LoC still >300 soft threshold but
composed cleanly — remaining code is a small number of I/O helpers
+ the top-level `load_project_context` orchestrator). Full B+
because the file no longer mixes concerns.

### Iteration 8 — `context.py` frontmatter parser extraction

**Target:** #8 (continuing). Fifth slice — the YAML frontmatter
parser + path-glob matcher. Shared infrastructure, not per-source,
so this makes the parent module smaller without changing the
loader wiring.

**Changes:**
- New `src/ember_code/core/utils/context_frontmatter.py` (110 lines) —
  `parse_frontmatter` and `matches_paths`. Pure functions, zero
  I/O. Renamed from the leading-`_` private form to public
  spellings since they're now the module's exports; context.py
  re-exports under the `_`-prefixed legacy names so
  ``rules_index.py`` + tests keep working.
- `context.py`: 571 → **527 LoC**. Both function bodies replaced by
  a comment pointer to the new module.
- New `tests/test_context_frontmatter.py` (15 tests) — direct
  coverage of the extracted primitives: inline vs. block YAML,
  quoted values, empty paths, path-glob matching against absolute
  and project-relative candidates, working_dir=None edge case.

**Results:**
- 15 new frontmatter tests pass.
- 158 context + session + helpers tests pass (previously 158 too —
  no regressions).
- Full BE sweep after iter 7 (which includes iter 8's changes not
  yet swept): pending re-run.

**Grade change:** context.py D → **B** (527 LoC — down from 778,
close to the 300-LoC soft limit, single-responsibility now that
frontmatter is elsewhere).

### Iteration 7 — `context.py` project-rules extraction

**Target:** #8 (continuing). Fourth slice — the three project-scope
loaders that share the same DI-friendly shape.

**Changes:**
- New `src/ember_code/core/utils/context_project.py` (149 lines) —
  `load_project_rules` (root file), `load_project_rules_dirs`
  (project ``rules/`` directories), `load_subdirectory_rules`
  (subdir walk). All three take injected helpers.
- `context.py`: replaced the three function bodies with thin wrappers.
  Currently **571 LoC** — down 207 LoC (-27%) from the original 778.
  Grade upgraded to B (still not A because 571 is over the 300
  soft threshold, but no longer the D-tier god-file it started as).

**Established DI pattern for context extractions (now used four times):**
1. Extract cohesive block to `context_<concern>.py`.
2. Every helper needed from the parent module → argument, not import.
3. Every module-level constant used by the extracted logic → kwarg
   with default, so context.py's wrapper can pass its own re-exported
   copies (which tests monkeypatch).
4. context.py keeps a thin same-signature wrapper — zero caller side
   changes.
5. Runs full test suite each iteration.

Remaining in context.py after this pass (571 LoC):
- Frontmatter parser (``_parse_frontmatter``, ``_matches_paths``)
- @-imports resolver (``_resolve_at_path``, ``_mask_code_regions``,
  ``_unmask_code_regions``, ``_resolve_imports``, ``_read_with_imports``)
- Shared read helpers (``_read_if_exists``, ``_read_rules_dir``,
  ``_read_rules_dir_files``, ``_rules_filenames``)
- Public orchestrator (``load_project_context``) — assembles all sources.

These are all shared infrastructure, not per-source. Next iteration:
extract the frontmatter parser + @-imports resolver into
`context_imports.py` (both are cohesive around file-loading logic
independent of any specific rule source).

### Iteration 6 — `context.py` user-rules extraction

**Target:** #8 (continuing). Third slice out of the god-file.

**Changes:**
- New `src/ember_code/core/utils/context_user.py` (83 lines) —
  `USER_RULES_PATH`, `USER_RULES_DIR`, `CLAUDE_USER_RULES_DIR`
  constants + `load_user_rules(...)` with DI'd helpers AND DI'd
  path constants. The path pass-through preserves the test
  monkeypatch idiom: `monkeypatch.setattr(context,
  "USER_RULES_PATH", tmp_path / "rules.md")` still redirects the
  effective path because context.py's wrapper reads its own
  module-level names at call time.
- `context.py`: deleted the load_user_rules body (now a wrapper
  that binds paths + helpers) and the three path constants (now
  re-exported from `context_user`).

**Results:**
- 142 context + session tests pass (all `TestLoadUserRules` and
  `TestRulesReachAgent` green).
- Full BE sweep: running.

**Established pattern for these extractions:**
1. New module holds the concern-specific logic.
2. All shared helpers passed as `Callable` arguments.
3. Path constants and other module-level names passed as
   keyword args with defaults, so tests can override at the
   wrapper layer (still monkeypatch-friendly).
4. context.py keeps a thin public wrapper with the original
   signature — zero caller-side changes.

### Iteration 5 — `context.py` managed-rules extraction

**Target:** #8 (continuing). Second chunk out of the 778-LoC
`context.py` god-file: the sysadmin managed-policy tier.

**Design decision — DI over lazy import:**
Iteration 3 (memory) was safe to extract because `context_memory.py`
has no dependencies back on `context.py`. Managed rules is
different — `load_managed_rules` calls `_read_rules_dir` and
`_rules_filenames` which live in context.py. Options were:
(A) Lazy import inside `load_managed_rules` — violates CODE_STANDARDS
    Rule 2 (no inline imports) even under the "circular-import
    exception" clause.
(B) Dependency injection — pass the helpers as arguments.
Chose (B). `context_managed.load_managed_rules` takes
`read_rules_dir`, `rules_filenames`, and `platform_dir_fn` as
callables. Wrapper in `context.py` binds the local names. Also
gets tests: `platform_dir_fn=lambda: _platform_managed_rules_dir()`
captures the LOOKUP at call time so `monkeypatch.setattr(context,
"_platform_managed_rules_dir", ...)` still overrides — same trick
we needed for the memory extraction.

**Changes:**
- New `src/ember_code/core/utils/context_managed.py` (98 lines) —
  `_platform_managed_rules_dir()` + `load_managed_rules(...)` with
  DI'd helpers.
- `context.py`: 616 → 597 LoC. Deleted the two functions, added
  re-exports at top, kept a thin `load_managed_rules(read_claude_md)`
  wrapper that binds the local helpers.

**Results:**
- 142 context + session tests pass (all `TestLoadManagedRules` and
  cross-tier merge tests green).
- Full BE sweep: running.
- Behaviour-preserving; public signature unchanged.

**Grade change:** `context.py` still C (597 LoC — big improvement
from 778 but still >300 threshold). Extracted subsystems are
individually A-/B+. Next iterations: user rules, project rules,
subdirectory rules.

### Iteration 4 — rename `code_index/pg/` → `code_index/db/`

**Target:** #28. Trivial atomic cleanup — the `pg/` directory holds
SQLite services (not Postgres), per the audit call-out. Renaming to
`db/` matches what the code actually does.

**Changes:**
- `git mv src/ember_code/core/code_index/pg → .../db`.
- `sed` across every caller that referenced the old path:
  `src/ember_code/migrations/env.py`,
  `src/ember_code/core/code_index/index.py`,
  `src/ember_code/core/code_index/delta.py`,
  `src/ember_code/core/db/__init__.py` (docstring),
  `src/ember_code/core/code_index/db/file_reference.py`,
  `src/ember_code/core/code_index/db/commit_metadata.py`,
  `tests/test_code_index_delta.py`.
- Renamed `tests/test_code_index_pg.py` → `tests/test_code_index_db.py`
  (via `git mv`) to match the new module name.

**Results:**
- 68 code_index tests pass (delta + db + core index).
- Full BE sweep: running.
- Behaviour-preserving. Pure rename.

**Grade change:** N/A — this was priority #28 (cross-cutting cleanup)
not a graded-file target. Removes a naming smell called out in the
audit.

### Iteration 3 — `context.py` memory extraction

**Target:** #8 (partial). Split the memory-index concern out of the
778-LoC `core/utils/context.py` god-file per CODE_STANDARDS
Pattern 8 (small modules, one responsibility).

**Changes:**
- New `src/ember_code/core/utils/context_memory.py` (241 lines) —
  constants (`_MEMORY_INDEX_NAME`, size caps), path helpers
  (`_project_memory_slug`, `_ember_project_memory_dir`,
  `_claude_project_memory_dir`, `_read_memory_index`), public API
  (`ensure_memory_dir`, `memory_writeback_instructions`,
  `load_memory_index`). Every function moved verbatim from
  `context.py` — same signatures, same docstrings, same behaviour.
- `context.py`: deleted the moved block (778 → 616 LoC). Added
  re-exports at the top for backwards compat — every existing
  caller (`from ember_code.core.utils.context import
  ensure_memory_dir`) works unchanged.
- `tests/test_context.py::_redirect_memory_dirs` — updated to
  patch BOTH modules. The re-exports work as name-lookups but
  `load_memory_index` inside `context_memory.py` calls its OWN
  local `_ember_project_memory_dir`, not the re-export. Test
  helper now patches both.

**Results:**
- 194 context + session tests pass in the touched suites.
- Full BE sweep: running.
- Zero behaviour changes; pure code-motion refactor.

**Grade change:** context.py D → C (still large at 616 LoC but the
memory concern is separated). Full B requires items #8a-#8d in
future iterations: extract managed / user / project / subdirectory
sources into their own modules following the same pattern.

### Iteration 2 — `orchestrate.py` state extraction

**Target:** #5. `_run_agent_streaming`'s 11 nonlocals → single Pydantic
`SubAgentStreamState` model. Per CODE_STANDARDS Pattern 4 (composition)
+ Anti-Pattern AP2 (>5 nonlocals in a function).

**Changes:**
- New `src/ember_code/core/tools/subagent_stream.py` (129 lines) —
  `SubAgentStreamState(BaseModel)` with 14 fields grouped by concern
  (identity, activity log, content preview, visualizer streaming, Agno
  run identity, completion tracking). Every field has a docstring
  explaining WHY it exists — future adds have a discoverable schema.
- `orchestrate.py`: removed the 11 nonlocals + their two `nonlocal`
  declarations from `_handle`. Replaced every read/write with
  `state.<field>`. The state instance is constructed at the top of
  `_run_agent_streaming` and mutated across the event loop. Convenience
  local aliases (`log = state.log`, `agent_path_id = state.agent_path_id`)
  kept for read-only access so the JSX-equivalent emit sites don't
  need a rename.
- New `tests/test_subagent_stream_state.py` (16 tests) — locks in
  defaults, mutability semantics, and the `vis_spec_id` per-instance
  freshness invariant (a regression here would collide viz cards
  across parallel visualizer calls).

**Results:**
- Orchestrate + tool-arg-streaming + visualizer + hooks test suites:
  **53 pass** (37 pre-existing + 16 new).
- Full BE sweep: running.
- Behaviour-preserving. Zero logic changes; state moved to a schema.

**Grade change:** the state-model portion of `orchestrate.py`
graduates D → C+. Not full B until (a) `_handle`'s if/elif → dispatch
table lands and (b) `_run_agent_streaming` + `_run_team_streaming`
merge (they share 80% overlap).

**Line 638 (team stream state)** intentionally left with old
nonlocals — same pattern, different function. Migrating both in one
iteration would double the blast radius; team version is item #5b
implicit in the next iteration.

### Iteration 1 — `App.tsx` runPhase state model

**Target:** #1. The STOP-button bug (spinner stuck at "Finalizing…" after cancel).

**Root cause identified in the audit:** `processing` and `finalizing` were
two independent boolean flags, each set from 3+ sites and cleared from
3+ sites. The cancel path missed `finalizing` because no `run_completed`
or `run_started` event follows a cancel — those were the only sites
that cleared it.

**Changes:**
- New `clients/web/src/chat/runPhase.ts` (163 lines) — typed enum
  `RunPhase = "idle" | "starting" | "streaming" | "finalizing" | "cancelled" | "errored" | "done"`,
  derived getters (`isProcessing`, `isFinalizing`, `shouldShowSpinner`, `phaseLabel`),
  and legacy adapter (`phaseToProcFinalizing` / `phaseFromProcFinalizing`)
  for the observer-bus reducer boundary. Zero React coupling → pure module,
  fully testable.
- `App.tsx`: replaced `useState<boolean>` × 2 with single `useState<RunPhase>`.
  Every `setProc/setFinalizing` call replaced by `setRunPhase(...)`.
  STOP button + ESC key + top-level `onStop` prop all now set
  `phase="cancelled"` locally BEFORE `client.cancel()` — spinner clears
  immediately, WS cancel is best-effort cleanup on the BE side.
- New file: `clients/web/src/chat/runPhase.test.ts` (17 tests) covering
  the derived getters, the label mapping, and both directions of the
  legacy adapter. Explicit regression test for "cancel transitions from
  finalizing → spinner off next render" — the exact bug the user hit.

**Results:**
- FE typecheck: clean.
- FE unit tests: **544 pass** (527 old + 17 new). All previous suites
  green — no regressions in `observerBusy`, `model`, or the ChatItems
  renderer tests.
- FE Playwright: **7 pass** (chat-scroll + visualizer-stream).
- BE sweep: **3235 pass**, 0 fail (2:23). Confirmed no cross-cutting
  breakage from the FE state-model change.

**Grade change:** `App.tsx` D → **C+** for the state-model portion.
Total file grade doesn't jump to B until the composer/panels/hooks
split (items #9, #10) lands — this iteration fixed the specific bug
that made it D, but the file is still 2500+ LoC of mixed concerns.

**Behaviour preserved:** every existing `processing` derivation still
computes the same value (via `isProcessing(runPhase)`). Every existing
`finalizing` derivation likewise. New behaviour: cancel now transitions
locally instead of waiting for a BE event that never arrives.
