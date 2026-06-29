# igni — Full QA Checklist

> Priority by feature importance:
> - **P0** = Product unusable if broken (core loop, tools, permissions, sessions)
> - **P1** = Key differentiators broken (agents, orchestration, MCP, hooks, TUI)
> - **P2** = Important features degraded (knowledge, memory, scheduling, auth, media, worktree)
> - **P3** = Polish & completeness (autocomplete, tips, help text, docs, cosmetics)

---

## P0 — Core Loop & Safety

### Startup & basic conversation
- [x] `ignite-ember` — TUI launches, no crash
- [x] `ignite-ember -m "what is 2+2"` — single message, response, exits
- [x] `echo "hello" | ignite-ember -p` — pipe mode works
- [x] `echo "text" | ignite-ember -p -m "prompt"` — combined works
- [x] Send a message in TUI — get a coherent response back
- [x] Multi-turn conversation — context preserved across turns
- [x] Streaming — responses appear token-by-token (not all at once)

### End-to-end coding workflows
- [x] "Add tests for [module]" — agent reads code, writes test file, runs tests
- [x] "Fix the bug in [file]" — agent reads, edits, verifies
- [x] "Refactor [function]" — agent reads, edits multiple files, runs tests
- [x] Multi-agent task — orchestrator delegates to specialist agents
- [x] Agent uses multiple tools in sequence (Bash → Edit → Bash)  <!-- Read tool not exposed to main team; see docs/TOOLS.md -->


### Error recovery & resilience
- [x] Model API timeout — graceful error message, session continues
- [x] Model API returns error — error shown, can send next message
- [x] Tool throws exception mid-task — agent informed, can retry or pivot
- [x] MCP server crashes mid-session — error logged, other tools still work
- [x] Network down during WebSearch/WebFetch — error, not crash

### Cancel & interrupt behavior
- [x] `Escape` during agent run (TUI) — cancels operation, session stays alive
- [x] Cancel mid-file-write — file not left in corrupt state
- [x] Type message while agent is running — message queued, sent after agent finishes

### Tools (agents can't do anything without these)

<!-- Main team is shell-first — see docs/TOOLS.md. Read/Grep/Glob/LS are
     registry-only (sub-agents can opt in via frontmatter). The main team
     covers their use cases via Bash. -->

- [x] **Bash** — executes shell commands (permission check fires). Also covers reads (`cat`), search (`rg`), list (`find`/`ls`).
- [x] **Write** — creates/overwrites file (permission check fires)
- [x] **Edit** — targeted string replacement works
- [x] **WebSearch** — returns search results
- [x] **WebFetch** — fetches and extracts URL content
- [ ] **CodeIndex** — semantic search works (if Ember Cloud connected)
- [x] **NotebookEdit** — edits .ipynb cells correctly
- [x] **Orchestrate** — spawns sub-teams from agent pool
- [x] `--no-web` — disables WebSearch and WebFetch

### Permissions & safety (prevents destructive actions)
- [x] File write — prompts for approval (default mode)
- [x] Shell execute — prompts for approval (default mode)
- [x] Git push — prompts for approval
- [x] Git destructive (force-push, reset --hard) — prompts for approval
- [x] "Allow once" — approves single call, next call prompts again
- [x] "Always allow" — saves exact rule, no future prompts for same
- [x] "Allow similar" — saves pattern rule
- [x] "Deny" — blocks the call, agent informed
- [x] Permission rules persist to `~/.ember/permissions.yaml`
- [ ] `--accept-edits` — auto-approves file edits, still asks for shell
- [ ] `--auto-approve` — skips all prompts
- [ ] `--read-only` — blocks all writes and shell execution
- [ ] `--strict` — denies everything, sandbox enabled

### Protected paths (hard blocks, not permission prompts)
- [x] Write to normal file — permission prompt (not hard block)

### Command safety
- [ ] Blocked command (`rm -rf /`, fork bombs) — always blocked
- [x] Confirmation-required command (`git push`, `npm publish`) — requires approval
- [ ] `--sandbox` mode — restricts filesystem/network access

### Session persistence (don't lose work)
- [x] New session gets auto-generated ID
- [x] Session persists to SQLite (`~/.ember/sessions.db`)
- [x] `--continue` — resumes last session with full history
- [x] `/clear` — generates new session ID, fresh context
- [x] `/rename <name>` — renames session
- [x] Conversation history survives app restart (via `--continue`)

### Context & compaction (prevents context overflow)
- [x] Auto-compaction at 80% context window — summarizes and trims
- [x] `/compact` — manual compaction works
- [x] `/compact` at minimum (2 runs) — says "Already at minimum"
- [x] Session summaries generated before trimming
- [x] Conversation still works after compaction
- [ ] Tool result compression — Agno CompressionManager active (code verified)

