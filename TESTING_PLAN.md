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

## Settings & policy (rows 10, 11)

**10. Settings precedence (5 tiers)** — `tests/test_settings.py`, `test_cli_permission_wiring.py`. **Manual:** place conflicting setting in user / project / project.local; verify the most-local wins. On macOS, drop file in `/Library/Application Support/Ember/managed-settings.yaml`; verify it overrides CLI flags.

**11. Managed-policy CLAUDE.md** — `tests/test_context.py`. **Manual:** drop CLAUDE.md in the managed dir; verify it appears at the top of the agent's instructions on session start.

---

## Rules / context (rows 12–18)

**12. CLAUDE.md root + subdir hierarchy** — `tests/test_rules_index.py`. **Manual:** add `subdir/CLAUDE.md`; touch a file in that subdir via Edit tool; verify the rule landed in the session reminder.

**13. .local.md overrides** — `test_rules_index.py`. **Manual:** add `ember.md` and `ember.local.md` with different content; verify `.local.md` is appended after.

**14. @import depth (4 hops)** — `tests/test_context.py:test_depth_capped`. **Manual:** chain 5 files A→B→C→D→E with @imports; verify E remains as literal `@./e.md`.

**15. @import skips code spans** — `test_context.py`. **Manual:** write `` `@./fake.md` `` in a rules file; verify it stays literal in the agent's instructions.

**16. Path-scoped rules** — `test_rules_index.py`. **Manual:** add `.ember/rules/security.md` with `paths: src/auth/**` frontmatter; verify it activates only when you touch a file under `src/auth/`.

**17. Cross-tool rules reading** — `test_hooks_cross_tool.py`. **Manual:** set `rules.cross_tool_support: true`; place a rule in `~/.claude/rules/`; verify it loads.

**18. Auto-memory MEMORY.md index** — `test_context.py`. **Manual:** create `~/.ember/projects/<slug>/memory/MEMORY.md` with a few entries; start a session in that project; verify MEMORY.md content lands in system prompt.

---

## Slash commands (rows 19, 20, 21, 27)

**19. Markdown-authored commands** — `tests/test_markdown_commands.py`. **Manual:** create `.ember/commands/review.md` with frontmatter + `$ARGUMENTS` body; type `/review my topic` in composer; verify the rendered prompt fires.

**20. `slash_commands` RPC** — `tests/test_slash_commands_rpc.py`. **Manual:** in dev tools, call `get_slash_commands` RPC; verify it returns built-ins + markdown + skill entries.

**21. Built-in slash command catalog** — `tests/test_commands.py`. **Manual:** type `/` in composer; scroll the menu; verify all 21+ commands appear.

**27. SlashCommand re-entrant tool** — `tests/test_slash_command_tool.py`. **Manual:** ask the agent "use the slash_command tool to list my sessions"; verify it dispatches `/sessions` and returns the output. Confirm `/quit` is refused.

---

## Tools (rows 22–30)

**22. File-op tools** — `tests/test_tool_functions.py`. **Manual:** the **main team is shell-first** — `Read`/`Grep`/`Glob`/`LS` are NOT registered to the main team (intentional v0.4.0 design — see `core/session/core.py:1051` and parity matrix row 22). To exercise file ops, ask the agent to: `cat README.md` (uses Bash), `rg "TODO" src/` (uses Bash), `find . -name "*.py"` (uses Bash), Write+Edit a file (uses dedicated tools). Read/Grep/Glob CAN be exercised by spawning a sub-agent whose frontmatter lists them — e.g. `/agents` then call one of the explorer-type agents.

**23. Shell tool** — `tests/test_shell_background_notify.py`. **Manual:** agent runs `run_shell_command`; verify stdout streams to the chat card.

**24. Web fetch/search** — `tests/test_web_tools.py` (21 cases). **Manual:** ask the agent to fetch a URL; verify HTML extraction works.

**25. TodoWrite** — `tests/test_todo_tool.py`. **Manual:** agent calls `todo_write` with 3 items; verify the PlanCard checklist shows them; agent calls again with one marked `in_progress`; verify status updates live.

**26. Task sub-agent dispatch** — `tests/test_orchestrate.py`. **Manual:** ask agent to spawn a sub-agent; verify the orchestrate card appears with the sub-agent's progress.

**27. SlashCommand re-entrant** — covered above.

**28. Background process tracking** — `tests/test_shell_background_notify.py`, `test_monitors.py`. **Manual:** start a long-running shell with `background: true`; verify `read_process_output` and `stop_process` work.

**29. Sub-agent allow-listed tools** — `tests/test_orchestrate.py`. **Manual:** define an agent in `.ember/agents/` with `tools: [Read, Grep]`; spawn it; verify it can't Edit.

**30. Sub-agent worktree isolation** — `tests/test_orchestrate_worktree.py`. **Manual:** spawn an agent with `isolation: worktree`; verify a new git worktree is created and the agent's edits land there (not in main).

---

## Plugins (rows 31–37)

**31. Plugin primitives (skills/agents/hooks)** — `tests/test_plugins_*.py` (13 files). **Manual:** install a plugin via `/plugin install <url>`; verify its agents/skills/hooks load.

**32. LSP server primitive** — `tests/test_lsp.py`. **Manual:** ship a plugin with `.lsp.json` defining a Python LSP; verify `lsp_query` returns hover info on a real Python file.

