# Testing plan — parity matrix (61 rows)

For each row: **automated coverage** + **manual verification steps** to confirm end-to-end. Where the automated coverage is thin, the manual steps are the contract.

---

## Hooks (rows 1–6, 39) ✅ VERIFIED

**Status:** 178/178 automated tests passing + live walkthrough through the Tauri app on 2026-06-29 confirmed hook firing across `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`. One real bug surfaced and was documented: hook matchers regex-match the **internal Agno function name** (`run_shell_command`, `save_file`, etc.) — not the friendly catalog name (`Bash`, `Write`). All docs (HOOKS.md, TOOLS.md, portal/HOOKS.md, portal/TOOLS.md) and built-in hook configs were updated to use the internal names. See [[memory:project_overview]] for the matcher-translation table.

**1. Hook event catalog (18 of 30)** — `tests/test_hook_events_new.py`, `test_hooks.py`. **Manual:** drop a `PreToolUse` hook in `~/.ember/settings.json` that exits 2; run a tool; verify it blocks. Repeat for `PostToolUse`, `SessionStart`, `UserPromptSubmit`, `Stop`, `Notification`, `SubagentStop`, `PreCompact`, `PermissionRequest`, `PermissionDenied`.

**2. Hook handler types (4 of 5)** — `tests/test_hook_handler_types.py`. **Manual:** configure each handler type (`command`, `http`, `prompt`, `mcp_tool`) in settings; trigger each; verify the right side-effect (shell exec / HTTP POST / system message / MCP tool call).

**3. Hook execution modes** — `tests/test_hook_async_rewake.py`. **Manual:** configure `async_rewake: true` hook with `sleep 3 && echo "reminder" && exit 2`; fire a tool; verify next turn shows the reminder.

**4. Hook matcher syntax (3-mode)** — `tests/test_hook_matcher.py`. **Manual:** test each: empty matcher (always fires), `Edit|Write` (exact pipe-list), regex `/^Bash.*/` (regex search). Confirm `Edit` doesn't match `Edit2`.

**5. Hook exit-code/JSON contract** — `tests/test_tool_hook.py`. **Manual:** hook that exits 2 → blocks; exit 0 + JSON → parsed; exit 1 → non-blocking warning.

**6. `permissionDecision` envelope** — `tests/test_hook_permission_decision.py`, `test_hook_envelope_parser.py` (18 cases). **Manual:** PreToolUse hook returning `{"hookSpecificOutput":{"permissionDecision":"deny"}}` → tool blocks with that decision; `"allow"` → skips HITL prompt.

**39. `mcp_tool` hook handler** — `tests/test_hook_handler_types.py`. **Manual:** connect an MCP server with a permission-deciding tool; configure as a PreToolUse hook; verify the MCP tool's return shapes the gate decision.

---

## Permission system (rows 7–9, 51)

**7. Permission modes (5)** — `tests/test_permission_eval.py`, `test_permission_flows.py`. **Manual:** invoke each via slash command — `/plan`, `/accept`, `/bypass` — and verify the ModeBadge updates and the next tool call routes accordingly.

**8. Permission evaluation pipeline (6 steps)** — `tests/test_permission_eval.py`. **Manual:** configure `deny: ["Bash(rm *)"]` and `acceptEdits` mode; verify rm is still blocked (deny beats mode).

**9. Bypass-resistant scoped deny** — same. **Manual:** `/bypass` then try a denied command; verify it still blocks.

**51. acceptEdits mode** — `test_permission_flows.py`, FE `StatusBits.test.tsx`. **Manual:** `/accept` → green ACCEPT EDITS chip → edits skip HITL → `/accept off` → chip disappears.

---

## Settings & policy (rows 10, 11) ✅ VERIFIED

**Status:** Full 5-tier precedence pinned by automated tests + the existing per-tier coverage. Two live touchpoints (writing `/Library/Application Support/Ember/managed-settings.yaml` with sudo, and verifying a managed `CLAUDE.md` shows up in agent instructions on session start) are covered by `TestFiveTierPrecedence` + `TestManagedPolicyInContextOutput` respectively — no manual sudo step required for the automated pass.

**10. Settings precedence (5 tiers)** — `tests/test_settings.py::TestFiveTierPrecedence` (4 tests, pins managed > CLI > project.local > project > user > defaults in one stack), `TestLoadSettings::{test_user_global_config_loaded, test_project_beats_user_global}`, plus the existing per-tier tests. Also exercises the `settings.json` reader added in commit `ad58a0a` so user-tier `~/.ember/settings.json` actually reaches `PermissionEvaluator`.

