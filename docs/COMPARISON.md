# Ember Code vs Claude Code — Comparison Analysis

A thorough feature-by-feature comparison between Ember Code and Claude Code.

## 1. Architectural Philosophy

| Aspect | Claude Code | Ember Code |
|---|---|---|
| **Core pattern** | Single-agent loop with optional sub-agents | Multi-agent team orchestration (Agno framework) |
| **Execution model** | One context window, one agent decides everything | Orchestrator meta-agent assembles purpose-built teams per task |
| **Framework** | Custom Anthropic runtime | Agno (`agno-agi/agno`) — open-source agent framework |
| **Language** | TypeScript/Node.js | Python 3.10+ |

Claude Code is a **monolithic agent** — one powerful Claude model handles routing, planning, execution, and review in a single conversation loop. It can spawn sub-agents, but only one level deep, and the parent must explicitly decide when and what to delegate.

Ember Code inverts this: an **Orchestrator** analyzes every task, picks agents from a pool, selects a team mode, and assembles a disposable team. Any agent can spawn sub-teams recursively (default depth: 5, configurable).

---

## 2. Agent System

| Aspect | Claude Code | Ember Code |
|---|---|---|
| **Agent format** | `.md` files with YAML frontmatter | Same `.md` format (cross-compatible) |
| **Built-in agents** | ~3 (main, sub-agent, plan mode) | 13 specialized agents (explorer, editor, planner, architect, reviewer, security, qa, debugger, simplifier, git, conversational, diagnostician, docs) |
| **Custom agents** | `.claude/agents/*.md` | `.ember/agents/*.md` + `.ember/agents.local/` (gitignored) |
| **Agent discovery** | Static directory scan | Multi-directory scan with priority (ephemeral → local → project → global → built-in) |
| **Sub-agent depth** | 1 level | Unlimited (configurable via `orchestration.max_nesting_depth`) |
| **Ephemeral agents** | No | Yes — Orchestrator auto-generates session-scoped agents when no existing agent fits |
| **Agent extensions** | `name`, `description`, `tools`, `model` | All Claude Code fields + `tags`, `reasoning`, `reasoning_min/max_steps`, `can_orchestrate`, `temperature`, `max_turns`, `color`, `mcp_servers` |
| **Cross-compat** | N/A | Scans `.claude/agents/` by default (cross-tool support on) |

### Team Modes (Ember Code only)

| Mode | Behavior | Example |
|---|---|---|
| **Route** | Pick one agent, pass through | "What does this function do?" → Explorer |
| **Coordinate** | Sequential multi-agent pipeline | "Add endpoint with tests" → Planner → Editor → QA |
| **Broadcast** | Parallel independent perspectives | "Review for security + performance" → Security + Reviewer |
| **Tasks** | Autonomous multi-step with iteration | "Migrate test suite" → decomposes and iterates |

Claude Code has no equivalent — it's always "single agent decides everything, optionally spawns one sub-agent."

---

## 3. Tool System

| Tool | Claude Code | Ember Code | Notes |
|---|---|---|---|
| Read | Yes | Yes (FileTools) | Same concept |
| Write | Yes | Yes (FileTools) | Same concept |
| Edit | Yes (targeted string replacement) | Yes (EmberEditTools) | Same approach — requires prior Read |
| Bash | Yes | Yes (ShellTools) | Same concept |
| Grep | Yes (ripgrep-based) | Yes (GrepTools, ripgrep) | Same backend |
| Glob | Yes | Yes (GlobTools) | Same concept |
| WebSearch | Yes (limited) | Yes (DuckDuckGo) | Ember uses DDG |
| WebFetch | Yes | Yes (httpx) | |
| LS | No (uses Bash) | Yes (FileTools) | Dedicated tool |
| Python | No | Yes (Agno PythonTools) | Execute Python inline |
| CodeIndex | No | Yes (semantic search) | Ember Cloud feature |
| Schedule | No | Yes (ScheduleTools) | Background task scheduling |
| Orchestrate | No (implicit via Agent tool) | Yes (OrchestrateTools) | Explicit `spawn_agent()`, `spawn_team()` |
| NotebookEdit | Yes | Yes (NotebookTools) | Read, edit, add, remove cells |
| TodoRead/Write | Yes | No (uses Tasks mode) | Different approach |

### Tool Permissions

| Aspect | Claude Code | Ember Code |
|---|---|---|
| **Permission model** | Pattern-based allow/deny lists per tool | Category-based tiers (allow/ask/deny) per capability |
| **Syntax** | `"allow": ["Bash(npm run *)"]` | `permissions.shell_execute: "ask"` |
| **Protected paths** | `"deny": ["Read(.env)"]` | `safety.protected_paths: [".env", "*.pem", "*.key"]` |
| **Blocked commands** | Implicit | Explicit `safety.blocked_commands` list |
| **Approval modes** | Yes/No per invocation | once / always / similar-pattern / deny |
| **Audit trail** | No | Yes (`~/.ember/audit.log`) |