**33. Monitor primitive** — `tests/test_monitors.py`. **Manual:** define a `.monitors.json` with a Python server; verify it auto-starts at session boot and the supervisor restarts it on crash.

**34. Theme primitive** — **not shipped.** No test, no manual check; documented gap.

**35. Plugin install scopes (4)** — `tests/test_plugin_managed_scope.py`. **Manual:** install plugins at each tier (user / project / local / managed); verify the managed-tier one can't be disabled.

**36. Plugin discovery namespaces** — `tests/test_plugins_loader.py`. **Manual:** put a plugin in `.ember/plugins/` and one in `.claude/plugins/`; verify both load.

**37. Plugin agent restrictions** — `tests/test_plugin_agent_restrictions.py`. **Manual:** ship a plugin agent with `mcpServers: {...}` frontmatter; verify a WARN is logged and the mcp_servers are stripped.

---

## MCP (row 38)

**38. MCP server integration** — `tests/test_mcp_*.py` (5 files). **Manual:** add a server to `.mcp.json`; verify the agent can call its tools.

---

## Session & search (rows 40–44, 61)

**40. Session storage** — `tests/test_db_engine.py` (20 cases). **Manual:** create a session; verify `<project>/.ember/state.db` exists; restart; verify session resumes.

**41. Session forking** — `tests/test_session_fork.py`. **Manual:** `/fork mybranch` → new 8-char ID → switch to it and confirm history mirrored.

**42. In-session chat search** — `tests/test_search_chat.py` (23 BE) + `ChatSearchBar.test.ts` (24 FE). **Manual:** `Cmd+F` in chat; type a phrase; click a result; verify the chat scrolls to that turn.

**43. Semantic code index** — `tests/test_code_index*.py`. **Manual:** `/codeindex` → confirm it's syncing; ask agent to find code via `codeindex_query`.

**44. Host-side trigram code search** — `clients/web/src/lib/host.test.ts` (45 cases). **Manual:** in VSCode webview, type a 5+ char string in the composer; verify the search-code response uses the host bridge (faster than the WS fallback).

**61. MEMORY.md write-back** — `tests/test_context.py`. **Manual:** tell the agent something about you ("I'm a Python dev"); confirm next session it remembers (memory file written under `~/.ember/projects/<slug>/memory/`).

---

## Loop / schedule / knowledge / orchestration (rows 45–49)

**45. `/schedule`** — `tests/test_schedule_tools.py`, `test_scheduler.py`. **Manual:** `/schedule daily review at 9am`; verify the task appears in `/schedule status`.

**46. `/loop`** — `tests/test_loop.py`. **Manual:** `/loop process all .py files`; verify the agent iterates with progress.

**47. Team orchestration** — `tests/test_orchestrate.py`. **Manual:** define a team in `.ember/agents/`; ask the agent to spawn the team; verify each specialist runs.

**48. Knowledge base** — `tests/test_knowledge_*.py`, `test_knowledge_ingest_helpers.py` (21 cases). **Manual:** `/knowledge add <url>`; `/knowledge search <query>`; verify ingest and retrieval work.

**49. Memory manager + learning** — `tests/test_memory_*.py`, `test_learning.py`. **Manual:** complete a multi-turn task; check `~/.ember/state.db` `ember_memories` table — entries should be there.

---

## Plan mode + output styles (rows 50, 52)

**50. Plan mode** — `tests/test_plan_mode.py` (24 BE) + `model.test.ts` plan helpers (18) + `PlanCard.test.tsx` (11). **Manual:**
1. Ask agent something complex; verify agent enters plan mode and the PLAN MODE chip appears.
2. Verify `exit_plan_mode(plan, tasks=[...])` produces the PlanCard with checklist.
3. Click Approve → mode flips back to default, agent executes.
4. Click Refine → mode stays, agent iterates.
5. With `/plan` toggled, verify file_write tools are blocked.

**52. Output styles** — `tests/test_output_styles.py`. **Manual:** `/output-style explanatory`; verify next turn is verbose + tutorial-style. `/output-style default` to revert.

---

## Surfaces (rows 53–58)

**53. CLI/TUI** — `tests/test_tui_*.py`. **Manual:** `ember-code` in terminal; verify TUI renders, slash commands work, tools execute.

**54. Tauri desktop** — `clients/tauri/src-tauri/src/lib.rs` (23 cargo tests). **Manual:** `cargo tauri dev` → window opens → chat works → traffic lights positioned right → ⌘F search works.

**55. Web bundle** — Playwright e2e at `clients/web/e2e/`. **Manual:** `npm run dev` → open `localhost:5179?ws=...` → exercise UI in plain Chrome.

**56. VSCode webview** — `clients/vscode/`. **Manual:** open VSCode extension; verify webview loads the same bundle.

**57. JetBrains JCEF** — `clients/jetbrains/`. **Manual:** open the JetBrains tool window; verify it loads.

**58. Shared FE bundle** — all four surfaces above use `clients/web/dist`. **Manual:** any change to `clients/web/src/**` must surface across all 4 (run each).

---

## Tauri-specific UX (rows 59, 60)

**59. External link routing** — `clients/tauri/src-tauri/src/lib.rs` (8 INIT_SCRIPT contract tests). **Manual:** click a link in a markdown response → opens in default browser, NOT in-app webview. Click the Welcome page "Sign in" link → same.

**60. Chat list virtualization** — no unit tests (UI-only). **Manual:** load a 500+ turn session; scroll; verify smooth (Virtuoso should virtualize off-screen items).

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