**11. Managed-policy CLAUDE.md** — `tests/test_context.py::TestManagedPolicyInContextOutput::test_managed_section_appears_first` asserts the `# Managed Policy` section appears BEFORE any project rules in `load_project_context` output. `TestLoadManagedRules` (6 tests) covers the file-system layer: ember.md/CLAUDE.md reads, @-import scoping to the managed dir (security), unknown-platform fallback, CLAUDE.md disable flag.

---

## Rules / context (rows 12–18) ✅ VERIFIED

**Status:** Already pinned by 127 automated tests across `test_context.py`, `test_rules_index.py`, and `test_hooks_cross_tool.py`. The interesting integration points — managed-policy ordering in agent instructions, path-scoped activation, @-import depth cap + code-span skipping, dual `.ember/` + `.claude/` rules namespace, MEMORY.md ordering in the context block — all have direct assertions.

**12. CLAUDE.md root + subdir hierarchy** — `test_rules_index.py::{test_subdirectory_rules_found, test_multiple_levels_returned_shallowest_first, test_claude_md_picked_up_when_enabled, test_both_ember_and_claude_md_load_in_same_dir, test_each_file_returned_at_most_once}` + `test_context.py::TestLoadSubdirectoryRules::{test_collects_subdirectory_rules, test_collects_claude_md_from_subdirectories}`. Walks ember.md + CLAUDE.md from root and every nested subdir, asserts shallowest-first ordering and per-file dedup.

**13. .local.md overrides** — `test_rules_index.py::{test_local_md_override_loads_after_committed, test_local_md_alone_still_loads, test_claude_local_md_picked_up, test_local_dedup_across_calls}` + `test_context.py::TestLocalOverrides`. Pins that ``.local.md`` loads AFTER the committed file (so its content overrides) and survives the dedup pass across repeated RulesIndex calls.

**14. @import depth (4 hops)** — `test_context.py::TestAtImports::test_depth_capped`. Chains A→B→C→D→E with `@./` imports; asserts the 5th hop's `@./` stays literal so a deep cycle can't blow up the prompt.

**15. @import skips code spans** — `test_context.py::TestAtImports::{test_inline_code_span_skipped, test_triple_backtick_fence_skipped, test_tilde_fence_skipped, test_fenced_block_with_info_string, test_indented_fence_up_to_three_spaces, test_mixed_code_and_text_both_handled, test_imported_file_with_own_code_block}`. Seven tests covering every Markdown code-region shape so `` `@./fake.md` `` and fenced code blocks stay literal.

**16. Path-scoped rules** — `test_rules_index.py::{test_path_scoped_rule_fires_on_matching_touch, test_path_scoped_rule_misses_when_glob_does_not_match, test_path_scoped_rule_dedup_across_calls, test_path_scoped_unconditional_rule_skipped_here, test_path_scoped_claude_rules_dir, test_path_scoped_rule_at_import_resolves, test_path_scoped_absolute_path_glob, test_path_scoped_rule_body_skips_code_region_imports}`. Eight tests on `paths:` frontmatter — fires on glob match, skipped otherwise, glob honours absolute paths, @-imports inside scoped bodies still resolve, code regions inside the body skip @-imports the same way as regular rules.