---

## 4. LLM & Model Support

| Aspect | Claude Code | Ember Code |
|---|---|---|
| **Default model** | Claude Sonnet 4.6 (Anthropic) | MiniMax M2.7 (Ember hosted) |
| **Provider lock-in** | Anthropic only (Claude models) | Any OpenAI-compatible provider |
| **BYOM** | No (Anthropic, Bedrock, or Vertex only) | Yes — full registry with provider, URL, key per model |
| **API key sources** | `ANTHROPIC_API_KEY` env var | `api_key` (direct) / `api_key_env` (env var) / `api_key_cmd` (shell command, e.g. 1Password) |
| **Per-agent models** | No (global model selection) | Yes — each agent `.md` specifies its own `model:` field |
| **Model resolution** | Alias map (string → string) | Registry lookup (name → provider + URL + model_id + key) |
| **Hosted endpoint** | `api.anthropic.com` | `api.ignite-ember.sh` |
| **Supported providers** | Anthropic, AWS Bedrock, Google Vertex | MiniMax, OpenAI, Anthropic, Groq, Together, OpenRouter, Ollama, any OpenAI-compatible |

---

## 5. UI & Interface

| Aspect | Claude Code | Ember Code |
|---|---|---|
| **Framework** | Ink (React for CLI) | Textual (Python TUI) + Rich |
| **Default mode** | Interactive CLI | Full TUI with panels and widgets |
| **Fallback mode** | N/A | `--no-tui` for plain Rich CLI |
| **Streaming** | Yes (token-by-token) | Yes (Agno native streaming) |
| **Task visualization** | Todo lists in output | Live `TaskProgressWidget` with status icons |
| **Session browser** | `--resume`, `--continue` | `/sessions` with interactive picker widget |
| **Token tracking** | Cost display (`/cost`) | Real-time status bar (context/completion tokens) |
| **Cloud indicator** | N/A | `☁ {org_name}` in status bar when connected |
| **Cancellation** | Ctrl+C (exit) | Escape (cancel current agent, stay in session) |
| **Media input** | `--file` flag or image paste | Auto-detect file paths and URLs in message text |
| **Keyboard shortcuts** | Standard | Extended (Ctrl+O collapse, Ctrl+V verbose, Ctrl+Q queue panel) |

---

## 6. Context & Rules System

| Aspect | Claude Code | Ember Code |
|---|---|---|
| **Project instructions** | `CLAUDE.md` (root + subdirectories) | `ember.md` (root + subdirectories) + optionally `CLAUDE.md` |
| **User-level rules** | `~/.claude/CLAUDE.md` | `~/.ember/rules.md` |
| **Hierarchy** | User → Root → Subdirectory (automatic) | User → Root → Subdirectory (automatic) |
| **Subdirectory rules** | Walk from working file up to root | Same walk-up approach |
| **Cross-tool compat** | N/A | Reads `CLAUDE.md` files by default (cross-tool support on) |
| **Config file** | `~/.claude/settings.json` (JSON) | `~/.ember/config.yaml` (YAML) with 5-layer merge |
| **Config hierarchy** | User → Project → Local | Defaults → User → Project → Project Local → CLI flags |

---

## 7. Memory & Persistence

| Aspect | Claude Code | Ember Code |
|---|---|---|
| **Memory model** | File-based (`MEMORY.md` index + `.md` files) | DB-backed (Agno Memory, SQLite default) |
| **Session storage** | JSONL transcripts | SQLite (`~/.ember/sessions.db`) |
| **Cross-device sync** | No (local files only) | Yes — set `storage.backend: postgres` |
| **Memory types** | user, feedback, project, reference (manually categorized) | User memory + session context + entity memory (Agno LearningMachine) |
| **Learning** | No | Yes — Agno builds user profiles, entity memory across sessions |
| **Context compaction** | Automatic at context limit | Automatic at 80% context window |
| **Session summaries** | Yes (compressed) | Yes (auto-generated by Agno) |

---

## 8. MCP (Model Context Protocol)

| Aspect | Claude Code | Ember Code |
|---|---|---|
| **As MCP Server** | Yes (stdio) | Yes (stdio) |
| **As MCP Client** | Yes (consume external servers) | Yes (consume external servers via Agno MCPTools) |
| **Config file** | `.mcp.json` | `.mcp.json` (same format, same location) |
| **Transports** | stdio, SSE | stdio, SSE |
| **IDE integration** | Manual setup | Agents declare `mcp_servers` in their `.md` file |
| **Exposed tools** | Full tool suite | Read, Write, Edit, Bash, Grep, Glob, ListDir, dispatch_agent |
| **Runtime management** | N/A | `/mcp` panel — browse servers, toggle on/off mid-session |

---

## 9. Skills

