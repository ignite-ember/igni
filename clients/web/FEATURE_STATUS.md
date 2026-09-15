# Web UI feature status

Living verification log, maintained by the `/loop` deep-verification
runs. **Verified** = exercised end-to-end against a live BE (real
model where applicable), not just code-reviewed.

States: ✅ verified · 🟡 implemented, not yet deep-verified ·
🔶 partial (known gaps listed) · ❌ broken / missing.

| # | Feature | State | Evidence / gaps |
|---|---------|-------|-----------------|
| 1 | Streaming chat + markdown | ✅ | live run mirrored 22+ deltas; markdown + highlight render |
| 2 | Thinking blocks (`<think>` + Agno events) | ✅ | live (MiniMax-M2.7): 3 collapsed `thinking…` toggles incl. post-tool resume |
| 3 | Tool cards (status/collapse/diff) | ✅ | **diff was broken** — FE typed DiffRow as a struct but BE sends `(text, style)` tuples; table rendered empty. Fixed + live: red/green ± rows w/ line numbers on real edit; cards mirrored to second view |
| 4 | HITL permission dialog (once/always/similar/reject, batch) | ✅ | live: "Allow Write?" + all 4 options; Allow once → tool ran (screenshot a1-hitl) |
| 5 | HITL cross-view dismissal | ✅ | live two-browser e2e: dialog appeared on both views; resolving on A dismissed B's within 8s |
| 6 | Cancel run (stop btn + Esc) | ✅ | live mid-stream: stop button AND Esc each ended processing <20s |
| 7 | Queue while processing | ✅ | live: "Queued — will run after the current turn." shown; queued msg ran after turn |
| 8 | Model dropdown + switch + persistence | ✅ | dropdown verified; BE persistence suite green. 2026-06-11: picker moved from header to a compact chip beside Send; /model routes to the same picker; Esc closes |
| 9 | Sessions sidebar: list/pick/history | ✅ | **was broken** — FE read RPC result as array but BE wraps `{sessions:[…]}`; sidebar was always empty. Fixed + live: past session listed, pick restores clean history |
| 10 | Session pool: per-tab sessions, parallel runs | ✅ | two parallel runs overlapped, stamps clean |
| 11 | Per-session project dirs + lock chip | ✅ | UI click-through: lock → `$ pwd` in locked dir. Chip shows the FULL path (left-truncated for long ones) per user request 2026-06-11 |
| 12 | Directory pickers (native hook / BE dialog / in-app) | 🔶 | in-app verified; BE native dialog untested (opens a real OS dialog — needs a human); Tauri hook unverifiable here (cargo not installed on this machine) |
| 13 | Mirroring: typing, echo, remote runs | ✅ | two-client e2e: typing relay (incl. clear-on-empty), attributed echo, mirrored stream — re-verified after the Esc/icon/panel changes |
| 14 | `<think>` never leaks literally | ✅ | live: clean during streaming; **history-restore path leaked** literal tags — fixed (`restoredItem`) + re-verified |
| 15 | `/` slash menu (builtins + skills) | ✅ | screenshot-verified incl. keyboard nav path. 2026-06-11: TUI-parity command-mode lighting — cyan "/ command" badge + lit slash button while input starts with "/" |
| 16 | `@` file mentions | ✅ | BE FileIndex completions rendered |
| 17 | `$` shell mode | ✅ | `$ pwd` verified in locked dir. 2026-06-11 TUI parity: prefix is consumed into the mode badge (input shows only the body); Backspace on empty exits; history recall re-enters the mode. Same for "/" command mode (#15) |
| 18 | Input history (↑/↓) | ✅ | live: ↑ recalls last, ↑ previous, ↓ walks back to draft |
| 19 | MCP panel: tools + descriptions, connect/disconnect | ✅ | aikido server connected live; 3 tools + descriptions expanded |
| 20 | MCP resources/prompts | ✅ | **implemented 2026-06-11**: `get_resources`/`get_prompts` on MCPClientManager via MCP session; surfaced in `get_mcp_server_details`; web panel renders Resources/Prompts sections. Live vs a fixture FastMCP server: "1 tools · 1 resources · 1 prompts" + uri/mime/args rendered |
| 21 | CodeIndex panel (sync/resync/clean/install, live %) | ✅ | real needs_install state + install link rendered |
| 22 | Agents panel + promote/discard ephemeral | ✅ | **two bugs found+fixed**: (a) FE read `ephemeral` but wire field is `is_ephemeral` — Keep/Discard never rendered; (b) pool never reloaded orphaned `agents.tmp` agents after restart (init_ephemeral now loads them). Live: Keep moved file to .igni/agents, Discard deleted it. Reworked 2026-06-11: list → detail page per agent with "Agents › name" breadcrumb; tools/tags/MCP pills, source path, system prompt rendered as markdown |
| 23 | Skills panel + run | ✅ | live: 6 bundled skills listed (init seeds them). Reworked 2026-06-11 per user request: Run now seeds the composer with "/skill " (focused, caret at end) instead of firing immediately, so arguments can be added |
| 24 | Plugins panel (enable/remove/install/marketplaces) | ✅ | live: list, Disable→Enable, Remove. **Install bug found+fixed** — panel sent the plugin ref as `install_ref` (branch/SHA flag), which would checkout a bogus ref; now omitted. Real round-trip: `@claude-plugins-official/code-simplifier` installed (git clone → listed) then removed cleanly |
| 25 | Knowledge panel (search/add/sync) | ✅ | live: inline add → chroma (local MiniLM embedder), search found the doc, sync wrote .igni/knowledge.yaml |
| 26 | Hooks panel | ✅ | live: seeded project hooks (pre-pr-review/post-commit-todo) rendered via get_hooks_details |
| 27 | Loop panel (pause/resume/stop, auto-refire) | ✅ | **was broken** — FE read `iteration`/`max_iterations` but BE sends `iteration_index`/`iterations_remaining`/`cap_explicit`/`announced_total`. Fixed; live mid-run: "1/30" rendered, Pause→paused, Resume→running, Stop→"No active loop". Note: `/loop N prompt`'s CommandResult returns only after the loop ends — open the panel via bare `/loop` |
| 28 | Schedule panel (list/cancel, pushes) | ✅ | **BE bug found+fixed** — `TaskStore()` fell back to the BE process cwd, so every session's tasks landed in the launcher's project store (leaked fixtures cleaned). Now project-scoped at all 6 call sites. Live: add → exactly 1 listed → cancel → gone |
| 29 | Login/logout flow | 🟡 | wired incl. push notifications; deliberately NOT auto-tested — exercising it would log out the user's real credentials. Needs a manual pass |
| 30 | Update banner | ✅ | render path verified vs stubbed BE (`check_for_update` → available): banner text, release link, dismiss all correct |
| 31 | Compact context | ✅ | live: /compact → "Context compacted. Conversation summarized, history cleared." |
| 32 | Help panel | ✅ | renders builtins + skills |
| 33 | Token stats after runs | ✅ | reworked 2026-06-11 after user feedback: ONE roll-up line per response ("✦ 29.3k in · 402 out · 17.2s") instead of per-LLM-step lines. The old per-step "in" looked wrong (each step re-sends ~14.5k context, so a 2-step turn read 27k+); the number was real billing, the presentation was the bug |
| 34 | Initial history load (resumed BE session) | ✅ | **was broken** — restored user turns showed literal `<system-context>` wrapper, assistant turns leaked `<think>`. Fixed (`restoredItem`, TUI-parity strip + unclosed-tag case) + re-verified live on reload |
| 35 | Responsive narrow mode | ✅ | 420px layout verified |
| 36 | Wide-screen dialogs (not drawers) | ✅ | Agents/Knowledge centered dialogs screenshot-verified |
| 37 | Reconnect resilience (reload, BE restart) | ✅ | transport tests + StrictMode/orphan fixes e2e'd |
| 38 | Crash recovery (pending messages) | ✅ | **was broken** — FE read `p.text` but RPC returns `content`; interrupted prompts never rendered (only the count marker). Fixed + live SIGKILL e2e: restart → sidebar pick → history + interrupted prompt + marker |
| 39 | Session auto-naming (Agno, after first run) | ✅ | new (2026-06-11): BE `maybe_auto_name_session` post-StreamEnd + `session_named` push; live in sidebar. Markdown-wrapped titles ("**…**") found leaking — now sanitized + re-persisted BE-side |
| 40 | New chat lands on welcome hero | ✅ | live: `/clear` no longer appends "New conversation started." — empty list renders hero. Reworked 2026-06-11 to TUI parity: greeting, model · project line, clickable "Why Ember" capability list (8 rows → panels), input hints |
| 41 | SVG icons (no emoji), flame brand, Ember Chat branding | ✅ | live screenshot: landing-page flame in hero/sidebar + favicon; folder/cloud/chevron/menu/close/stop/send SVGs; title + h1 "Ember Chat" |
| 42 | Panels close on Escape | ✅ | **was broken** — close button tooltip said "Close (Esc)" but no handler existed; Drawer now binds capture-phase Esc (and shields the app-level cancel-run Esc). Live-verified |
| 43 | Esc on autocomplete no longer cancels the run | ✅ | **was broken** — Composer's menu-close Escape propagated to the app-level cancel-run handler, so dismissing the slash menu mid-run killed the run. stopPropagation added; live-verified (menu Esc keeps run, bare Esc still cancels) |
| 44 | FE↔BE wire-contract test | ✅ | new: `scripts/dump_wire_schema.py` → `clients/web/src/protocol/wire-schema.json`; 18 vitest cases assert every field the FE reads. Already caught a 5th latent mismatch on landing (SchedulePanel read `schedule`/`next_run`; real fields `scheduled_at`/`recurrence` — fixed) |
| 45 | Response duration + real ctx counter | ✅ | new: `RunCompleted.duration` (Agno run metrics) shown on the stats line; **ctx counter was broken** — `get_status` never computed `context_tokens`/`max_context`, footer always read "ctx 0%". Now counts real context (tokenizer path shared with TUI) and refreshes after every run: live "ctx 9.8k · 5%" |
| 47 | Sidebar shows session ids; no cross-chat event leaks | ✅ | 2026-06-11: short id rendered beside each session name; **bug fixed** — a pre-clear run's late `run_completed` (post-stream tail) could append its stats line into a fresh chat; view-generation guard drops stale events |
| 46 | Connection dot next to brand (tooltip) | ✅ | per user request 2026-06-11: passive "connected" chip removed from header; colored dot beside "Ember Chat" (sidebar + collapsed-header brand) with `backend <state>` tooltip; actionable reconnect chip kept |

Known architectural gaps (BE-level, affect TUI too): MCP
resources/prompts (#20); conversation migration when re-locking a
session's dir; cross-project session list; auto-compact on context
threshold; queue/task side panels; crash restart starts a FRESH
session unless the launcher passes `--resume-session <id>` — web
recovery therefore goes through the sidebar pick, not auto-resume.

2026-06-11 deep-verification runs (Playwright headless Chromium against
fresh BEs on tmp project dirs, real MiniMax-M2.7 runs): promoted
#2–7,14,18,20,22–28,30,31,33,34 to ✅; implemented #20; found + fixed
11 FE/BE bugs: session-list unwrap (#9), history-restore stripping
(#34/#14), pending-message field (#38), DiffRow schema (#3), markdown
leak in auto-names (#39), AgentsPanel `is_ephemeral` field (#22),
orphaned agents.tmp never reloaded (#22), Drawer Esc missing (#42),
autocomplete-Esc cancelling runs (#43), LoopPanel status fields (#27),
TaskStore cwd-scoping (#28). Harness: /tmp/ember-pw/verify{1..16}.mjs +
mcp_e2e.py fixture server. Remaining: #12 (needs human/OS dialog +
cargo not installed here), #29 login/logout (manual only — real
credentials). The wire-field mismatch class (5 of 13 bugs) is now
pinned by the contract test (#44); bug 13 was PluginsPanel passing the
plugin ref as `install_ref`. Post-fix regression sweeps green: chat
round-trip, menu-Esc vs cancel-Esc, HITL+diff, 420px narrow layout
with the SVG icon set, two-view mirroring (typing relay + mirrored
stream), 10-panel open/Esc cycle with zero console errors; vite
production build green; full BE suite 2065 passed.