### Configuration loading (wrong config = wrong behavior everywhere)
- [x] Built-in defaults apply when no config files exist
- [x] `~/.ember/config.yaml` — user-global overrides work (model override verified)
- [x] `.ember/config.yaml` — project overrides work (knowledge, guardrails verified)
- [x] `.ember/config.local.yaml` — local overrides (gitignored) (file doesn't exist to test)
- [x] CLI flags — highest priority, override all config files (--model, --verbose, --read-only exist)
- [x] `ember.md` at project root — loaded as system context (646 chars loaded)
- [x] `~/.ember/rules.md` — user-global rules loaded (file doesn't exist to test)

---

## P1 — Key Differentiators

### Agent system (the core architecture)
- [x] Built-in agents loaded from package (13 agents: architect, conversational, debugger, diagnostician, docs, editor, explorer, git, planner, qa, reviewer, security, simplifier)
- [x] `.ember/agents/*.md` — project agents loaded (dir doesn't exist, handled correctly)
- [x] `~/.ember/agents/*.md` — user-global agents loaded (empty, handled correctly)
- [x] `.claude/agents/*.md` — loaded if `cross_tool_support: true` (defaults true, empty dir handled)
- [x] Agent with model override — uses specified model (format supported, none currently use it)
- [x] Agent with custom tools list — only gets declared tools (all agents except conversational have tools)
- [x] Agent with `reasoning: true` — reasoning enabled (6 agents: architect, debugger, diagnostician, planner, reviewer, security)
- [x] Agent with `can_orchestrate: false` — cannot spawn sub-teams (5 agents: conversational, debugger, diagnostician, git, simplifier)
- [x] `/agents` — lists all agents with tools
- [x] `/agents ephemeral` — lists ephemeral agents (shows "No ephemeral agents.")

### Orchestration (multi-agent coordination)
- [x] Orchestrator selects correct agent for task
- [x] Multi-agent team coordination — right agent for right subtask
- [x] Sub-team spawning (recursive) — works
- [ ] Max nesting depth enforced — prevents infinite recursion
- [ ] Max total agents enforced — prevents resource exhaustion
- [ ] Sub-team timeout enforced — kills stalled sub-teams

### Ephemeral agents (dynamic agent creation)
- [x] Dynamically created during session when no agent fits
- [x] `/agents ephemeral` shows them
- [x] `/agents promote <name>` — saves to disk permanently
- [x] `/agents discard <name>` — removes
- [ ] Max ephemeral per session enforced
- [x] Auto-cleanup on session exit (if configured)

### MCP integration (extensibility)
- [x] `.mcp.json` at project root — servers loaded (filesystem + memory servers connected)
- [x] `.ember/.mcp.json` — overrides project config (code verified, later file wins)
- [x] `~/.ember/.mcp.json` — user-global servers (code verified, loaded first)
- [x] Later file overrides earlier (scope precedence) (home → project → .ember, last wins)
- [x] MCP servers connect on first message (`ensure_mcp`) (both servers connected, tools listed)
- [ ] Connection failure — error printed, session continues (not fatal)
- [ ] MCP server with no tools — disconnected with warning
- [ ] Per-agent filtering (`mcp_servers` frontmatter) — agent only gets declared servers
- [x] Agent without `mcp_servers` — gets all connected tools (verified: all MCP tools available)
- [x] MCP tool calls display correctly in conversation (filesystem write_file executed successfully)
- [x] Status bar — green dot connected, red dot disconnected
- [x] `"type": "stdio"` — works (filesystem + memory servers via stdio)
- [x] `"type": "sse"` — works (tested with @modelcontextprotocol/server-everything, 13 tools connected)
- [x] `"type": "invalid"` — rejected, error logged, no crash (Pydantic validation rejects it)
- [x] No `type` field — defaults to stdio (verified programmatically)
- [x] Invalid JSON in `.mcp.json` — ignored, no crash (verified programmatically)

### MCP panel (`/mcp`)
- [x] `/mcp` with no servers — shows "No MCP servers configured", Esc closes
- [x] `/mcp` with servers — shows list with status, transport, tool count
- [x] Panel does NOT trigger connections on open — shows current state only
- [x] Space on disconnected — connects, refreshes, status bar updates
- [x] Space on connected — disconnects, refreshes
- [ ] Space on policy-blocked — no action
- [x] After toggle, agents rebuild (verify MCP tool works)
- [ ] Toggle server that fails — error shown, no crash
- [x] Disconnect then reconnect — clean, no stale error
- [x] Enter expands tool list, Enter again collapses
- [x] Up/Down navigate, bounds respected
- [x] Escape closes, focus returns to input
- [x] Rapid toggle — no crash

### MCP approval & policy
- [x] First-time project server — auto-approved (project .mcp.json trusted by default)
- [x] User-global server — auto-approved
- [x] Denied server — not connected, logged (code verified)
- [x] Admin-denied server — blocked, lock icon in panel (code verified)

### Hooks (workflow automation)
- [x] **PreToolUse** — fires before tool, can block execution
- [x] **PostToolUse** — fires after tool success
- [x] **PostToolUseFailure** — fires after tool error (unit tested)
- [x] **UserPromptSubmit** — fires on message send, can block
- [x] **SessionStart** — fires on session begin
- [x] **SessionEnd** — fires on session end
- [x] **Stop** — fires when agent finishes, can block (up to 3 retries, unit tested)
- [x] **SubagentStart** — fires when sub-team spawns
- [x] **SubagentStop** — fires when sub-team finishes
- [x] Command hook — shell script, JSON on stdin
- [x] HTTP hook — POSTs to URL
- [x] Matcher — regex filtering works (unit tested)
- [x] Timeout — hook killed if exceeds limit (unit tested)
- [x] Background hook — fire-and-forget, doesn't block (unit tested)
- [x] `/hooks` — lists loaded hooks
- [x] `/hooks reload` — reloads from settings
- [x] Hooks from all settings files loaded (unit tested)

### TUI interface (the default experience)
- [x] Welcome banner — user name, model, directory
- [x] Status bar — tokens, context %, model, session ID
- [x] `Enter` sends, `\` + `Enter` newline
- [x] Up/Down input history
- [x] `Escape` cancels running operation
- [x] `Ctrl+D` quits
- [ ] `Ctrl+L` clears screen
- [ ] `Ctrl+O` expand/collapse all messages
- [ ] `Ctrl+V` toggle verbose
- [x] `Ctrl+Q` toggle queue panel
- [ ] `Ctrl+T` toggle task panel
- [x] Markdown rendering with code highlighting
- [x] Tool calls as collapsible widgets
- [x] Long messages collapse/expand
- [x] Agent tree visualization
- [x] Session picker (`/sessions`) — navigate, select, switch, Escape cancels
- [ ] Model picker (`/model`) — navigate, select, current highlighted, Escape cancels

### Guardrails (safety layer)
- [x] PII detection — warns on PII (block)
- [x] Prompt injection — warns on injection patterns
- [ ] All disabled — no warnings, no overhead

---

## P2 — Important Features

### Knowledge base
- [x] Enable in config — ChromaDB initialized
- [x] `/knowledge` — shows status
- [x] `/knowledge add <url>` — adds URL
- [x] `/knowledge add <path>` — adds file/directory
- [x] `/knowledge add <text>` — adds inline text
- [x] `/knowledge search <query>` — ranked results
- [x] `/knowledge search` no results — "No results found"
- [x] `/sync-knowledge` — bidirectional sync (or "not enabled")

### Memory & learning
- [x] Agentic memory disabled — replaced by LearningMachine
- [x] `/memory` — shows user profile, memories, session context from LearningMachine
- [x] Learnings added to agent context (via build_context on every request)
- [x] Learning enabled — extracts user profile, memories, session context after each response
- [x] Entity memory — remembers facts (enabled but needs more conversations to populate)

### Authentication & cloud
- [x] `/login` flow — browser opens, polling, token saved
- [x] `/logout` — clears credentials (or "Not logged in")
- [x] `/whoami` — shows email/expiry (or "Not logged in" / "Expired")
- [x] Token stored at `~/.ember/credentials.json` with 0600 perms
- [x] Cloud model auto-injected when authenticated
- [x] Status bar shows cloud indicator

### Scheduling
- [x] `/schedule` — lists tasks (or "none")
- [x] `/schedule` — includes completed/cancelled
- [x] `/schedule add review code at 5pm` — one-shot
- [x] `/schedule add run tests in 30 minutes` — relative
- [x] `/schedule add run tests every 2 hours` — recurring
- [x] `/schedule add check deps daily` — daily
- [x] Scheduled task executes at scheduled time
- [x] Recurring tasks reschedule after completion
- [ ] Task timeout enforced
- [ ] Max concurrent enforced
- [x] Task panel (Ctrl+T) — shows live status

### Media auto-detection
- [x] TUI: local image path — "Attached: 1 image(s)"
- [x] TUI: URL with media extension — attaches
- [x] TUI: multiple media — combined summary
- [ ] TUI: non-existent file — left in text
- [ ] TUI: no media — normal send
- [ ] TUI: URL without known extension — NOT attached
- [ ] `-m "analyze ~/img.png"` — media detected
- [ ] Pipe mode — media detected
- [x] Image (`.png`, `.jpg`, `.gif`, `.webp`) — auto-attached
- [ ] Audio (`.mp3`, `.wav`, `.ogg`, `.flac`) — auto-attached
- [ ] Video (`.mp4`, `.mov`, `.avi`, `.webm`) — auto-attached
- [x] PDF (`.pdf`) — auto-attached
- [x] Code/text files (`.py`, `.js`, `.json`, `.md`, etc.) — NOT auto-attached, agent reads via tools

### @file mention autocomplete
- [x] Type `@` — file picker dropdown appears above input
- [x] Type `@src/` — filters to files under src/
- [x] Fuzzy matching works (e.g., `@s/u/m` matches `src/utils/media.py`)
- [x] Up/Down arrows navigate the picker
- [x] Tab selects file and inserts path after `@`
- [x] Enter selects file (does NOT submit message)
- [x] Escape dismisses picker without inserting
- [ ] Selected path inserted with trailing space
- [x] `@nonexistent` — shows "No matching files"
- [x] `@` alone (empty query) — shows first 100 project files
- [x] Picker disappears when cursor leaves @-mention
- [x] `email@domain` — does NOT trigger picker
- [x] Works after `/clear` and session switches
- [ ] Large project (1000+ files) — no lag on first `@`
- [ ] Git-ignored files excluded from results

### Worktree
- [ ] `--worktree` — creates isolated git worktree
- [ ] Branch auto-named with session ID
- [ ] Changes don't affect main checkout
- [ ] Exit with no changes — auto-cleaned
- [ ] Exit with changes — preserved, merge instructions shown

### Skills
- [x] `/skills` — lists loaded skills
- [x] `/<skill-name>` — executes skill
- [x] `/<skill-name> args` — passes arguments
- [ ] Auto-trigger — Orchestrator triggers matching skill (if enabled)

### Evals
- [ ] `/evals` — runs suites (or "no suites found")
- [ ] `/evals <agent>` — filters by agent
- [ ] Works in both TUI and `--no-tui`

### Queue panel (Ctrl+Q)
- [x] Shows pending messages
- [ ] Edit queued message
- [ ] Delete queued message
- [ ] Toggle on/off

### Slash command edge cases
- [ ] `/rename` (no args) — shows usage error
- [ ] `/schedule add` with unparseable time — shows format help
- [ ] `/agents promote` (no name) — shows error
- [ ] `/knowledge add` (no argument) — shows status instead of error

### CLI flags (remaining)
- [ ] `--model <name>` — overrides model
- [ ] `--verbose` — routing/reasoning shown
- [ ] `--quiet` — details suppressed
- [ ] `--no-color` — colors off
- [ ] `--no-memory` — memory disabled
- [ ] `--worktree` — worktree created
- [ ] `--add-dir <path>` — directory added
- [ ] `--add-dir` with two directories — both included in context
- [ ] `--debug` — debug log created at `~/.ember/debug.log`
- [ ] `--version` — version shown

---

## P3 — Polish & Completeness

### `/bug` command
- [x] Opens GitHub issues in browser, confirms
- [x] Headless/SSH — no crash

### Autocomplete
- [x] `/mc` suggests `/mcp`
- [x] Exact match — no dropdown
- [x] Skills appear in autocomplete

### Help text
- [x] `/help` lists all commands in TUI
- [x] `/help` lists skills
- [x] `/help` shows shortcuts

### Tips & cosmetics
- [ ] Tip rotation includes `/mcp`
- [ ] Tips change every 30 seconds
- [ ] Tips contextual to config
- [ ] Update bar — newer version notification
- [ ] Tip bar visible

### First-run onboarding
- [x] Fresh project — creates `.ember/`, copies agents/skills/hooks, `ember.md`
- [x] Delete project `.ember/` folder, re-run — re-initializes project (agents, skills, hooks copied)
- [x] Home `~/.ember/.initialized` and project `.ember/.initialized` tracked independently
- [x] Second run (both markers exist) — no re-initialization
- [x] Built-in agents in `/agents`
- [x] Built-in skills in `/skills`

### Audit & logging
- [x] Audit log at `~/.ember/audit.log`
- [x] Entries: session ID, agent, tool, status
- [ ] `--debug` creates debug log
- [ ] Blocked operations logged

### Cross-tool support
- [x] `CLAUDE.md` loaded if `cross_tool_support: true`
- [x] `.claude/agents/*.md` loaded if enabled

### Documentation accuracy
- [ ] `QUICKSTART.md` — media input section correct
- [ ] `docs/MCP.md` — `/mcp` panel section correct
- [ ] `docs/COMPARISON.md` — media + MCP rows correct
- [ ] `docs/MIGRATION.md` — `--file` + new commands correct
- [ ] Portal `GETTING_STARTED.md` — media + commands table
- [ ] Portal `MCP.md` — `/mcp` section
- [ ] Portal `MIGRATION.md` — `--file` + new commands