| Aspect | Claude Code | Ember Code |
|---|---|---|
| **Format** | `SKILL.md` with frontmatter | Same format (cross-compatible) |
| **Invocation** | `/skill-name [args]` | `/skill-name [args]` |
| **Auto-trigger** | No | Yes — Orchestrator recognizes intent and loads matching skill |
| **Execution modes** | Inline only | Inline (default) or forked (`context: fork` runs isolated sub-agent) |
| **Built-in skills** | N/A | `/commit`, `/explain`, `/resolve-issues`, `/simplify`, `/update-docs` |
| **Progressive disclosure** | Full load | Metadata always loaded (~100 words), body loaded on invoke |
| **Cross-compat** | N/A | Scans `.claude/skills/` by default (cross-tool support on) |

---

## 10. Safety & Guardrails

| Aspect | Claude Code | Ember Code |
|---|---|---|
| **Permission model** | Pattern-based per-tool | Category tiers (allow/ask/deny) |
| **Command safety** | macOS sandbox-exec, Linux containers | Blocked patterns, confirmation prompts |
| **PII detection** | No | Yes (Agno pre-hook, opt-in) |
| **Prompt injection** | Flags suspicious tool results | Yes (Agno pre-hook, opt-in) |
| **Content moderation** | No | Yes (OpenAI moderation API, opt-in) |
| **Audit logging** | No | Yes (`~/.ember/audit.log`) |
| **Protected paths** | Via deny rules | Dedicated `safety.protected_paths` list |
| **Blocked commands** | Implicit | Explicit `safety.blocked_commands` list |
| **HITL** | Implicit (permission prompts) | Explicit — agents can pause for confirmation or user input via HITLHandler |

---

## 11. Features Unique to Each

### Claude Code Only

| Feature | Notes |
|---|---|
| **Effort levels** (`--effort low/high`) | Token budget control |
| **Anthropic model optimization** | Deep integration with Claude's extended thinking, caching |

### Ember Code Only

| Feature | Notes |
|---|---|
| **Git worktrees** (`--worktree`) | Isolated parallel sessions on separate branches (also in Claude Code) |
| **Multi-workspace** (`--add-dir`) | Work across multiple directories/repos simultaneously (also in Claude Code) |
| **NotebookEdit** | Read, edit, add, remove Jupyter notebook cells (also in Claude Code) |
| **Dynamic team assembly** | Orchestrator auto-picks agents and mode per task |
| **4 team modes** | Route, Coordinate, Broadcast, Tasks |
| **Unlimited agent nesting** | Recursive sub-teams (configurable depth) |
| **Ephemeral agents** | Auto-generated session-scoped agents |
| **CodeIndex** | Semantic code intelligence (6 analysis categories) |
| **Knowledge base** | ChromaDB vector store with custom embeddings |
| **Agno LearningMachine** | User profiles, entity memory across sessions |
| **Guardrails** | PII detection, prompt injection, moderation |
| **Task scheduling** | Background jobs with recurrence and bounded concurrency |
| **Task visualization** | Live TUI widget with status icons |
| **Agent evals** | Built-in regression testing for agent definitions |
| **First-run onboarding** | Automatic first-run setup proposes project-specific agents |
| **Model agnostic** | Any provider via BYOM registry |
| **Per-agent model** | Each agent can use a different model |
| **Skill auto-trigger** | Orchestrator loads skills by intent match |
| **Cross-device sync** | Remote storage backends (Postgres, etc.) |

---

## 12. Cross-Compatibility Summary

| Asset | Claude Code → Ember Code | Ember Code → Claude Code |
|---|---|---|
| Agent `.md` files | Works when `agents.cross_tool_support: true` | Works (extensions ignored) |
| Skills | Works when `skills.cross_tool_support: true` | Works (extensions ignored) |
| `CLAUDE.md` | Works when `rules.cross_tool_support: true` | N/A |
| `.mcp.json` | Works as-is (same format) | Works as-is |
| Hooks | Same event names & JSON format | Same format |
| Memory | Not portable (different backends) | Not portable |
| Config | Not portable (YAML vs JSON, different schema) | Not portable |

---

## 13. Summary

**Claude Code** excels as a **single-agent powerhouse** — deeply optimized for Anthropic's models, with tight IDE integration (VS Code extension, worktrees), and a simple mental model. Its strength is that one very capable model handles everything in one context window.

**Ember Code** takes a **multi-agent orchestration** approach — instead of one agent doing everything, specialized agents collaborate in dynamically assembled teams. This adds complexity but provides: model flexibility (any provider), deeper code intelligence (CodeIndex), persistent learning (Agno Memory), and recursive delegation. The tradeoff is coordination overhead for simple tasks where a single capable agent would suffice.

The cross-compatibility design (`cross_tool_support` flags) means teams can adopt Ember Code incrementally without discarding Claude Code assets.