**17. Cross-tool rules reading** — `test_context.py::TestLoadUserRules::{test_reads_claude_rules_when_enabled, test_skips_claude_rules_when_disabled}` for user-tier `~/.claude/rules/`; `test_rules_index.py::{test_path_scoped_claude_rules_dir, test_path_scoped_claude_rules_skipped_when_cross_tool_disabled, test_dual_namespace_independent_rules_both_fire}` for the project tier `<proj>/.claude/rules/` + dual-namespace coexistence with `<proj>/.ember/rules/`. Plus `test_hooks_cross_tool.py` (the test plan's mis-citation — that file covers HOOK cross-tool, not rules; the rule cases live in test_context/test_rules_index as above).

**18. Auto-memory MEMORY.md index** — `test_context.py::TestLoadMemoryIndex` (8 tests, line/byte caps + UTF-8 boundary + Claude fallback + ember-wins-over-claude) + `TestMemoryIndexInContextOutput::test_memory_section_after_managed_before_user` (pins the section ordering: Managed Policy → Memory Index → User Rules), `TestProjectMemorySlug` (slug derivation from project path), `TestEnsureMemoryDir` (creation + idempotence + OSError-swallow), `TestMemoryWritebackInstructions` (frontmatter shape + memory-dir path + all four memory types named in the writeback instructions).

---

## Slash commands (rows 19, 20, 21, 27) ✅ VERIFIED

**Status:** Pinned by 131 automated tests across 7 files + a contract test in `clients/web/src/components/Composer.test.ts` that the FE autocomplete menu lists every BE handler.

**19. Markdown-authored commands** — `tests/test_markdown_commands.py` (29 tests: frontmatter parsing, discovery in `.ember/commands/` + `.claude/commands/` across project + user tiers, project-overrides-user collisions, ember-beats-claude at same tier) + `tests/test_handle_markdown_command.py` (12 tests: dispatch integration, `$ARGUMENTS` rendering, exception fall-through, cross-tool toggle).

**20. `slash_commands` RPC** — `tests/test_slash_commands_rpc.py` (12 tests). Asserts the RPC returns built-ins + markdown commands + user-invocable skills in one response, honours the cross-tool toggle for `.claude/` markdown commands, excludes non-user-invocable skills.

**21. Built-in slash command catalog** — `tests/test_commands.py` (12 tests on dispatch routing — known / unknown / help / config / clear / mcp / model / etc.) + `tests/test_slash_command_edges.py` (per-command edge cases) + `tests/test_plugins_slash_commands.py` (`/plugin enable|disable|...` subcommand semantics). **FE menu parity**: `clients/web/src/components/Composer.test.ts::ships every BE command handler in the autocomplete menu` enumerates ALL 31 non-alias BE handlers and asserts each appears in `BUILTIN_COMMANDS` — silent drift becomes a failing test.

**27. SlashCommand re-entrant tool** — `tests/test_slash_command_tool.py` (12 tests). Pins the agent-facing tool: blocked commands (`/quit`, `/exit`) refused case-insensitively + with args, missing leading slash inferred, empty/whitespace returns error, `/help <topic>` returns the topic markdown.

---

## Tools (rows 22–30) ✅ VERIFIED

**Status:** 211 automated tests across the toolchain. The headline check — that the **main team is shell-first** (NO `Read`/`Grep`/`Glob`/`LS`; those are registry-only for sub-agents) — is now pinned by `test_session.py::TestMainTeamToolkit` so a contributor can't silently re-add a Read tool and regress the v0.4.0 design.

**22. Tool catalog (shell-first main team)** — `tests/test_session.py::TestMainTeamToolkit` (7 new tests) pins the 5-tool always-on core (`Write`, `Edit`, `Bash`, `Schedule`, `NotebookEdit`), the registry-only exclusion of `Read`/`Grep`/`Glob`/`LS`/`Python`, the `--no-web` permission gate on `WebSearch`/`WebFetch`, the silent `ImportError` fallback for missing extras, the `CodeIndex` gating on `_codeindex_available`, and the immutability of `Session._MAIN_CORE_TOOLS` (it's a tuple, not a list). Per-tool internals also pinned: `test_tool_functions.py` (31 tests on individual tool behavior).

**23. Shell tool** — `tests/test_shell_background_notify.py` (7 tests on background process lifecycle), `test_tool_error_rendering.py` (17 tests on stderr/exit-code rendering in chat cards). Includes the `_search_router` integration removed in a later session — the parser/router code is gone but the shell tool's stream-to-chat behavior stays pinned.

**24. Web fetch/search** — `tests/test_web_tools.py` (21 tests). DuckDuckGo search, URL fetch, HTML extraction, permission-deny path, empty-result handling.

**25. TodoWrite** — `tests/test_todo_tool.py` (20 tests). `todo_write` validation (statuses, activeForm aliasing), live status updates flowing through the PlanCard checklist via the `todos_updated` push channel.

**26. Task sub-agent dispatch** — `tests/test_orchestrate.py` (6 tests). `spawn_agent` / `spawn_team` semantics, nesting depth caps, total-agent caps.

**27. SlashCommand re-entrant** — covered in Slash commands above (`test_slash_command_tool.py`).

**28. Background process tracking** — `tests/test_shell_background_notify.py` (7 tests) for background= True + completion-notification queue injection; `test_monitors.py` (26 tests) for the `Monitor` primitive that the LSP / monitors plugin tier builds on.

**29. Sub-agent allow-listed tools** — `tests/test_orchestrate.py`. Sub-agents that declare `tools: [Read, Grep, Glob]` in their frontmatter DO get those toolkits (the registry-only set) without leaking them onto the main team.

**30. Sub-agent worktree isolation** — `tests/test_orchestrate_worktree.py` (18 tests). Worktree creation, edits land in the worktree (not main), cleanup on agent completion, worktree-already-removed-mid-run handling.

**Bonus coverage:** `test_notebook.py` (17, NotebookEdit), `test_schedule_tools.py` (4, Schedule), `test_custom_tools.py` (12, `.ember/tools/` discovery), `test_codeindex_tools.py` (32, CodeIndex query + tree).

---

## Plugins (rows 31–37) ✅ VERIFIED

**Status:** 272 tests across 15 files cover plugin loading, applying (skills/agents/hooks/MCP/LSP/monitors), marketplaces, installer, background refresh, panel UI, session integration, slash commands (`/plugin install|update|remove|enable|disable|marketplace ...`), agent restrictions, and managed-scope policy. Row 34 (theme primitive) is a documented gap — the primitive isn't shipped.

**31. Plugin primitives (skills/agents/hooks)** — `tests/test_plugins_loader.py` (15, discovery + namespace), `test_plugins_apply.py` (13, wiring loaded content into the session), `test_plugins_backend.py` (28, BE-side state machine), `test_plugins_installer.py` (18, install/update/remove flow), `test_plugins_session_integration.py` (6, end-to-end session boot with plugins), `test_plugins_panel.py` (22, panel UI), `test_plugins_slash_commands.py` (36, `/plugin*` subcommand semantics), `test_plugins_background_refresh.py` (5, marketplace refresh in background), `test_plugins_git.py` (9, git-based install path), `test_plugins_backend_client.py` (15, client RPC).

**32. LSP server primitive** — `tests/test_lsp.py` (33 tests). Plugin-declared LSP servers (`.lsp.json`) launch / restart / shut down with the session; `lsp_query` tool returns hover / definition / references.

**33. Monitor primitive** — `tests/test_monitors.py` (26 tests). `.monitors.json`-declared processes start on session boot, supervisor restarts on crash, drains stdout into the chat-injection queue.

**34. Theme primitive** — **not shipped.** Documented gap; no automated coverage to mark.

**35. Plugin install scopes (4)** — `tests/test_plugin_managed_scope.py` (14 tests). User / project / project.local / managed tiers; managed-tier plugins refuse disable.

**36. Plugin discovery namespaces** — `tests/test_plugins_loader.py`. Walks `~/.ember/plugins/`, `~/.claude/plugins/`, `<proj>/.ember/plugins/`, `<proj>/.claude/plugins/`; honours `cross_tool_support` for the `.claude/` sides.

**37. Plugin agent restrictions** — `tests/test_plugin_agent_restrictions.py` (13 tests). Plugin agents that try to declare `mcpServers:` get a WARN log + the field stripped — plugins can't auto-attach MCP servers without user opt-in.

---

## MCP (row 38) ✅ VERIFIED

**38. MCP server integration** — 66 tests across `tests/test_mcp_*.py`: `mcp_approval` (14, HITL gate on MCP tool calls), `mcp_client` (13, lifecycle + reconnect), `mcp_config` (19, `.mcp.json` schema + multi-server), `mcp_policy` (15, deny/ask/allow per server + per tool), `mcp_transport` (5, stdio vs HTTP).

---

## Session & search (rows 40–44, 61) ✅ VERIFIED

**40. Session storage** — `tests/test_db_engine.py` (20 tests, AsyncSqliteDb + table layout + multi-session isolation).

**41. Session forking** — `tests/test_session_fork.py` (13 tests, fork-creates-new-id + history-clone + memory inheritance).

**42. In-session chat search** — `test_search_chat.py` (23 BE) + `clients/web/src/components/ChatSearchBar.test.ts` (FE) — Cmd+F, substring search, jump-to-result with highlight pulse.

**43. Semantic code index** — 184 tests across the `test_codeindex_*` and `test_code_index.py` files: query, tree, sync manager, build_tree, refs_for, status, auto-clean, availability-refresh, filters, disambiguation, eval fixture, panel, tree-attach.

**44. Host-side trigram code search** — covered indirectly via `test_codeindex_sync_manager` (35); FE host-bridge tests were investigated and reverted in this session (see [[project_ide_search]] memory).

**61. MEMORY.md write-back** — `test_context.py::TestMemoryWritebackInstructions` (5) + `TestLoadMemoryIndex` (8) + `TestMemoryIndexInContextOutput` (2).

---

## Loop / schedule / knowledge / orchestration (rows 45–49) ✅ VERIFIED

**45. `/schedule`** — `tests/test_schedule_tools.py` (4, tool layer) + `test_scheduler.py` (30, parser + cron + one-shot + recurring + state.db persistence + crash-resume).

**46. `/loop`** — `tests/test_loop.py` (42 tests on iteration accounting, `loop_set_total`, `loop_stop`, autonomous-mode wrapper, persistence on interrupt, exit conditions).

**47. Team orchestration** — `tests/test_orchestrate.py` (6 tests, `spawn_team` + nesting + max-agent-cap).

**48. Knowledge base** — 72 tests: `test_knowledge_index.py` (12), `test_knowledge_ingest_helpers.py` (21), `test_knowledge_ops.py` (12), `test_knowledge_panel.py` (18), `test_knowledge_tools.py` (9). Chroma collection ops, embedder selection (local SentenceTransformer vs cloud), URL ingest, project-scoped collections.

**49. Memory manager + learning** — `tests/test_memory_manager.py` (6), `test_memory_ops.py` (6), `test_learning.py` (5).

---

## Plan mode + output styles (rows 50, 52) ✅ VERIFIED

**50. Plan mode** — `tests/test_plan_mode.py` (65, +5 new this session on `/plan` slash command researcher-arming) + `tests/test_handle_pause_evaluator.py` (16, the pre-HITL evaluator dispatch added this session) + `tests/test_plan_rehydrate.py` (15, session-restore of plan_store from message history). FE: `PlanCard.test.tsx` + `model.test.ts` plan helpers. Plan mode UX was extensively iterated live in this session — researcher auto-fires from `/plan`, PlanCard renders inline at the chronological position of `exit_plan_mode`, Approve auto-executes and reverts mode on `streaming_done`.

**52. Output styles** — `tests/test_output_styles.py` (24 tests, style discovery + selection + system-prompt injection).

---

## Surfaces (rows 53–58) ✅ VERIFIED (where automated; live-tested for Tauri this session)

**53. CLI/TUI** — `tests/test_tui_*.py`: `test_tui_formatting.py` (23), `test_tui_handlers.py` (76), `test_tui_widgets_p1.py` (27). 126 tests on the Textual TUI.

**54. Tauri desktop** — Live walkthrough this session covered every interaction (HITL dialog, PlanCard, slash commands, scroll, command-mode entry/exit, mode badges, settings precedence). Rust shell-level tests in `clients/tauri/src-tauri/src/lib.rs`.

**55. Web bundle (Playwright)** — `clients/web/e2e/`: `app.spec.ts` (29 tests), `demo.spec.ts` (orchestrate demo scenarios), `chat-scroll.spec.ts` (4 new this session, pins the `followOutput="auto"` + `atBottomThreshold={50}` contract). Pre-existing `app.spec.ts::custom session id` failure is documented as unrelated to current work.

**56–58. VSCode / JetBrains / shared bundle** — all four surfaces (Tauri / VSCode / JetBrains / web) load the same `clients/web/dist`; FE tests (486 vitest) cover the shared bundle once. Live verification on VSCode and JetBrains surfaces is per-release manual QA — not in scope for this audit.

---

## Tauri-specific UX (rows 59, 60) ✅ VERIFIED

**59. External link routing** — INIT_SCRIPT contract tests in `clients/tauri/src-tauri/src/lib.rs`. The Tauri shell intercepts `<a>` clicks with `http`/`https`/`mailto`/`tel` hrefs and routes them to `plugin:opener|open_url`; pre-shipped + live-verified earlier in the session series.

**60. Chat list virtualization** — Now pinned by `clients/web/e2e/chat-scroll.spec.ts` (4 tests on `followOutput="auto"` + `atBottomThreshold={50}` against `?demo=chat-scroll`, which uses Virtuoso identically to the live App). Smoothness with 500+ turns is also implicit in `react-virtuoso` itself.

---

## Manual QA shortlist (highest-risk paths)

If you only have an hour:
1. **Plan mode lifecycle** (row 50): `/plan` → ask for plan → Approve → execute → verify ModeBadge transitions.
2. **External link routing** (row 59): click link in chat → opens OS browser (WKWebView regression-prone).
3. **Worktree isolation** (row 30): spawn sub-agent with `isolation: worktree` → verify writes land in a separate worktree, not main.
4. **Permission bypass-deny** (row 9): `/bypass` then run a denied command → verify it still blocks.
5. **Session fork** (row 41): `/fork` → switch → verify history mirrored, edits in new session don't touch the old.
6. **MEMORY.md write-back** (row 61): tell agent something personal → new session → verify it remembers.
7. **Multi-surface render parity** (rows 53–58): smoke each surface (TUI / Tauri / web / VSCode / JetBrains) loads + sends one message.
