# Claude Code ↔ ember-code parity table

Feature-by-feature comparison generated 2026-06-25 from a deep-research workflow
(22 sources, 25 claims adversarially verified) plus codebase cross-checks.
Status legend:

- ✅ parity
- ⚠️ partial / different design
- ❌ ember-code gap
- ➕ ember-code-only or broader

| #   | Feature                                                                          | Claude Code                                                                                                                                                          | ember-code                                                                                                                                                                                                                                                                                                          | Status                |
| --- | -------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------- |
| 1   | Hook event catalog                                                               | 30 events (lifecycle/turn/tool/permission/compaction/workspace/elicitation)                                                                                          | **18 events** (10 original + 6 above + `PermissionRequest`/`PermissionDenied` — landed 2026-06-25)                                                                                                                                                                                                                | ⚠️ partial (8 of 20 missing landed) |
| 2   | Hook handler types                                                               | 5 (`command`, `http`, `mcp_tool`, `prompt`, `agent`)                                                                                                                 | **4 of 5 landed 2026-06-25**: `command`, `http`, `prompt`, `mcp_tool`. `agent` deferred (agent-triggers-agent feedback-loop risk).                                                                                                                                                                                  | ⚠️ near parity        |
| 3   | Hook execution modes                                                             | 3 (sync, `async`, `asyncRewake`)                                                                                                                                     | **3 of 3 landed 2026-06-25**: sync (default), `background: True` (≡ `async`), `async_rewake: True` (queues stderr/stdout on exit-2 → drained as `<system-reminder>` next turn). Loader accepts both `asyncRewake` and `async_rewake` JSON keys.                                                                  | ✅ parity             |
| 4   | Hook matcher syntax                                                              | exact / pipe-list / JS regex                                                                                                                                         | **3-mode landed 2026-06-25**: empty or `*` → all; `Edit` / `Edit\|Write` → exact / pipe-list-exact (CC-compatible — no more substring surprises); anything else → regex search                                                                                                                                       | ✅ parity             |
| 5   | Hook exit-code/JSON contract                                                     | exit 2 = block, exit 0 = JSON, others = non-blocking                                                                                                                 | **already implemented** (`executor.py:118-138`)                                                                                                                                                                                                                                                                     | ✅ parity             |
| 6   | `permissionDecision` envelope                                                    | `allow` / `deny` / `ask` / `defer` for `PreToolUse`                                                                                                                  | **landed 2026-06-25**: `HookResult.permission_decision`; executor parses `hookSpecificOutput.permissionDecision` (or top-level fallback) from stdout JSON; tool-hook reorders so PreToolUse fires first; multi-hook merge precedence: deny > ask > allow > defer. Legacy `should_continue=False` still blocks.    | ✅ parity (sans canUseTool bridge) |
| 7   | Permission modes                                                                 | 6 (`default`, `dontAsk`, `acceptEdits`, `bypassPermissions`, `plan`, `auto`[TS])                                                                                     | **5 modes landed 2026-06-25** (skip TS-only `auto`): `default`, `dontAsk`, `acceptEdits`, `bypassPermissions`, `plan` via `PermissionEvaluator`                                                                                                                                                                    | ✅ parity (sans `auto`) |
| 8   | Permission evaluation pipeline                                                   | 6 steps (hooks → deny → ask → mode → allow → `canUseTool`)                                                                                                           | **6 steps landed 2026-06-25** in `PermissionEvaluator.evaluate` (hooks fire at the tool-hook layer; deny → ask → mode → allow → defer in the evaluator)                                                                                                                                                            | ✅ parity (sans canUseTool bridge) |
| 9   | Bypass-resistant scoped deny                                                     | `Bash(rm *)` blocks even in `bypassPermissions`                                                                                                                      | **landed 2026-06-25** — `deny` rules evaluate before `mode` step, so `Bash(rm *)` survives bypass + acceptEdits + plan                                                                                                                                                                                              | ✅ parity             |
| 10  | Settings precedence tiers                                                        | 5 (managed > CLI > local > project > user)                                                                                                                           | **5 (managed > CLI > local > project > user)** — landed 2026-06-25. `_platform_managed_settings_path` maps to `/Library/Application Support/Ember/managed-settings.yaml` (darwin), `/etc/ember/managed-settings.yaml` (linux), `%PROGRAMDATA%\Ember\managed-settings.yaml` (win32). YAML or JSON content. Wins over CLI. | ✅ parity             |
| 11  | Managed-policy CLAUDE.md                                                         | `/Library/Application Support/ClaudeCode/CLAUDE.md` etc.                                                                                                             | **landed 2026-06-25** — `_platform_managed_rules_dir()` maps to `/Library/Application Support/Ember/` (darwin) / `/etc/ember/` (linux) / `%PROGRAMDATA%\Ember\` (win32). Reads `ember.md` + `CLAUDE.md` (when `cross_tool_support`). Prepended as section 0 under "# Managed Policy" header. `@` imports scoped to managed dir. | ✅ parity             |
| 12  | CLAUDE.md / ember.md root + subdir hierarchy                                     | walk-up + lazy subdir load on file touch                                                                                                                             | `RulesIndex` (parity)                                                                                                                                                                                                                                                                                               | ✅ parity             |
| 13  | `.local.md` override siblings                                                    | `CLAUDE.local.md` after `CLAUDE.md`                                                                                                                                  | `ember.local.md` + `CLAUDE.local.md`                                                                                                                                                                                                                                                                                | ✅ parity             |
| 14  | `@<path>.md` import depth                                                        | up to 4 hops                                                                                                                                                         | **4 hops** — bumped from 3 → 4 on 2026-06-25 (`_IMPORT_MAX_DEPTH` in `context.py`); test `test_depth_capped` now asserts 4 layers resolve and the 5th is left literal.                                                                                                                                                | ✅ parity             |
| 15  | `@import` skips code spans/fences                                                | yes (docs explicit)                                                                                                                                                  | **landed 2026-06-25** — fenced blocks (```` ``` ````/``~~~`` with optional info string + up-to-3-space indent) and inline backtick spans are masked with NUL sentinels before the `@` substitution pass, then restored. Rules files can document `@<path>.md` syntax inside backticks without triggering accidental imports. | ✅ parity             |
| 16  | Path-scoped rules (`paths:` frontmatter)                                         | `.claude/rules/*.md`; activates when files matching glob are touched                                                                                                  | `.ember/rules/*.md` + `.claude/rules/*.md` (dual namespace); activates on tool touch via `RulesIndex._match_scoped_rules` (CC parity for the on-touch semantics); `@import` substitution in scoped-rule bodies inherits row-15 code-region masking. Verified 2026-06-25 with cross-feature tests in `test_rules_index.py`. | ➕ ember broader      |
| 17  | Cross-tool rules reading (`.claude/*` from ember)                                | n/a (single namespace)                                                                                                                                               | `cross_tool_support` setting toggles                                                                                                                                                                                                                                                                                | ➕ ember-only         |
| 18  | Auto-memory MEMORY.md index (200 lines / 25 KB prefix)                           | v2.1.59+ at `~/.claude/projects/<proj>/memory/`                                                                                                                      | **landed 2026-06-25** — ember-native at `~/.ember/projects/<slug>/memory/MEMORY.md` (slug = CC convention: absolute path with `/` → `-`). Same 200-line / 25-KB prefix budget. Cross-tool fallback reads `~/.claude/projects/<slug>/memory/MEMORY.md` when `cross_tool_support` is on, so a CC user gets their existing memory bank without migration. Loaded as section 0.5 of `load_project_context` (between Managed Policy and User Rules). | ✅ parity             |
| 19  | Markdown-authored custom commands                                                | `.claude/commands/*.md` + `.claude/skills/<n>/SKILL.md` with frontmatter, `$ARGUMENTS`, `` !`cmd` ``, `@path`                                                         | **landed 2026-06-25** — `markdown_commands.py` discovers `.md` files at `~/.claude/commands/`, `~/.ember/commands/`, `<project>/.claude/commands/`, `<project>/.ember/commands/` (project beats user, ember beats claude). Substitutes `$ARGUMENTS`, runs `` !`cmd` `` via shell (30s timeout, concurrent fan-out), inlines `@path`. Frontmatter: `description`, `allowed-tools`, `argument-hint`, `model`. Routed through `CommandHandler.handle` between built-ins and skills — built-ins win, markdown overrides skills with the same name. Project commands can't `@import` paths outside `project_dir`.  | ✅ parity             |
| 20  | `slash_commands` exposed in system/init                                          | yes (SDK)                                                                                                                                                            | **landed 2026-06-25** — `RpcMethod.GET_SLASH_COMMANDS` returns `[{name, description, source, argument_hint}, ...]` over the same RPC channel as `get_skill_details`. Three sources merged: `builtin` (from `CommandHandler._COMMANDS`), `markdown` (via `discover_markdown_commands`, gated by `cross_tool_support`), `skill` (user-invocable from `skill_pool`). Skill / markdown failures degrade gracefully — builtins always return. | ✅ parity             |
| 21  | Built-in slash command catalog                                                   | `/help`, `/clear`, `/compact`, `/context`, `/usage`                                                                                                                  | broader: also `/fork`, `/loop`, `/schedule`, `/knowledge`, `/codeindex`, `/agents`, `/skills`, `/plugins`, `/hooks`, `/memory`, `/mcp`, `/evals`, `/sync-knowledge`, `/bug`, `/ctx`, `/sessions`, `/rename`, `/whoami`, `/model`, `/login`, `/logout`, `/config`, `/plugin marketplace`, `/quit`, `/exit`              | ➕ ember broader      |
| 22  | File-op tools (read / write / edit / glob / grep / list-dir)                     | `Read`, `Write`, `Edit`, `Glob`, `Grep`                                                                                                                              | **Main team: shell-first.** Exposes `Bash` + `Write` + `Edit` (+ `Schedule`, `NotebookEdit`) only. `Read` / `Grep` / `Glob` toolkits exist in the registry but are NOT registered to the main team — file reads / searches go through `Bash` (`cat`, `rg`, `find`). Design choice made in v0.4.0 (commit `7e50705`) — overlap with shell confused the model. Registry resolution covered in `tests/test_tool_functions.py`; main-team tool list pinned at `core/session/core.py:1051`. Sub-agents CAN opt into `Read`/`Grep`/`Glob` via their frontmatter `tools:` allowlist. **Implication for hooks**: matchers targeting `read_file` / `Read` are dead-code on the main team — use `run_shell_command` instead. | ⚠️ partial (Read/Grep/Glob omitted by design) |
| 23  | Shell tool                                                                       | `Bash`                                                                                                                                                               | `run_shell_command` via `/bin/sh -c`                                                                                                                                                                                                                                                                                | ✅ parity             |
| 24  | Web fetch / search tools                                                         | `WebFetch`, `WebSearch`                                                                                                                                              | likely via Agno but not named in catalog                                                                                                                                                                                                                                                                            | ⚠️ verify             |
| 25  | `TodoWrite` planning tool                                                        | yes                                                                                                                                                                  | **landed 2026-06-25** — `TodoTools` registers `todo_write(todos)` on every session. Each call REPLACES the list atomically (CC semantics). Items: `{content, status, activeForm}` (statuses: `pending` / `in_progress` / `completed`). Validation drops malformed rows but surfaces errors in the reply string. Snapshot exposed via `RpcMethod.GET_TODOS`. Reply summary warns when >1 item is `in_progress`. | ✅ parity             |
| 26  | `Task` sub-agent dispatch tool                                                   | yes (fire-and-forget, isolated context)                                                                                                                              | "team" orchestration (structured)                                                                                                                                                                                                                                                                                   | ⚠️ different design   |
| 27  | `SlashCommand` re-entrant tool                                                   | agent can invoke another slash command                                                                                                                               | **landed 2026-06-25** — `SlashCommandTool` registers `slash_command(command)` on every session. Dispatches through `CommandHandler.handle` (NOT the session-level `dispatch` wrapper, so `CommandAction.QUIT` / `CLEAR` can't SystemExit). Blocked set: `/quit`, `/exit`, `/clear`, `/login`, `/logout`, `/model` — refused with an explanatory error before dispatch. Everything else (info commands, codeindex/knowledge searches, compact, schedule, loop, markdown commands, skills) returns the command's text output to the agent. | ✅ parity             |
| 28  | Background process tracking                                                      | via `asyncRewake` hooks + monitors plugin primitive                                                                                                                  | `_registry` with TTL eviction, `read_process_output`, `stop_process`                                                                                                                                                                                                                                                | ✅ parity             |
| 29  | Sub-agent allow-listed tools (frontmatter)                                       | `tools:` per-agent                                                                                                                                                   | mostly model-level allowlists                                                                                                                                                                                                                                                                                       | ⚠️ verify per-agent   |
| 30  | Sub-agent `isolation: "worktree"`                                                | yes (fresh git worktree per agent)                                                                                                                                   | **landed 2026-06-25** — `spawn_agent(task, agent_name, isolation="worktree")` creates a fresh `WorktreeManager`-backed worktree, shallow-copies the spawned agent's toolkits and rebases each one's `base_dir` to the worktree (pool's shared instances stay untouched), prepends a working-directory note to the task, then cleans up (clean) or preserves (dirty) with a footer reporting `git merge` / `git worktree remove` commands. Tools without a `base_dir` attribute still see the project root (documented caveat). | ✅ parity             |
| 31  | Plugin primitives (skills/agents/hooks)                                          | yes                                                                                                                                                                  | yes                                                                                                                                                                                                                                                                                                                 | ✅ parity             |
| 32  | Plugin LSP-server primitive                                                      | yes                                                                                                                                                                  | **landed 2026-06-25** — new `core/lsp/` module (config + client + manager) + `core/tools/lsp.py` agent toolkit. Plugin manifest `has_lsp` flag, `.lsp.json` discovery (`~/.ember/lsp.json` < `<project>/.lsp.json` < plugin-bundled `.lsp.json` with `<plugin>:<server>` namespacing). Minimal LSP client speaks JSON-RPC over stdio with Content-Length framing — initialize / initialized handshake, request / notify, graceful shutdown. Manager lazy-launches per server; concurrent first-queries share one launch via per-server `asyncio.Lock`. Agent tool: `lsp_query(server, method, params)` + `lsp_list_servers()`. | ✅ parity             |
| 33  | Plugin monitor (background process) primitive                                    | yes (experimental)                                                                                                                                                   | **landed 2026-06-25** — new `core/monitors/` package + `core/tools/monitors.py` toolkit. `.monitors.json` discovery (user/project/plugin tiers with `<plugin>:<name>` namespacing), `MonitorManager` with eager start, per-monitor stdout-drain task feeding a 1000-line rolling buffer, supervisor task auto-restarts crashed monitors with bounded exponential backoff (`1s/2s/5s/15s` then `failed`), `stop`/`restart` clear the supervisor, `shutdown_all` is idempotent. Restart policies: `never`/`on_crash`/`always`. Agent tools: `monitor_status` / `monitor_output` / `monitor_restart` / `monitor_stop`. | ✅ parity             |
| 34  | Plugin theme primitive                                                           | yes                                                                                                                                                                  | no                                                                                                                                                                                                                                                                                                                  | ❌ ember gap          |
| 35  | Plugin install scopes                                                            | 4 (user / project / local / managed)                                                                                                                                 | **4 (user / project / local / managed)** — managed tier landed 2026-06-25. `_platform_managed_plugins_root()` returns the same OS-protected directory as `managed-settings.yaml` (darwin `/Library/Application Support/Ember/`, linux `/etc/ember/`, win32 `%PROGRAMDATA%\Ember\`). Loader scans `<managed>/.claude/plugins/` (priority 5) and `<managed>/.ember/plugins/` (priority 6) — managed beats project on same-name collision. `PluginDefinition.is_managed` flag surfaces to the panel; `set_plugin_enabled(..., enabled=False)` refuses with an explanatory error for managed plugins; `_disabled_plugins` strips managed names so a persisted disable list can't override org policy. | ✅ parity             |
| 36  | Plugin discovery namespaces                                                      | `.claude/plugins/` only                                                                                                                                              | `.claude/plugins/` + `.ember/plugins/` (dual)                                                                                                                                                                                                                                                                       | ➕ ember broader      |
| 37  | Plugin agent restrictions (no hooks/mcpServers/permissionMode; isolation=worktree) | yes — security-hardened                                                                                                                                              | **landed 2026-06-25** — `_apply_plugin_restrictions` strips `mcp_servers` from plugin-loaded `AgentDefinition`s, sets `force_isolation="worktree"`, and WARNs on detected restricted frontmatter keys (`hooks`, `mcpServers`, `permissionMode`, etc.) for audit trails. `AgentPool._load_directory(plugin_restricted=True)` activates the envelope; `PluginLoader.apply_to_agents` passes the flag. `spawn_agent` reads `defn.force_isolation` (string-only, isinstance-guarded against mock leakage) and overrides the caller-supplied `isolation` arg — plugin agents always get a fresh worktree. | ✅ parity             |
| 38  | MCP server integration                                                           | first-class (`.mcp.json` project, `~/.claude.json` user)                                                                                                              | via Agno bindings                                                                                                                                                                                                                                                                                                   | ✅ parity             |
| 39  | `mcp_tool` hook handler                                                          | yes (v2.1.118+)                                                                                                                                                      | **landed** — handler type added with row 2 work; full envelope parity confirmed on 2026-06-26. `_run_mcp_tool_hook` now translates dict results through the same envelope parser as command-hook stdout JSON (`continue`, `systemMessage`, `hookSpecificOutput.permissionDecision`, bare `permissionDecision`), so an MCP-server-authored hook can `allow`/`deny`/`ask` tool calls without learning a different schema. Non-dict returns (str/None) still surface as plain `message` for back-compat. | ✅ parity             |
| 40  | Session storage                                                                  | scattered (`~/.claude.json` + per-file)                                                                                                                              | one `<project>/.ember/state.db` (AsyncSqliteDb)                                                                                                                                                                                                                                                                     | ➕ ember cleaner      |
| 41  | Session forking (clone history under new id)                                     | none documented                                                                                                                                                      | `/fork [name]`                                                                                                                                                                                                                                                                                                      | ➕ ember-only         |
| 42  | In-session conversation search                                                   | none documented                                                                                                                                                      | SQLite-backed `search_chat` RPC + UI bar — BE tests: `tests/test_search_chat.py` (23 cases — empty/no-match, non-string content skip, snippet windowing with ellipsis, case-insensitive find, match-offset bookkeeping, limit cap, default-50 limit, dispatch-table wiring). FE tests: `clients/web/src/components/ChatSearchBar.test.ts` (24 cases — `formatTurnTime`: empty/just-now/m/h/d buckets, >7d fallback, clock-skew time-of-day; `cleanSnippetText`: system-context block strip, attached-files multi-line block strip, think/loop-iteration tags, code-paste collapse, @code:id rename, markdown emphasis/inline-code/heading strip, whitespace collapse, leave-plain-text-alone negative check; `translateIndex`: happy path, filtered-out history turns, negative + overflow indices, post-`/clear` shrunken liveItemCount, empty map). | ➕ ember-only         |
| 43  | Semantic code index                                                              | none built-in                                                                                                                                                        | chroma + sync manager                                                                                                                                                                                                                                                                                               | ➕ ember-only         |
| 44  | Host-side trigram code search (when in IDE)                                      | n/a                                                                                                                                                                  | `host.searchCode()` (JetBrains/VSCode) — tests: `clients/web/src/lib/host.test.ts` (16 cases: short-circuit, cefQuery success/failure/malformed-JSON/throw/double-resolve/timeout, VSCode id-correlated reply/mismatched-id/wrong-type/timeout, tauri+web pass-through)                                                                                                                                                                                                                                                                              | ➕ ember-only         |
| 45  | `/schedule` (cron + one-shot)                                                    | none                                                                                                                                                                 | `core/tools/schedule.py` + `scheduler_tasks` table                                                                                                                                                                                                                                                                  | ➕ ember-only         |
| 46  | `/loop` primitive                                                                | n/a — agent decides                                                                                                                                                  | yes                                                                                                                                                                                                                                                                                                                 | ➕ ember-only         |
| 47  | Multi-agent "team" orchestration                                                 | flat `Task` dispatch                                                                                                                                                 | named-specialist team in pool                                                                                                                                                                                                                                                                                       | ➕ ember structured   |
| 48  | Knowledge base (`/knowledge`)                                                    | none                                                                                                                                                                 | yes                                                                                                                                                                                                                                                                                                                 | ➕ ember-only         |
| 49  | Memory manager + learning machine                                                | auto-memory model writes                                                                                                                                             | Agno memories + learning extraction                                                                                                                                                                                                                                                                                 | ⚠️ overlap            |
| 50  | Plan mode                                                                        | yes (no source edits)                                                                                                                                                | **landed 2026-06-26** — distinct from Agno's `TeamMode.tasks` (which is multi-agent planning, no permission sandbox). Enforcement existed since row 7 (`PermissionMode.PLAN` blocks `FILE_EDIT_TOOLS`); today added the workflow: `/plan` slash command toggles via `Session.set_permission_mode` (mutates the live evaluator's `mode` — takes effect on the next tool call), `PlanTool.exit_plan_mode(plan)` lets the agent submit a plan but does NOT flip the mode (user-controlled exit, mirrors CC), `PlanStore` keeps the latest + history of submitted plans, `GET_LATEST_PLAN` RPC exposes them to the UI. **FE plan-card tests added 2026-06-28**: `clients/web/src/chat/model.test.ts` — 18 cases (5 for `normalizePlanTask` covering empty content, non-object input, trim, status forward-compat, missing activeForm; 3 for bulk `normalizePlanTasks`; 10 for `mergePlanTasks` covering: empty existing fast-path, no-match fast-path, status update, activeForm preservation when empty, activeForm override when non-empty, last-known-status preservation when todos drop a task, NEW tasks from todos NOT grafted into the plan, unparseable todos noop). The merge/normalize logic was extracted out of `App.tsx`'s `plan_submitted`/`todos_updated` channel handlers into pure helpers so the contract is testable without DOM.   | ✅ parity             |
| 51  | `acceptEdits` mode                                                               | yes (auto-approve edits)                                                                                                                                             | **landed 2026-06-26** — enforcement existed since row 7 (`PermissionMode.ACCEPT_EDITS` auto-allows `FILE_EDIT_TOOLS`); today added the runtime workflow: `/accept` slash command toggles via `Session.set_permission_mode` (parallel to `/plan`), `ModeBadge` in the status line shows a green chip "ACCEPT EDITS" when active (generalises the prior plan-only badge to handle all four non-default modes with distinct colors). Scoped denies still hold (row 9 invariant). The agent intentionally does NOT get a tool to flip into acceptEdits — only the user can opt in (loosening the sandbox is a user decision). | ✅ parity             |
| 52  | Output styles                                                                    | yes                                                                                                                                                                  | **landed 2026-06-26** — new `core/output_styles/` module mirrors the markdown-commands shape. Discovers `.md` files at `~/.ember/output-styles/`, `<project>/.ember/output-styles/`, plus `.claude/` equivalents (gated by `cross_tool_support`) and plugin-bundled `output-styles/`. Each file's body is appended to `instructions` as `# Output style: <name>\n\n<body>`. `/output-style` slash command lists / sets / shows the active style; `Session.set_output_style` hot-patches the live team's `instructions` list (strips the existing block, appends the new one — next turn picks up the new tone without rebuilding). Ships three built-ins in `.ember/output-styles/`: `default` (concise), `explanatory` (verbose + educational), `learning` (interactive teacher). `GET_OUTPUT_STYLES` RPC + `output_style_changed` broadcast wire the FE. | ✅ parity             |
| 53  | Surfaces — CLI/TUI                                                               | CLI native; TUI hosted by IDE                                                                                                                                        | Textual TUI                                                                                                                                                                                                                                                                                                         | ✅ parity             |
| 54  | Surfaces — desktop                                                               | Anthropic desktop app                                                                                                                                                | Tauri shell                                                                                                                                                                                                                                                                                                         | ✅ parity             |
| 55  | Surfaces — web                                                                   | claude.ai/code                                                                                                                                                       | Vite/React bundle (works in any browser)                                                                                                                                                                                                                                                                            | ✅ parity             |
| 56  | Surfaces — VS Code                                                               | thin extension wrapping CLI subprocess                                                                                                                               | webview hosting same Vite bundle                                                                                                                                                                                                                                                                                    | ➕ ember richer UI    |
| 57  | Surfaces — JetBrains                                                             | thin plugin wrapping CLI                                                                                                                                             | JCEF panel hosting same Vite bundle                                                                                                                                                                                                                                                                                 | ➕ ember richer UI    |
| 58  | Shared FE bundle across all surfaces                                             | n/a (terminal hosted)                                                                                                                                                | yes (Tauri / browser / VSCode / JetBrains all load `clients/web/dist`)                                                                                                                                                                                                                                              | ➕ ember-only         |
| 59  | External link → OS browser routing                                               | n/a                                                                                                                                                                  | INIT_SCRIPT click interceptor → `plugin:opener\|open_url` — tests: `clients/tauri/src-tauri/src/lib.rs` (8 cases pinning the INIT_SCRIPT contract — command name `plugin:opener\|open_url`, capture-phase listener, http/https/mailto/tel allowlist (with negative checks blocking ftp/file/data), `window.open` shim, `__EMBER_HOST__.openUrl` bridge, defaultPrevented + left-button-only guards, composedPath walk for nested anchors) + capability assertion that `opener:allow-open-url` is in `capabilities/default.json` for the `main` window. Runtime click→opener behavior is WKWebView-only; verified manually (session 2026-06-28) — Playwright e2e runs in Chromium where the INIT_SCRIPT is never injected.                                                                                                                                                                                                                                                           | ➕ ember-only         |
| 60  | Chat list virtualization (Virtuoso)                                              | n/a                                                                                                                                                                  | `react-virtuoso`, memoized `ChatItemView`                                                                                                                                                                                                                                                                           | ➕ ember-only         |
| 61  | `MEMORY.md` automatic context write-back                                         | model writes via auto-memory subsystem                                                                                                                               | **landed 2026-06-26** — row 18 added the READ side (`MEMORY.md` index loaded into the system prompt); row 61 adds the WRITE side via convention (mirrors CC's design — no special "save memory" tool, just docs). New `memory_writeback_instructions(project_dir)` builds the system-prompt block: names the four memory types (user/feedback/project/reference), gives concrete WHEN-to-save triggers, lists what NOT to save (code patterns, paths, git history — anything derivable from the codebase), spells out the file frontmatter shape + MEMORY.md index update. Appended to the agent's instructions in `_build_main_team`. `ensure_memory_dir(project_dir)` is called at session bootstrap so the agent's first `save_file` into the memory area doesn't fail on a "parent doesn't exist" error. | ✅ parity             |

## Tallies

| Status                  | Count |
| ----------------------- | ----- |
| ✅ Parity               | **33**    |
| ⚠️ Partial / different  | 5     |
| ❌ ember-code gap       | **1**     |
| ➕ ember-only / broader | 17    |
| ⚠️ Verify               | 2     |
| ✅ Already implemented (despite report claiming otherwise) | 1 (row 5) |

**Updates since first snapshot:**
- 2026-06-25: row 1 moved ❌ → ⚠️ — added `PreCompact`, `PostCompact`, `InstructionsLoaded`,
  `TaskCreated`, `TaskCompleted`, `StopFailure`, `PermissionRequest`, `PermissionDenied`
  (18 of 30 events, 12 still missing).
- 2026-06-25: rows 7/8/9 moved ❌ → ✅ — landed the 5-mode permission system,
  the 6-step evaluation pipeline, and bypass-resistant scoped denies. The
  `canUseTool` bridge isn't wired yet; `ASK` decisions currently fire a
  `PermissionRequest` hook and treat-as-deny for safety.
- 2026-06-25: row 2 moved from "1 of 5 / partial" to "4 of 5 / near parity"
  — added `prompt` (static text → systemMessage) and `mcp_tool` (call an
  MCP server tool with the hook payload). `agent` handler deferred.
- 2026-06-25: row 3 moved ⚠️ → ✅ — added `asyncRewake` execution mode.
  Background hooks that exit with code 2 queue a system reminder
  (stderr/stdout, or `systemMessage` JSON if present) which drains
  on the next `handle_message` turn as a `<system-reminder>` block.
- 2026-06-25: row 4 moved ⚠️ → ✅ — tri-mode matcher (empty/`*` → all;
  alphanumeric ± pipes → exact; else regex). Headline behavior change:
  `matcher: "Edit"` now matches ONLY the tool named `Edit`, not
  `Edited` / `edit_file` / `MultiEdit`. Existing tests audited; one
  in `test_tool_hook.py` adjusted to use the proper exact name.
- 2026-06-25: row 6 moved ❌ → ✅ — added `permissionDecision` envelope
  on PreToolUse. Pipeline reordered so hooks fire first (CC parity).
  Headline safety invariant: `allow` skips the evaluator but NOT the
  hard-coded `protected_paths` / `blocked_commands` lists — those
  remain bypass-resistant even with a malicious plugin.
- 2026-06-25: CLI permission flags (`--read-only`, `--accept-edits`,
  `--auto-approve`, `--strict`) are now wired end-to-end. Each flag
  sets `permissions.mode` (read by `PermissionEvaluator`) alongside
  the legacy per-category fields. Flag order is permissive → strict
  so the strictest passed flag wins. Verified with
  `tests/test_cli_permission_wiring.py` (13 tests, all pass).
- 2026-06-25: row 10 moved ❌ → ✅ — added the managed-policy
  settings tier (5-of-5 CC precedence). Sysadmin file at
  `/Library/Application Support/Ember/managed-settings.yaml`
  (darwin) / `/etc/ember/managed-settings.yaml` (linux) /
  `%PROGRAMDATA%\Ember\managed-settings.yaml` (win32). Wins over
  CLI by design — the whole point is a user can't
  `--auto-approve` their way out of an org policy. JSON content
  parses too (YAML is a superset). Tests live in
  `TestPlatformManagedSettingsPath` + `TestManagedSettings` in
  `tests/test_settings.py` (11 new tests, all pass).
- 2026-06-25: row 11 moved ❌ → ✅ — added the managed-policy
  CLAUDE.md tier. Reads `ember.md` and `CLAUDE.md` from the
  platform's managed dir (sibling to `managed-settings.yaml`).
  Section is prepended FIRST in `load_project_context` under a
  `# Managed Policy` header so the model encounters org-pinned
  guidance ahead of user/project rules. `@<path>.md` imports
  inside the managed file are scoped to the managed dir, so a
  policy can't reach into `/etc/passwd` or the user's project.
  Tests live in `TestPlatformManagedRulesDir`,
  `TestLoadManagedRules`, and `TestManagedPolicyInContextOutput`
  in `tests/test_context.py` (13 new tests). Includes an
  invariant test that the rules dir IS the same parent as the
  managed-settings file.
- 2026-06-25: row 14 moved ⚠️ → ✅ — bumped `_IMPORT_MAX_DEPTH`
  from 3 → 4 to match Claude Code's documented limit. Existing
  depth-cap test extended to assert 4 hops resolve and the 5th
  is left as a literal token.
- 2026-06-25: row 15 moved ❌ → ✅ — `@<path>.md` substitution
  now skips code regions. Added `_mask_code_regions` /
  `_unmask_code_regions` helpers that stash fenced blocks
  (```` ``` ````/``~~~`` with optional info string and up to 3
  spaces of indent) and inline backtick spans behind
  `\0CODE<idx>\0` sentinels before the `@` regex runs. Headline
  behavior: rules files can now document the import syntax
  inside backticks (e.g. `` `@./other.md` `` in prose explaining
  how imports work) without inadvertently triggering one.
  Recursive nesting handled per-level (inner imports' own code
  regions mask/restore independently). Covered by 7 new tests
  in `TestAtImports`.
- 2026-06-25: row 16 audited — already broader than CC and the
  on-touch activation semantics already match. Sharpened the
  parity row description: ember-code activates path-scoped
  rules when the tool actually touches a matching file (via
  `RulesIndex._match_scoped_rules`), not at session start by
  working-dir, and the row-15 code-region masking inherited by
  scoped-rule `@import` bodies has been verified end-to-end.
  Added two integration tests in `tests/test_rules_index.py`:
  `test_path_scoped_rule_body_skips_code_region_imports` and
  `test_dual_namespace_independent_rules_both_fire`.
- 2026-06-25: row 18 moved ❌ → ✅ — added the per-project
  `MEMORY.md` index loader. Lives at
  `~/.ember/projects/<slug>/memory/MEMORY.md`, slug encoded as
  CC does (absolute path with `/` → `-`) so the cross-tool
  fallback can find an existing CC memory bank for the same
  project without any migration step. Enforces both Claude
  Code's caps: first 200 lines OR 25 KB, whichever hits first,
  with the byte cap dropping trailing partial UTF-8 codepoints
  cleanly. Loaded into `load_project_context` as section 0.5,
  between Managed Policy and User Rules — so the agent reads
  sysadmin directives first, then its own remembered context,
  then the layered rules. 13 new tests in
  `TestProjectMemorySlug`, `TestLoadMemoryIndex`, and
  `TestMemoryIndexInContextOutput`.
- 2026-06-26: row 61 moved ❌ → ✅ — added auto-memory
  write-back. Row 18 had landed the READ side (memory index
  loaded into the system prompt at session start); row 61
  closes the WRITE side. Mirrors CC's design exactly: no
  special "save memory" tool, just a system-prompt block
  teaching the agent the convention. New
  `memory_writeback_instructions(project_dir)` in
  `core/utils/context.py` returns the block; it names the
  four memory types (`user` / `feedback` / `project` /
  `reference`) with concrete examples of each, lists explicit
  triggers for when to save (user shares a fact / corrects
  approach / confirms a non-obvious choice), and — crucially
  — lists categories the agent must NEVER save (code
  patterns, paths, git history, anything derivable from the
  codebase itself), so the memory bank doesn't fill with
  noise that's already in the source. Frontmatter shape
  (`name` / `description` / `metadata.type`) and the
  two-step save (memory file + index line in `MEMORY.md`)
  are spelled out so the model produces consistent entries.
  Block is appended to `instructions` in `_build_main_team`.
  New `ensure_memory_dir(project_dir)` is called at session
  bootstrap so the agent's first `save_file` doesn't fail on
  missing parent; idempotent + OSError-swallowing for
  read-only filesystems. 8 new tests in `test_context.py`:
  directory creation (3) and instruction-block shape (5 —
  path appears, all four types named, NOT-to-save guidance
  present, frontmatter shape included, MEMORY.md index
  mentioned).
- 2026-06-26: row 52 moved ❌ → ✅ — added output styles. New
  `core/output_styles/` package (mirrors markdown-commands
  shape): `OutputStyle` dataclass + `discover_output_styles`
  walks user / project / plugin tiers with last-write-wins
  precedence. Three built-ins ship in
  `.ember/output-styles/`: `default` (concise), `explanatory`
  (verbose + teaches WHY behind each change),
  `learning` (interactive mentor mode with comprehension
  checks). `Session` discovers + holds them; the active
  style's body is appended to the agent's `instructions` as
  a `# Output style: <name>\n\n<body>` block. `/output-style`
  slash command lists / sets / shows the active style with
  the standard subcommand vocabulary (bare = list,
  `set <name>` and bare `<name>` both switch).
  `Session.set_output_style` hot-patches the live team's
  `instructions` (strips the previous style block, appends
  the new one) so mid-session switches take effect on the
  next turn without rebuilding the agent. `GET_OUTPUT_STYLES`
  RPC exposes the catalog + active style to the FE;
  `output_style_changed` broadcast updates a future style
  chip. 24 new tests across discovery, frontmatter, switch,
  hot-patch, slash command, and the RPC dispatch.
- 2026-06-26: row 51 moved ❌ → ✅ — added the runtime
  `acceptEdits` toggle. The enforcement (auto-allow on file-
  edit tools when `mode == ACCEPT_EDITS`) shipped with row 7;
  what was missing was the user-facing workflow. Added
  `/accept` slash command (toggle / on / off / status — same
  shape as `/plan`). Generalised `PlanBadge` → `ModeBadge`
  with mode-specific styling: orange pulse for `plan`,
  GREEN for `acceptEdits`, red variants for `dontAsk` /
  `bypassPermissions` (the latter two are still flag-only
  but the badge handles them when set). `PlanBadge` kept as
  a legacy alias so existing call sites compile. CSS
  variants `mode-badge--accept` / `--dontask` / `--bypass`
  share the same pulse cadence so all modes feel like one
  visual family. Crucially, **no agent tool to enter
  acceptEdits** — it loosens the sandbox, so only the user
  opts in (security envelope: agent can only enter plan,
  which TIGHTENS). Scoped denies (row 9) still apply.
  8 new tests in `TestAcceptSlashCommand`: toggle in,
  toggle out, on, off, status, unknown arg, transition
  from plan, broadcast attribution.
- 2026-06-26: row 50 — agent-initiated plan mode. Added
  `enter_plan_mode(reason)` to `PlanTool`, the asymmetric
  complement of `exit_plan_mode`: the agent can flip ITSELF
  into the read-only sandbox (safe — strictly tighter), but
  cannot exit (user-only via `/plan` or the Approve button).
  System prompt now nudges the model: "For complex
  multi-step tasks (multi-file refactors, architectural
  changes, broad feature additions where the right path
  isn't obvious), call `enter_plan_mode(reason)` BEFORE
  doing any work. Skip this for simple one-shot requests."
  Broadcast carries `source: "agent"` + `reason` so the FE
  injects an inline info banner "Agent entered plan mode —
  <reason>" alongside the existing badge animation. 6 new
  tests in `TestEnterPlanMode`: flips the mode, includes
  reason in reply, agent-attributed broadcast, empty reason
  works, defensive on missing `set_permission_mode`, exit
  doesn't pair with enter (mode stays PLAN until user
  approves). Headline UX: for "refactor the auth system",
  the agent now ENTERS plan mode itself, gathers context,
  submits a plan, and waits — the user no longer has to know
  to type `/plan` first.
- 2026-06-26: row 50 full UI landed — frontend pieces that
  make plan mode actually usable from the web/Tauri client.
  New `PlanBadge` (status-line chip with pulsing dot, only
  visible when `status.permission_mode === "plan"`); new
  `PlanCard` inline chat item rendering the agent's submitted
  plan as markdown plus **Approve & exit plan mode** /
  **Refine** buttons. Approve sends `/plan off` through the
  existing user-message pipeline and marks the card
  `approved` (green footer); Refine flips the card to
  `dismissed` so the user types their feedback as a normal
  message and the agent iterates. BE wiring: `StatusUpdate`
  now carries `permission_mode` so the badge populates from
  the initial status fetch; `Session.broadcast(channel,
  payload)` fans `permission_mode_changed` and
  `plan_submitted` push notifications out to attached
  clients — wired in `backend/__main__.py` via
  `register_broadcast_callback` (loop-safe through
  `call_soon_threadsafe`). `set_permission_mode` now
  broadcasts on every flip; `PlanTool.exit_plan_mode`
  broadcasts after storing the plan. Headline UX win: the
  user no longer has to type `/plan off` — they click
  Approve on the rendered card and the agent's next message
  can execute. FE tests + TS typecheck pass (82 + 0
  errors). BE suite 2503 passing.

- 2026-06-26: row 50 moved ❌ → ✅ — added CC-style plan mode
  (read-only sandbox + user-approval workflow, distinct from
  Agno's `TeamMode.tasks` which is multi-agent task planning
  without a permission sandbox). The enforcement side
  (`PermissionMode.PLAN` denying `FILE_EDIT_TOOLS`) had
  shipped with row 7; today added the missing workflow
  pieces: `Session.set_permission_mode(mode)` mutates the
  live `PermissionEvaluator.mode` (cached on `self` for the
  first time so the tool-event hook sees the update),
  `/plan` slash command (toggle, `on`/`off`/`status`
  variants) drives that flip with a user-facing markdown
  explanation, `PlanTool.exit_plan_mode(plan)` lets the agent
  submit a plan and explicitly steers it to STOP (the reply
  string says "do not continue executing"), `PlanStore`
  keeps the latest plan + bounded history (cap 10), the
  `GET_LATEST_PLAN` RPC exposes both for the UI.
  Headline security property: the agent cannot exit plan mode
  on its own — `exit_plan_mode` does NOT touch the permission
  evaluator (verified by
  `test_exit_plan_mode_does_not_flip_mode`); only `/plan`
  (user-initiated) flips it. 24 new tests in
  `tests/test_plan_mode.py`.
- 2026-06-26: row 39 moved ❌ → ✅ — the `mcp_tool` handler
  type itself was added with the row 2 work earlier this
  session, but its return-value handling was lossy (results
  were always stringified into `message`). Today closed the
  loop by factoring out a `_hook_result_from_envelope` helper
  and routing `mcp_tool` results through it: dict returns now
  honour the same envelope the `command` handler reads from
  stdout JSON (`continue`, `systemMessage`,
  `hookSpecificOutput.permissionDecision`, bare
  `permissionDecision` fallback). So an MCP server author can
  ship a hook that gates tool calls via `permissionDecision:
  deny` or blocks via `continue: false` without learning a
  different schema. Non-dict returns (str / None / list) keep
  the prior pass-through behaviour as a regression guard. 5
  new envelope tests in `tests/test_hook_handler_types.py`.
- 2026-06-25: row 37 moved ❌ → ✅ — added the plugin agent
  security envelope. New `_apply_plugin_restrictions(defn,
  raw_keys, plugin_name)` helper in `core/pool.py` strips
  `mcp_servers` from plugin-loaded `AgentDefinition`s, sets
  `force_isolation="worktree"`, and WARNs (with audit-friendly
  log lines naming the plugin and the restricted keys) when
  the original frontmatter declared any of
  `_PLUGIN_RESTRICTED_FRONTMATTER_KEYS` (`hooks`, `mcpServers`,
  `permissionMode`, `permissions`, plus snake-case aliases).
  `_raw_frontmatter_keys()` re-parses the YAML header at
  load-time so detection sees keys that `parse_agent_file`
  itself silently drops. `AgentPool._load_directory` accepts a
  `plugin_restricted=False` flag that opts into the envelope;
  `PluginLoader.apply_to_agents` flips it for every plugin
  load. New `AgentDefinition.force_isolation: str | None`
  field threaded through to `OrchestrateTools.spawn_agent`,
  which overrides the caller-supplied `isolation` arg when
  the agent forced its own (string-only, isinstance-guarded
  against duck-typed mocks so a MagicMock can't silently
  disable the worktree branch). 13 new tests in
  `tests/test_plugin_agent_restrictions.py` covering the
  helper, the raw-frontmatter parser, the loader integration,
  and the spawn override. Logging-test harness uses a
  monkeypatched `logger.warning` spy to dodge caplog flakiness
  caused by chromadb's import-time logger config.
- 2026-06-25: row 35 moved ❌ → ✅ — added the managed plugin
  install scope (4th tier matching CC). New
  `_platform_managed_plugins_root()` helper points at the
  same OS-protected directory as `managed-settings.yaml`
  (darwin `/Library/Application Support/Ember/`, linux
  `/etc/ember/`, win32 `%PROGRAMDATA%\Ember\`). `PluginLoader`
  now scans six roots in priority order: user-claude (1),
  user-ember (2), project-claude (3), project-ember (4),
  managed-claude (5), managed-ember (6). Highest priority
  wins same-name collisions — a managed plugin shadows a
  project plugin of the same name. New
  `PluginDefinition.is_managed` property + `managed: bool`
  field on the `PluginInfo` wire shape so the panel can lock
  the disable toggle. `BackendServer.set_plugin_enabled`
  refuses to disable managed plugins with an explanatory
  error before any state mutation;
  `Session._disabled_plugins` filters out managed plugin
  names so a stale persisted disable list (from before a
  plugin became managed) is silently ignored. 14 new tests
  in `tests/test_plugin_managed_scope.py` covering platform
  paths (including the sibling-with-managed-settings
  invariant), discovery + namespace-bucketing, the
  `is_managed` predicate, and both the disable-refusal and
  enable-accept paths in the RPC.
- 2026-06-25: row 33 moved ❌ → ✅ — added the plugin monitor
  (background process) primitive. New `core/monitors/`
  package: `MonitorConfig` parses `.monitors.json` with the
  same precedence as MCP / LSP (user < project < plugin with
  `<plugin>:<name>` namespacing); `MonitorManager` /
  `MonitorHandle` run each monitor as an
  `asyncio.subprocess.Process` with merged stdout+stderr
  drained into a 1000-line rolling deque. A per-monitor
  supervisor task auto-restarts crashed monitors per policy
  (`never`/`on_crash`/`always`) with bounded exponential
  backoff (`1s/2s/5s/15s` → `failed`); user-initiated
  `restart` clears the crash counter and re-launches even a
  `failed` monitor. `shutdown_all` is SIGTERM → wait
  (`_TERM_GRACE=2s`) → SIGKILL, cancels supervisors, and is
  idempotent. Plugin manifest gains a `has_monitors` flag and
  the loader exposes `collect_monitor_roots(disabled)`.
  Session bootstrap constructs the manager unconditionally;
  the `MonitorTools` toolkit registers only when at least one
  monitor is configured. Agent surface: `monitor_status` (all
  snapshots as JSON, including never-started monitors so the
  panel doesn't omit them), `monitor_output(name, lines=N)`,
  `monitor_restart`, `monitor_stop`. 26 new tests in
  `tests/test_monitors.py`.
- 2026-06-25: row 32 moved ❌ → ✅ — added the LSP server
  primitive. New `core/lsp/` package houses `LspServerConfig`
  (manifest parsing with camelCase/snake_case alias support),
  `LspClient` (minimal JSON-RPC over stdio: Content-Length
  framing, initialize/initialized handshake, request/notify,
  graceful shutdown→exit), and `LspServerManager` (lazy launch,
  per-server `asyncio.Lock` to dedupe concurrent first-
  queries, `last_error` for panel surfacing). Plugin manifests
  gain a `has_lsp` flag (`.lsp.json` presence); plugin-bundled
  servers register with `<plugin>:<server>` namespacing via
  `collect_lsp_roots`. Session bootstrap constructs the
  manager unconditionally; the `LspTools` toolkit registers
  only when at least one server is configured, so non-LSP
  sessions don't get tool clutter. Agent surface:
  `lsp_query(server, method, params)` (low-level JSON-RPC
  passthrough — agent supplies LSP method names directly) +
  `lsp_list_servers()` (discoverability). 33 new tests in
  `tests/test_lsp.py` covering config parsing (precedence,
  namespacing, malformed input), client framing
  (Content-Length, EOF, invalid UTF-8), dispatch (response
  routing, error surfacing, unmatched/notification drop),
  manager lifecycle (cache, error recording, query routing,
  shutdown_all), and the agent tool (JSON encoding, null
  result, invalid params, error surfacing, discoverability).
- 2026-06-25: row 30 moved ❌ → ✅ — added per-spawn worktree
  isolation to `OrchestrateTools.spawn_agent`. New `isolation`
  parameter on `spawn_agent`; accepting `"worktree"` triggers
  `_create_isolated_worktree` (errors cleanly if the project
  isn't a git repo) and rebinds tool `base_dir` to the
  worktree path via `_rebind_tool_base_dirs` (shallow-copies
  the agent's toolkit list first, so the pool's shared
  instances stay untouched — no race for concurrent non-
  isolated spawns). `_finalize_worktree` restores tool
  `base_dir`s and either reaps the worktree (no changes) or
  preserves it with a footer listing the `git merge` / `git
  worktree remove` commands. Worktree creation/cleanup happens
  inside try/finally-ish paths so timeouts and exceptions
  don't leak worktrees. Tools without a `base_dir` attribute
  (most MCP clients) still see the project root — the task
  description includes the worktree path so the model can
  route around that. 18 new tests in
  `tests/test_orchestrate_worktree.py` covering the pure
  helpers (`_finalize_worktree`, `_rebind_tool_base_dirs`,
  `_create_isolated_worktree`) and full end-to-end spawns
  including the dirty-worktree-preserved path.
- 2026-06-25: row 27 moved ❌ → ✅ — added the agent-facing
  `slash_command` tool (`SlashCommandTool` in
  `core/tools/slash.py`). Lets the agent invoke any slash
  command from inside a tool-using turn — `/codeindex search`,
  `/knowledge search`, `/compact`, `/schedule`, markdown
  commands, skills, etc. — and read back the resulting text.
  Dispatches through `CommandHandler.handle` directly (NOT the
  session-level `dispatch` wrapper that calls
  `_render_result`), so `CommandAction.QUIT` / `CLEAR` can't
  SystemExit the process or wipe the session. A small blocked
  set (`/quit`, `/exit`, `/clear`, `/login`, `/logout`,
  `/model`) is refused with an explanatory error before
  dispatch — those would either kill the session, invalidate
  the current turn, or require UI interaction. Headline safety
  test: `test_clear_does_not_invoke_handler` confirms the
  block fires BEFORE the underlying handler runs (no side
  effect leaks through). 17 new tests in
  `tests/test_slash_command_tool.py`.
- 2026-06-25: row 25 moved ❌ → ✅ — added the `TodoWrite`
  planning tool. New module `core/tools/todo.py` defines
  `TodoItem` (content, status, active_form), `TodoStore` (the
  atomic per-session list), and `TodoTools` (single-method
  toolkit registering `todo_write`). Wired onto `Session` as
  `session.todo_store` and into the agent tool list alongside
  `LoopTools`. Each `todo_write` call REPLACES the entire list
  (CC parity — no partial merge, no delta detection). Inputs
  are validated: malformed rows are dropped but errors surface
  in the reply string so the model can self-correct on the
  next call. Reply also nudges "keep at most one item
  in_progress at a time" when the count goes above one.
  Snapshots are exposed via `RpcMethod.GET_TODOS` →
  `BackendServer.get_todos()` so UI surfaces (webview sidebar,
  TUI status line, future ASCII status pane) can render the
  list. 20 new tests in `tests/test_todo_tool.py`.
- 2026-06-25: row 20 moved ❌ → ✅ — added the SDK-style
  `GET_SLASH_COMMANDS` RPC. Mirrors Claude Code's
  `slash_commands` field: returns one list combining built-in
  commands (from `CommandHandler._COMMANDS`), markdown-authored
  commands (via `discover_markdown_commands`), and
  user-invocable skills. Each entry carries the same four-key
  shape (`name`, `description`, `source`, `argument_hint`) so
  SDK consumers iterate uniformly. Failures in skill / markdown
  enumeration degrade gracefully — built-ins always return so
  the RPC never hard-fails. Wired through
  `RpcMethod.GET_SLASH_COMMANDS` → `BackendServer.get_slash_commands()`
  → dispatch table in `backend/__main__.py`. Includes the
  startup-validation `validate_rpc_table` invariant (catches a
  forgotten dispatch entry). 11 new tests in
  `tests/test_slash_commands_rpc.py`.
- 2026-06-25: row 19 moved ❌ → ✅ — added markdown-authored
  custom commands. New module `core/utils/markdown_commands.py`
  discovers `.md` files at four roots (user-tier
  `~/.claude/commands/` + `~/.ember/commands/`; project-tier
  `<project>/.claude/commands/` + `<project>/.ember/commands/`)
  with deterministic precedence: project beats user, ember
  beats claude within a tier. Frontmatter parsed via
  `yaml.safe_load` (graceful fail-open). Three template tokens:
  `$ARGUMENTS` (plain), `` !`cmd` `` (shell via
  `/bin/sh -c`, 30 s timeout, all tokens in a body fan out
  concurrently), `@path` (file inlining with project-dir
  scoping — a project-tier command cannot read paths outside
  its project, but a user-tier command can reference anywhere).
  Wired into `CommandHandler.handle` between the built-in
  dispatch table and the skill-matching fallback, so built-ins
  always win and markdown commands beat same-name skills.
  Includes a built-in-protection test
  (`test_builtin_command_still_beats_markdown`). 29 new tests
  in `tests/test_markdown_commands.py`.
- `UserPromptExpansion` deferred — natural firing site (markdown-command expansion)
  is row 19, which hasn't landed.
- TS-only `auto` permission mode skipped (Python SDK doesn't expose the model
  classifier).
- 2026-06-28: FE pure-helper sweep landed — `clients/web/src/components/ChatItems.test.ts`
  (24 cases) locks down the code-paste edit-mode round-trip (`swapCodeBlocks` /
  `restoreCodeBlocks`: placeholder shape, identity round-trip, snippet removal on
  placeholder delete, reorder-on-reorder, store-clear-on-new-edit, lost-snippet
  recovery), `looksLikeAsciiArt` (2+ structural lines required, prose-with-arrow
  false-positive guard, Unicode/ASCII mixed input), and `guessLang`
  (case-insensitive, last-dot wins, tsx/jsx disambiguation, shell aliases →
  bash, yml/yaml + html/xml + empty fallback). Helpers were file-local; exported
  them so the tests could import. Cross-cuts rendering across many parity rows
  (edit-message, ASCII art display, tool-output highlighting). All 164 vitest
  cases pass + `tsc --noEmit` clean.
- 2026-06-28: WS-URL precedence resolver now covered —
  `clients/web/src/protocol/client.test.ts` (8 cases). Pins the
  param > meta > global > dev-default precedence the four surfaces
  rely on for BE discovery: Tauri sets `?ws=` to the spawned BE's
  random port; VSCode webview delivers via `<meta name="ember-ws-url">`
  (CSP-safe); JetBrains JCEF injects `window.__EMBER_WS_URL__`; plain
  web falls back to the dev port. Negative checks: empty `?ws=` must
  NOT pre-empt the meta tag; unrelated query params ignored; missing
  `document` (SSR probe) still resolves. 172/172 vitest, tsc clean.
- 2026-06-28: Session-side rewake + MCP resolver glue now directly
  tested — `tests/test_session.py` adds `TestSessionQueueRewake`
  (3 cases: empty-text drop, single append, FIFO accumulation —
  documents that ``_pending_reminders`` is a list-append contract
  so a refactor to a set would trip) and `TestSessionMcpResolver`
  (6 cases: manager absent / None / unknown server / unknown tool /
  happy path identity / client-missing-functions-attr graceful
  fallback). The executor-side counterparts were tested in
  `test_hook_async_rewake.py` + `test_hook_handler_types.py`, but
  the Session-side closures were only exercised indirectly via
  Session construction. 31/31 in test_session.py.
- 2026-06-28: FE DOM-render tests enabled. Added jsdom +
  @testing-library/react/jest-dom as dev-deps. Per-file
  `// @vitest-environment jsdom` directive — keeps the existing
  172 pure-helper tests on the node runner (faster + their custom
  window stubs don't conflict with jsdom's pre-populated one).
  First component-level test: `clients/web/src/components/PlanCard.test.tsx`
  (11 cases) covers the plan-mode UX contract — pending state renders
  body + both buttons + pause hint; Approve/Refine click handlers
  fire with item.id; task checklist renders nothing for prose-only
  plans; per-status markers (○/●/✓) are pinned so a refactor can't
  silently swap them; activeForm shown while in_progress (falls
  back to content if empty); approved/dismissed states hide buttons
  + show the footer + drop the pause hint + apply the
  `plan-card--<state>` variant class. 183/183 vitest, tsc clean.
  Sets the pattern for future component tests (Composer, Sidebar,
  StatusBits etc.).
- 2026-06-28: StatusBits component coverage —
  `clients/web/src/components/StatusBits.test.tsx` (27 cases) covers
  the four status-line widgets touched by rows 50/51/7. `ModeBadge`:
  hidden in default mode + for unknown modes (forward-compat),
  per-mode label/variant-class for plan/acceptEdits/dontAsk/
  bypassPermissions, hover title with `/plan`-style guidance.
  `AutoApproveSwitch`: aria-checked + is-on class flip with mode,
  bidirectional toggle callback (off→true, on→false), title hint
  flips with state. `SessionChip`: empty-id placeholder, 8-char
  short prefix rendered, title shows the FULL id, click writes the
  FULL id to `navigator.clipboard` (not the short prefix).
  `CtxMeter`: tone thresholds pinned at exactly 60 (warn) and 85
  (danger), pct clamped to [0,100] (handles BE token-count lag),
  fmtTokens: <1k verbatim / 1k-10k one-decimal / ≥10k no-decimal,
  title shows raw token count, omits "of 0" when max unknown.
  210/210 vitest, tsc clean.
- 2026-06-28: HitlDialog (permission-prompt UI) covered —
  `clients/web/src/components/HitlDialog.test.tsx` (15 cases).
  Single-requirement path: friendly_name in title with tool_name
  fallback; counter hidden in 1-of-1 case; agent_path + details
  sub-lines render when set; all four buttons present; each of
  Allow-once/Always/Similar/Reject resolves with the right
  ``{action, choice}`` pair (load-bearing — swapping the choice
  strings silently persists wrong allow rules). Batch path:
  N/M counter renders for multi-req; first click does NOT
  resolve (counter advances + next-req title); only the LAST
  click fires onResolve **once** with the full ordered batch
  (prevents partial BE unblock); mixed confirm/reject in the
  same batch all land. Empty list renders nothing.
  225/225 vitest, tsc clean.
- 2026-06-28: HitlArgsView (tool-args renderer used by the
  permission dialog) covered —
  `clients/web/src/components/HitlArgsView.test.tsx` (23 cases).
  The component is a key-name router that picks one of seven
  render strategies per arg; each branch silently regresses the
  permission-dialog UX. Sentinels: undefined/empty-object render
  nothing; null/undefined values emit em-dash placeholder (not the
  literal "null"). Primitives: bool/num get class wrappers; short
  single-line strings render as a code chip, long (≥80) or
  multi-line escape to a pre. Key-name routing: `file_path` +
  `path` → FileTypeIcon pill with basename + full path;
  `command`/`cmd` → shell block; `contents`/`body`/`text`/`prompt`/
  `query` → pre block regardless of length (file-write tool needs
  this even for short content). Arrays: primitives → mini-tags
  (scan-friendly); mixed primitives still tags; objects → JSON
  pre. Edit-pair (`old_string` + `new_string` both strings) →
  side-by-side diff strip; source keys filtered out so they don't
  duplicate as rows; `(empty)` placeholder when one side is blank;
  one-sided ``old_string`` alone falls through to the generic
  short-string code chip. Key labels snake_case → spaced.
  248/248 vitest, tsc clean.
- 2026-06-28: Toasts notification stack covered —
  `clients/web/src/components/Toasts.test.tsx` (11 cases). The
  toast stack carries project-level async events (scheduled
  tasks finishing, update banners) so they don't bury themselves
  in whichever chat is open. Tests pin: empty list collapses;
  one card per toast with title + optional body; ARIA region
  landmark wired; body click fires onClick then dismisses after
  150ms animation tail; missing onClick doesn't crash on body
  click; `is-closing` class flips synchronously so the CSS
  animation can play; X-button dismisses WITHOUT firing onClick
  (`stopPropagation` guard — easy bug for an event-bubbling
  refactor); auto-dismiss at the default 8000ms TTL + 200ms tail;
  custom ttlMs respected (used by long-lived update banners).
  259/259 vitest, tsc clean.
- 2026-06-28: UpdatePrompt (auto-updater modal) covered —
  `clients/web/src/components/UpdatePrompt.test.tsx` (12 cases).
  Version copy with current + latest; host-aware primary button
  (`Install & restart` under Tauri vs `Download` on web); under
  Tauri, click invokes `ember_install_update` and flips to
  `Installing…` while both buttons disable; invoke failure
  surfaces the error message inline and clears busy so the
  user can retry; backdrop click BLOCKED while busy (no abort
  mid-install — half-replaced binary would brick the app);
  on web, Download click opens the `download_url` in `_blank`;
  missing download_url no-ops cleanly. Dismissal paths: Later
  button → onDismiss; backdrop click dismisses when idle;
  click inside the dialog does NOT dismiss (event-target
  guard).
- 2026-06-28: ThemeToggle (auto/light/dark cycler) covered —
  `clients/web/src/components/ThemeToggle.test.tsx` (11 cases).
  Initial state from localStorage (default auto, light/dark
  honoured, unknown values fall back to auto for forward-compat).
  Click cycles auto→light→dark→auto and persists each step.
  Title hint forecasts the NEXT step (not the current state) so
  the user can predict the outcome. In auto mode,
  `data-os-prefers-light` mirrors `matchMedia(prefers-color-scheme)`
  so the CSS @media rule doesn't fight an explicit override.
  Explicit modes remove the mirror attr (OS pref no longer
  relevant). Hit a Node 25 vs jsdom storage-shim conflict —
  Node 25 ships a native `localStorage` stub that's an empty
  object with no methods (masks jsdom's Storage); fixed by
  installing an in-memory shim per test via
  `Object.defineProperty(window, 'localStorage', …)`. Same
  treatment for `matchMedia` which jsdom doesn't ship at all.
  282/282 vitest, tsc clean.
- 2026-06-28: Sidebar (session-list drawer) + CodeIndexIndicator
  badge — `clients/web/src/components/Sidebar.test.tsx` (13 cases)
  and `clients/web/src/components/CodeIndexIndicator.test.ts`
  (15 cases). Sidebar: open/closed class drives the slide; empty
  state shows "No past sessions"; rows render name + 8-char id
  prefix; session_id falls back when name is blank;
  ``.current`` class flags the active conversation; ``detail``
  hover-title with name fallback; click pick → onPick(id);
  "+ New chat" → onNewChat; mobile-only backdrop (innerWidth ≤
  700) — pinned the threshold because regressing it would
  silently dismiss desktop sidebars on every chat click; backdrop
  absent when closed (no zombie overlay). CodeIndexIndicator:
  `providerName` matches github/gitlab/bitbucket via http(s) AND
  `git@` SSH form, case-insensitive, falls back to "Git provider"
  for unknown hosts; `classify` priority order pinned at every
  transition (sync_error > needs_install > inactive >
  sync_in_progress > head_indexed > "not indexed" fallback) —
  silent priority drift = wrong pill state, so each pair is one
  test. Sidebar needed `vi.hoisted` to install the
  localStorage + ResizeObserver shims BEFORE the embedded
  ThemeToggle's module-load reads — first time we hit
  Node-stub-masks-jsdom on an indirect import. 310/310 vitest,
  tsc clean.
- 2026-06-28: FileTypeIcon (kindFor) + FilePill (inline file ref)
  covered — `clients/web/src/components/FileTypeIcon.test.ts`
  (14 cases) and `clients/web/src/components/FilePill.test.tsx`
  (12 cases). kindFor: extension routing across all 10 kinds
  (image/pdf/code/data/doc/archive/video/audio/shell/file
  fallback), case-insensitive, last-dot-wins for multi-dot
  filenames, unknown extensions degrade to "file" not crash.
  Pinned the code-vs-data split (js/ts/py/rs go in "code", not
  "data") because that's the most likely silent regression.
  FilePill: basename derived from path, full path in title attr,
  menu toggle on click, Esc closes, outside-click closes (mouse-
  down listener — pinned because mousedown vs click matters for
  popovers), Copy path writes the FULL path to clipboard (not
  the basename — same load-bearing distinction as
  ``SessionChip``), Open/Preview routes through ``host.openFile``
  so VSCode/JetBrains/Tauri/web each take the right branch.
  336/336 vitest, tsc clean.
- 2026-06-28: FilePreview + FileRefPicker (the file-pick + file-
  preview flows) covered —
  `clients/web/src/components/FilePreview.test.tsx` (9 cases)
  and `clients/web/src/components/FileRefPicker.test.tsx`
  (15 cases). FilePreview: loading/success/error states from the
  ``read_file`` RPC; language-class from response with extension
  fallback; rejection AND BE-success-with-error-field both
  surface; Esc/backdrop/close-button all dismiss; Copy writes
  the resolved canonical path (not the original prop — symlinks
  matter). FileRefPicker: input autofocuses on mount (keyboard-
  first affordance); seed empty-query call so the list isn't
  blank; "No matches." placeholder; query changes refire
  ``completeFiles(q, 30)``; **stale-result race guard** pinned —
  if RPCs resolve out of order the ``seq`` counter must drop
  the stale one (would only show on slow networks otherwise);
  RPC failure clears prior results rather than leaving stale
  ones up; arrow Up/Down cycles with bounds capped; Enter picks
  the active row; Esc cancels; Enter with no results no-ops;
  mouseEnter sets active; **mouseDown (not click) picks** —
  load-bearing because the picker sits above the composer and
  click loses to the input's blur event; backdrop click cancels.
  360/360 vitest, tsc clean.
- 2026-06-28: ScrollIndicator thumb math extracted + tested —
  refactored `ScrollIndicator.tsx` to pull the geometry out of
  the scroll-event handler into a pure `computeThumb` helper +
  `SCROLL_INDICATOR_PAD` / `SCROLL_INDICATOR_MIN_THUMB` constants.
  Then `clients/web/src/components/ScrollIndicator.test.ts`
  (13 cases). Visibility: hides when fits exactly + absorbs
  1px-rounding slack (otherwise a flex layout that rounds by 1
  flickers the thumb); shows when there's anything to scroll.
  Position: starts at PAD; **ends exactly at track-bottom when
  scrolled all the way** (load-bearing — the indicator lies if
  this is off); linear interpolation at 50%. Height: ratio ×
  clientHeight, **min-24px floor on long lists** (prevents
  invisible 3-pixel thumbs on 100k-line files), capped at
  track length. Edge cases: tiny viewport (pad eats whole
  height) still renders a measurable thumb; custom pad
  override; defensive over-scroll without NaN. Caught one math
  mistake in my own test setup (wrong expected value) — fixed
  to match the actual ratio×height formula, which is itself
  now pinned.
  373/373 vitest, tsc clean.
- 2026-06-28: Composer slash-command surface covered (BUILTIN_
  COMMANDS structure + filterSlashCommands helper) —
  `clients/web/src/components/Composer.test.ts` (14 cases).
  Refactored Composer to extract the inline slash filter from
  ``refreshMenu`` as a pure helper. Tests pin: BUILTIN_COMMANDS
  is non-empty; all names start with ``/``; descriptions are
  non-empty; no duplicate names; the load-bearing CC-parity
  commands (/help, /clear, /compact, /sessions, /fork, /model,
  /login, /logout, /plugins, /hooks, /loop, /schedule) all
  present. filterSlashCommands: empty query returns the full
  pool; prefix match on name-after-slash (``co`` → /compact +
  /codeindex, NOT /clear); case-insensitive; substring does
  NOT match (``lear`` ≠ /clear — prevents typos from
  surfacing unwanted commands); default 12-result cap; custom
  limit override (0 → empty); preserves source order (the
  composer's ``active: 0`` depends on it); works on
  built-ins + skills merged pool. The full Composer component
  (mention menu, history, mode machine, contenteditable) is
  left to E2E + manual verification — too much DOM-bound
  state for useful unit tests. 387/387 vitest, tsc clean.
- 2026-06-28: BE empty-call guardrail covered —
  `tests/test_empty_guard.py` (13 cases) for
  `core/tools/codeindex/empty_guard.is_empty_call`, the
  precondition that catches the agent's case-11-shape failure
  (``codeindex_query(security=None, sections=[…], limit=15)`` —
  named a dimension, passed None instead of severities, then
  read arbitrary ranked items as "worst offenders"). Pinned:
  empty kwargs → True; all-None → True; empty-list filters →
  True; mixed None + [] still True; query_text OR ids OR any
  typed-filter set → False; bool ``False`` for
  ``needs_refactoring`` is meaningful (NOT empty — "items that
  don't need refactoring" is a real query); narrowing + output-
  control combined is still not empty.
  Found a docstring-vs-behaviour drift: the docstring claimed
  ``sections`` / ``limit`` / ``commit`` "don't count toward
  narrowing input", but the helper doesn't actually filter by
  name. The only call site
  (``query_service.codeindex_query``) excludes those kwargs
  itself, which is what makes the detection work. Pinned the
  actual behaviour + added a "caller-discipline" warning in
  the source docstring so the divergence is visible to future
  callers.
- 2026-06-28: ``protocol/agno_events`` (event → TUI-string layer)
  covered — `tests/test_agno_events.py` (26 cases). format_tool_args
  sentinels (None/empty/non-dict don't crash), generic path
  (1-N keys joined with `, `, 3-key cap, 30-char value cap with
  ellipsis, non-string values stringified), spawn_agent special-case
  (agent name surfaced, mode included when truthy, task collapsed
  to first non-empty line capped at 80 chars, short task not
  truncated, empty task omits the quote pair). TOOL_NAMES
  friendly-name contract: core filesystem tools (Read/Write/Edit),
  shell (Bash), search variants collapsed to Grep/Glob, web tools
  to WebSearch/WebFetch, orchestration (Agent/Team/Delegate),
  subsystem tools (Knowledge/Memory/Schedule). House-style invariant:
  every display name is single-word TitleCase (no spaces — would
  break terminal-column alignment).

  **Real bug surfaced + fixed**: spawn_team's `agent_names` arg is a
  LIST (its contract). The previous source did
  `parts = [agent]` where `agent` was the list directly, then
  ``", ".join(parts)`` crashed with TypeError. Every spawn_team
  tool-call header would have crashed the event renderer.
  Fixed by coercing list/tuple to comma-joined string before
  building parts. Pinned with a regression test. 2619/2619 BE
  tests pass.
- 2026-06-28: ``core/knowledge/ingest`` private helpers covered —
  `tests/test_knowledge_ingest_helpers.py` (21 cases) for
  ``_is_text_path``, ``_string_meta``, ``_reader_for_url``.
  ``_is_text_path``: extension-based text detection, case-
  insensitive, no-extension → not text. ``_string_meta``: defends
  chroma's `dict[str, str]` requirement — non-dict drops to
  `{}`, all values coerced to str, keys also coerced (rare
  int-key edge case), `None` values filtered out (won't emit
  `"None"` string), empty string / 0 / False preserved. URL
  routing: youtube.com + youtu.be → YouTubeReader (transcript,
  NOT page chrome — wrong reader silently degrades ingest
  quality), wikipedia.org → WikipediaReader, arxiv.org →
  ArxivReader, `.pdf` path → PDFReader (works on any host),
  else → WebsiteReader. Host + path checks both case-insensitive.
- 2026-06-28: `core/db/engine` cache identity contract covered —
  `tests/test_db_engine.py` (20 cases). The cache is per
  normalised path so `~/.ember/state.db` and its expanded
  absolute form must collapse to ONE engine — otherwise SQLite
  locking gets confused with two engines on the same file.
  Pinned: `_normalize_path` expands `~`, resolves relative
  paths, collapses `..` segments, accepts str|Path with same
  result; `sync_url`/`async_url` use the right SQLAlchemy
  driver prefix (`sqlite:///` vs `sqlite+aiosqlite:///` —
  drift here silently breaks alembic or the async engine);
  three equivalent path spellings all hit the same cache slot
  (the load-bearing invariant); sync vs async caches are
  independent; parent directory auto-created on first call
  (so callers don't have to mkdir defensively); `dispose_all`
  actually clears the caches (otherwise the next call returns
  a disposed engine unusable for queries).
- 2026-06-28: TUI formatting helpers covered —
  `tests/test_tui_formatting.py` (23 cases) for
  `frontend/tui/widgets/_formatting`. `format_elapsed_time`:
  zero, sub-second decimals, 59.9s last in seconds form, 60.0
  flips to "1m 0s" (transition pinned — a refactor using
  `> 60` instead of `< 60` would leave 60 as "60.0s"), mixed
  minute+second display, 59m 59s last in minute-and-seconds,
  1h+ renders as "60m 0s" (no hours branch — pinned so a
  future addition is deliberate). `format_token_count`: 8
  explicit thresholds (1k/10k/1m/10m/1b/10b/1t/10t) each
  pinned at the boundary just-below-threshold (one-decimal
  format) vs at-threshold (next-tier format). The
  "9999 → 10.0k vs 10000 → 10k" visual quirk pinned and
  documented (one extra char vanishes at the boundary —
  intentional one-decimal-precision behaviour, not a bug).
  Goes up to "999t" through the final fallback ``return``.
- 2026-06-28: `core/utils/mentions.process_file_mentions` covered —
  `tests/test_mentions.py` (21 cases). Pins the `@file` extractor
  used by interactive REPL / session runner / backend server.
  Empty / no-@ pass-throughs; mention at start / middle / end /
  after newline; **email-style `user@domain` does NOT match**
  (load-bearing: users paste emails in prompts; regex lookbehind
  requires whitespace or start-of-string before `@`); mixed
  email + real mention; multiple mentions in appearance order;
  duplicates kept (no implicit dedup); absolute / relative /
  multi-extension paths; hint block format and prepending.

  **Real docstring drift fixed**: top-level docstring claimed
  "The `@` tokens are removed from the body entirely" but the
  source preserves them (the inline comment correctly says
  "Preserve the literal @<path> token..."). Updated the
  docstring to match the actual behaviour and explain WHY the
  token stays (FE bubble renders the user's reference inline).

  Side observation: there's a parallel `process_file_mentions`
  in `frontend/tui/input_handler.py` that DOES strip the `@`
  prefix and uses a different hint format (`[Referenced files:
  ...]` line, not `<attached-files>` wrapper). Active TUI tests
  cover that variant. Two functions with the same name doing
  slightly-different things is a smell worth consolidating
  later but out of scope here.
- 2026-06-28: `core/utils/media` URL-side covered —
  `tests/test_media_urls.py` (19 cases) fills the gap left by
  `test_images.py` (which covers the path-side). Tests pin:
  `_classify_extension` dispatcher across all 4 media kinds
  (image / audio / video / document=pdf), unknown ext returns
  ``"unknown"`` (defensive — callers branch on the string),
  case-insensitive. `extract_media_urls`: empty / no-URL /
  no-extension URL all return ``None``; image / audio / video /
  PDF each routed to the right Agno class with the right
  kwarg bucket key (`images` / `audio` / `videos` / `files`);
  both http and https schemes match; URL with query string
  classifies on the path before `?` but the Agno Image.url
  preserves the full URL including query; multiple URLs in one
  message all extracted; uppercase extensions match via
  `re.IGNORECASE`; **only kinds with actual matches emit
  buckets** (no empty `images: []` that would forward as a
  bad provider call).
- 2026-06-28: `_hook_result_from_envelope` covered —
  `tests/test_hook_envelope_parser.py` (18 cases). The
  universal translator that turns mcp_tool handler return
  values into the same `HookResult` shape the command handler
  emits from stdout-JSON. CC-compatible envelope: `continue:
  false` blocks, `systemMessage` carries explanation,
  `hookSpecificOutput.permissionDecision` is the structured
  verdict, bare `permissionDecision` is the legacy fallback.
  Pinned: **wrapped form wins over bare** (silent-regression
  risk if precedence drifts), empty nested falls back to bare,
  non-dict `hookSpecificOutput` falls back to bare; None →
  empty continue=True; str / list / int stringified into the
  message (preserves MCP tool payload). Defensive guards:
  ``None`` `systemMessage` / `permissionDecision` don't leak
  as the literal "None"; non-bool `continue` (0/1) coerced
  correctly.
- 2026-06-28: `Session.broadcast` + `register_broadcast_callback`
  contract pinned — `tests/test_session.py` adds
  `TestSessionBroadcast` (10 cases). Indirect coverage existed
  via output_styles + plan_mode tests; now the contract itself
  is pinned: append-on-register, **idempotent re-register**
  (load-bearing because /plan and /accept both subscribe the
  same transport closure during bootstrap — double-dispatch
  would fire every event twice), distinct callbacks both kept,
  fan-out fires every subscriber in registration order,
  **payload identity preserved** (no defensive deepcopy —
  callers rely on identity equality for downstream
  bookkeeping), **exception isolation** (one buggy subscriber
  doesn't sink the rest), no-callback broadcast is a no-op,
  **missing `_broadcast_callbacks` attribute is a no-op**
  (defensive for `Session.__new__` test constructs that skip
  `__init__`), **subscriber registering during broadcast
  doesn't fire mid-broadcast** (the snapshot copy in the
  source prevents the list-mutation RuntimeError and defers
  the new subscriber to the next broadcast).
  41/41 in test_session.py (was 31).
- 2026-06-28: Row 24 (Web fetch/search) deepened —
  `tests/test_web_tools.py` adds `TestExtractTextFromHtmlDeepDive`
  (15 cases) on top of the existing 6 happy-path tests. The
  HTML→text extractor runs 4 regex passes (script removal /
  style removal / generic tag → space / whitespace collapse);
  before, only the script-removal happy path was tested. Now
  pinned: **`<style>` block removal** (was untested — symmetric
  to script but easy to drop in a refactor), multi-line script
  + style via DOTALL flag, multiple scripts per doc, scripts
  with attributes (`<script type="...">`), nested tags all
  collapse, whitespace runs collapse to single space, leading
  /trailing whitespace stripped, empty + plain-text input safe,
  HTML entities pass through literally (not decoded — pinned
  so a future `html.unescape` addition is deliberate),
  malformed `<script>` without closing tag falls through
  (documented limitation pinned), self-closing tags, deeply
  nested DOM, DOCTYPE + HTML comments collapse via the generic
  tag-stripper. 21/21 in test_web_tools.py (was 6).
- 2026-06-28: `chat/model.ts` remaining pure helpers covered —
  `clients/web/src/chat/model.test.ts` adds 23 cases for
  `extractAttachedPaths`, `correctStatsCtx`, `restoredStatsItem`.
  `extractAttachedPaths`: hint-line primary path (BE
  `<attached-files>` wrapper with comma split, trim, filter
  empty), hint-wins-over-@-fallback precedence, @-mention
  fallback for legacy messages, email-style `@` rejected (same
  constraint as the BE side). `correctStatsCtx`: stats item
  patched with new inputTokens + corrected:true on matching
  runId, non-stats items pass through identity-equal,
  mismatched runId pass through, multiple matches all patched,
  **input not mutated in place** (load-bearing for React
  identity-equality memoisation). `restoredStatsItem`: standard
  numeric fields read, missing fields default to 0 (not NaN —
  visible badge bug if NaN leaks), string-numeric coercion
  (older Agno persists numbers as strings), null→0, non-numeric
  strings→0 (NaN guard), runId coerced to string, empty default,
  visibleThinkTokens always 0 (thinking stripped on restore),
  corrected:true (no live RPC will patch historical stats).
  410/410 vitest, tsc clean.
- 2026-06-28: `CommandHandler._handle_markdown_command` wrapper
  contract pinned — `tests/test_handle_markdown_command.py`
  (9 cases). Discovery is exhaustively tested in
  `test_markdown_commands.py`; this file covers the wrapper's
  error-handling boundary: empty name (bare `/`) → None, **
  discovery exception swallowed → None** (fall-through to next
  dispatch tier — user typed a slash command; if it's not
  markdown they want the dispatcher to try the next registry),
  unknown name → None, **render exception surfaces as
  CommandResult.error** (different from discovery — user
  explicitly invoked this command, silently swallowing would
  read as "command doesn't exist"), success → CommandResult
  with `action=RUN_PROMPT` + `kind=INFO`, args + project_dir
  forwarded to render, `cross_tool_support` flag wires through
  to discover's `read_claude` arg. The swallow-vs-surface
  asymmetry is the load-bearing distinction — pin it so a
  "consistent error handling" refactor doesn't silently flip
  one direction.
- 2026-06-28: `applyEvent` error/info-event branches covered —
  `clients/web/src/chat/model.test.ts` adds 8 cases for
  previously-untested event types. `tool_error` (the BE's
  dedicated error event, distinct from `tool_completed{is_error:
  true}`): patches the running tool card with status=error +
  isError=true + result=msg.error; **only patches a RUNNING
  tool** (a stale tool_error arriving after the tool completed
  normally must NOT reopen the closed card — spawns a
  standalone error item instead); fallback spawns standalone
  error when no prior tool_started (defensive — don't drop
  out-of-band errors); patches the MOST RECENT running tool
  with multiple concurrent cards. `run_error` and `error`
  events append error items; `info` events append info items.
  **Unknown event types fall through** (forward-compat — new
  BE events the FE doesn't recognise pass items through
  unchanged rather than crashing). 418/418 vitest, tsc clean.
- 2026-06-28: `EmberClient` listener-subscription contract
  pinned — `clients/web/src/protocol/client.test.ts` adds 6
  cases. `onEvent` and `onStateChange` are the pub/sub APIs
  the rest of the FE builds on; the invariants are simple but
  load-bearing for React StrictMode double-mount cleanup:
  subscribe returns an unsubscribe function (canonical
  effect-cleanup shape), close() is safe pre-connect (cleanup
  may fire before connect lands), close() is idempotent
  (defensive), each subscribe call returns a distinct
  unsubscribe (Set semantics — pinned so a future Array-based
  refactor surfaces deliberately), default constructor calls
  resolveWsUrl. WebSocket lifecycle (connect/onmessage/
  onclose/backoff) intentionally NOT unit-tested — covered
  by Playwright e2e in clients/web/e2e/. 424/424 vitest,
  tsc clean.
- 2026-06-28: Host bridge `notify` + `notifyFileEdited`
  covered — `clients/web/src/lib/host.test.ts` adds 14 cases
  for the sister methods of the already-tested `searchCode`.
  `notify`: empty title+body short-circuits to false, web
  returns false (caller falls back to in-app toast),
  `__EMBER_HOST__.notify` bridge wins over per-host dispatch
  (richer host-injected APIs preferred), Tauri dispatches via
  `plugin:notification|notify` with body defaulting to empty
  string (plugin requires the key), Tauri shim without
  `core.invoke` falls through to false (partial-shim safety),
  VSCode via `ember:notify` postMessage, JetBrains via
  cefQuery, exceptions caught and surfaced as false.
  `notifyFileEdited`: empty path is a no-op, JetBrains
  cefQuery dispatch, VSCode postMessage dispatch, Tauri/web
  no-ops (IDE's own file watcher catches the write), exceptions
  swallowed (best-effort fire-and-forget hint). 438/438 vitest,
  tsc clean.
- 2026-06-28: Host bridge `openFile` + `canOpenNatively` +
  `setPreviewFallback` covered —
  `clients/web/src/lib/host.test.ts` adds 15 cases for the
  primary file-open routing the file-pill / file-mention
  surfaces depend on. Per-host dispatch pinned: Tauri prefers
  `__EMBER_HOST__.openFile` bridge → falls back to
  `plugin:shell|open` → falls back to preview callback;
  VSCode via `ember:openFile` postMessage; JetBrains prefers
  `__EMBER_HOST__.openFile` bridge over `cefQuery` (load-bearing
  precedence — JCEF plugin injects the richer bridge); web
  falls through to the preview callback. Empty path returns
  false (defensive); web without a registered fallback
  returns false silently; Tauri partial-shim (no `core.invoke`,
  no bridge) falls back to preview rather than crash;
  exceptions from a buggy bridge.openFile are caught + the
  fallback fires. `canOpenNatively` returns true for all three
  IDE hosts and false for web. 453/453 vitest, tsc clean.
- 2026-06-28: `ClientStateStore` + `ensureClientId` covered —
  `clients/web/src/clientState.test.ts` (20 cases). Per-client
  UI state with optimistic local cache + debounced RPC writes;
  zero prior test coverage. `ensureClientId` mints/reads the
  stable client id from localStorage, prefers
  `crypto.randomUUID` with the `c-<ts>-<rand>` fallback for
  pre-2022 browsers, tolerates localStorage unavailability
  without throwing. `ClientStateStore`: hydrate populates cache
  via `get_client_state` RPC, **marks hydrated even on RPC
  failure** (first-paint robustness), null response safe; set
  is **optimistic** (cache update synchronously visible before
  the RPC); **set debounces per-key** (rapid same-key writes
  collapse to one RPC — load-bearing for composer typing not
  flooding WS); different keys debounce independently; **delete
  cancels pending set** for the same key (otherwise a typed-
  then-cleared draft re-creates the row); onChange fires `(key,
  value)` on every set/delete (delete signals via empty-string
  value), unsubscribe removes the listener, multiple subscribers
  all fire; flush stops pending timers. 473/473 vitest, tsc
  clean.
- 2026-06-28: `backend/__main__._serialize` covered —
  `tests/test_backend_serialize.py` (22 cases). The JSON-safety
  converter every RPC response runs through. None passthrough;
  primitives (str/int/float/**bool — pinned to land BEFORE int
  branch** because ``isinstance(True, int)`` is True in Python
  and serializing True as 1 would be a subtle wire-shape
  change); list/tuple recursion (tuple → list since JSON has
  no tuple); empty dict, **non-str keys coerced to str**
  (load-bearing — int-keyed dicts from older Agno schemas
  would crash at the JSON layer), None key → "None" pinned
  defensively, values recursively serialized; **Pydantic models
  hit model_dump branch BEFORE str() fallback** (otherwise
  responses would serialize as Python repr strings); Path /
  arbitrary classes via str(); **sets fall through to str()**
  (current behaviour pinned — probably wrong but a future fix
  is deliberate); nested list-of-dicts-of-lists / dicts with
  pydantic values / tuples of pydantic models all serialize
  correctly.
- 2026-06-28: `protocol/rpc.validate_rpc_table` covered —
  `tests/test_rpc_validate_table.py` (13 cases). The meta-check
  that every `RpcMethod` enum value has a handler in the
  backend dispatch table; fires once at startup so "added enum
  member, forgot to register" surfaces immediately rather than
  at first call. Tests pin: complete table passes, extra
  unregistered keys are fine (one-way contract), accepts every
  Iterable shape (list, set, dict.keys, generator — pin the
  real call-site shape), empty iterable raises with every
  missing key named, single-missing case names the exact wire
  string, error mentions the file + function to fix (developer
  affordance), missing keys listed in sorted order (stable for
  CI diffs). Enum-shape invariants: all values are strings, all
  unique, no empty values.
- 2026-06-28: `AuditLogger` deepened beyond smoke level —
  `tests/test_audit_logger.py` (13 cases) replaces the two
  "doesn't crash" tests in `test_onboarding_and_audit.py`.
  Entry shape: JSON-line format (jq-compatible), ISO-8601
  timestamps with timezone (pinned against epoch-ms drift),
  details omitted when None / nested when set, default status
  is "success". Append behavior: multiple log() calls accumulate
  (never overwrite), constructor preserves existing content
  (open in "a" mode), parent dir auto-created. **OSError
  swallowed** (load-bearing — full-disk / readonly-file must
  NOT crash an in-flight tool call); `_enabled = False`
  kill-switch is a clean no-op. log_blocked uses uppercase
  **"BLOCKED" status** (pinned against case drift that would
  silently miss them in audit grep), reason lands in
  `details.reason` (top-level reserved for canonical keys).
- 2026-06-28: `_format_edit_diff` rows-output covered —
  `tests/test_format_edit_diff.py` (18 cases). The function
  returns `(collapsed_table, expanded_table, rows)`; the
  `rows` element is pure `(display_text, style_string)` data
  testable without rendering Rich. Sentinels: None args / non-
  dict args / both empty → None. Diff opcodes: delete-only
  rows get `-` prefix + red-on-dark-red style, insert-only
  get `+` prefix + green-on-dark-green, **replace emits ALL
  deletes then ALL inserts** (diff convention — not
  interleaved), equal lines unstyled with two-space prefix
  for context-line alignment. Line numbers right-aligned in
  4-char field. start_line auto-detection from file_path
  works when the file already contains `new_string` (history
  re-render path) — **documented limitation pinned: live
  in-flight edits fall back to line 1** because
  `find(new_string)` returns -1 when the file still has the
  pre-edit content. Missing file / new_string not in file
  both fall back cleanly. Colour codes
  (`#ff6b6b on #3d0000` / `#69db7c on #003d00`) pinned so a
  palette refactor must update tests too.
- **2026-06-28: BUG FIX** — `_format_edit_diff` start_line
  computation broken for live edits. The previous iteration
  documented "live in-flight edits fall back to line 1" as a
  quirk; on reflection it's a real bug — every Edit-tool card
  rendered while the edit is still in flight showed the diff
  starting at line 1 regardless of the actual source line.
  Fix: when `find(new_string)` returns -1, **fall back to
  `find(old_string)`** (which IS in the file for live edits).
  Three-tier resolution now: try `new` first (post-edit /
  history re-render), then `old` (live edit), then default
  to 1 (file completely rewritten or missing). The previously-
  documented limitation test now asserts the correct behavior;
  added a new "neither in file" test for the genuine last-
  resort case. This is the **3rd real bug surfaced by
  test-writing this session** (after `spawn_team` TypeError
  crash and FilePill clipboard-write-basename mistake).
  45/45 across format_edit_diff + agno_events tests.
- 2026-06-28: `ToolRegistry.resolve` deepened — existing 3
  happy-path tests joined by 13 edge-case tests in the new
  `TestToolRegistryResolveEdges` class. Comma-string input
  with whitespace + empty-segment filtering; MCP:/Orchestrate/
  Knowledge silently skipped (handled by other subsystems);
  unknown tool raises ValueError with the tool name + a known
  available tool in the message (actionable); denied tool
  silently dropped from the resolved list; **BashOutput→Bash
  canonical dedup** (load-bearing — Agno can't register the
  same toolkit twice); same-name-twice dedup; **`available_tools`
  is a `@property`** (pinned access shape so a refactor to
  method-with-parens surfaces as a deliberate API change);
  `register` round-trip for custom factories; `confirm` arg
  defaults to True for unknown tools (safe default, mirrors
  ``get_level``'s fallback to "ask") and False when permissions
  explicitly allow. 31/31 in test_tool_functions.py (was 18).
- 2026-06-28: `extract_result` summary-computation branch
  covered — `tests/test_extract_result_summary.py` (22 cases).
  The error-detection branch is exhaustively tested in
  `test_tool_error_rendering.py`; this file covers the post-
  diff summary path. **MCP None/null/undefined string
  normalization**: literal "None" / "null" / "undefined"
  strings (from MCP servers ported from JS land) treated as
  empty so the tool card doesn't show a misleading pill;
  contains-substring NOT stripped (`"None of the above"` is
  real content). Single-line summary truncates at exactly
  80 chars + ellipsis (pinned the 80 / 81 off-by-one boundary
  + the exact 83-char length 80+3). Multi-line collapses to
  "N lines of output" (2 lines too — the collapsed card is
  always single-line). Timing suffix appends ", X.XXs" when
  duration present; **two-decimal format pinned** so a
  refactor to `.1f` is deliberate; "completed in <timing>"
  fallback when there's no summary text; **ordering pinned**:
  MCP normalization happens BEFORE the timing branch, so
  "None" + duration → "completed in 0.25s" (NOT "None,
  0.25s"). Sentinels: no-tool event, missing metrics,
  None duration, whitespace strip on result.
- 2026-06-28: `extract_response_text` deepened beyond
  `isinstance(str)` smoke checks — `tests/test_response.py`
  rewritten from 4 to 12 cases. Now pins actual values + the
  dispatch-order priority: str pass-through first (Agno may
  pass plain strings), `.content` attribute next (string
  returned as-is; non-string stringified through `str()`,
  pinned so a future ``if isinstance(content, list):`` branch
  is deliberate; `None` content yields the literal string
  "None" — documented oddity worth knowing about), `.messages`
  fallback walks REVERSED skipping empty/None content (load-
  bearing for runs where the final message is a tool-call
  stub with no text), messages without a ``content`` attr
  skipped via `hasattr` guard, all-empty messages fall
  through to `str(response)`. **Dispatch-order priority
  pinned**: str wins over content, content wins over messages
  — drift would silently change which value the agent sees.
- 2026-06-28: `_resolve_at_path` + code-mask round-trip
  covered — `tests/test_context_helpers.py` (16 cases) pins
  context-helpers' security-critical and round-trip
  invariants. `_resolve_at_path`: ~/abs/relative resolution
  rules; **rejects paths outside `allowed_root` via
  `Path.relative_to`** — security-critical defense against
  `@../../../etc/passwd`-style traversal in rules files;
  rejects absolute paths outside root; missing file → None;
  directory → None; OSError/ValueError swallowed → None.
  `_mask_code_regions` / `_unmask_code_regions`: **round-trip
  identity** (load-bearing — masking then unmasking must
  produce byte-for-byte original); no-code-regions is a noop;
  fenced blocks AND inline backticks both protect their
  `@<path>` tokens from substitution; sentinel format
  `\0CODE<idx>\0` pinned (drift would silently expose `@`
  tokens inside code blocks); originals indexed in appearance
  order; **out-of-range sentinel leaves literal** rather than
  IndexError (defensive). The security check in
  `_resolve_at_path` was previously only indirectly covered
  via integration tests — pinning it directly makes the
  traversal-defense contract explicit.

## Notes

- Rows 5, 14, 15 were originally claimed by the research report as gaps; verification
  against the codebase showed row 5 is already implemented, rows 14/15 are real but
  narrower than the report framed them.
- The hook subsystem accounts for ~6 of the 24 gap rows — single biggest cluster.
- The permission subsystem accounts for ~4 gap rows clustered around the same root
  cause (no permission-mode enum, no structured `permissionDecision` schema).
- Plugin primitives (LSP, monitor, theme, agent-sandbox restrictions, managed scope)
  account for another 5 gap rows.

## Source

Primary sources: code.claude.com/docs/en/{hooks,settings,memory,plugins-reference,
skills,agent-sdk/permissions,agent-sdk/slash-commands,agent-sdk/mcp}. Ember-code
claims cross-checked against `src/ember_code/` and `clients/`.
