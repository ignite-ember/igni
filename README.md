# Ember Code

**One spark ignites a team.** An AI coding assistant built with [Agno](https://github.com/agno-agi/agno) orchestration.

[ignite-ember.sh](https://ignite-ember.sh)

Inspired by [Claude Code](https://github.com/anthropics/claude-code), Ember Code is a terminal-based coding agent that assembles specialized AI teams on the fly. Describe your task — the Orchestrator picks the right agents, the right team mode, and runs them.

## Why Ember Code?

Claude Code uses a single agent loop — powerful but monolithic. Ember Code takes a different approach: **dynamic multi-agent orchestration**. Instead of one agent doing everything, Agno's team system decomposes tasks, routes them to specialized agents, and synthesizes results — all automatically.

### The numbers

Head-to-head benchmark on a 12-case software-engineering suite, 5 runs per system, deterministic grading. See [docs/BENCHMARKS.md](docs/BENCHMARKS.md) for the full breakdown.

| | Ember Code (MiniMax-M2.7) + CodeIndex | Claude Code (Opus-4.7) |
|---|---:|---:|
| Directly mergeable (✅) | **49 / 60 (82 %)** | 31 / 60 (52 %) |
| Needs clarification (⚠) | **4 / 60 (7 %)** | 22 / 60 (37 %) |
| Wrong target (❌) | 7 / 60 (12 %) | 7 / 60 (12 %) |
| Reproducibly correct cases (5/5 ✅) | **8 of 12** | 3 of 12 |
| Cost per run | **~$0.05** | $4.01 |
| Wall time mean | 2 331 s | **1 548 s** |

**Ember Code wins by +18 ✅ trials at 1/80th the cost.** The gap is concentrated in the ⚠ band — Ember Code commits to a concrete answer where Claude Code stays in design-conversation mode. Both systems hit the same hard-wrong rate (12 %), so the win comes from converting partial/clarifying responses into directly-mergeable ones.

The architectural choices that drive the gap: (1) **CodeIndex queried first, not files** — a pre-built semantic + metadata index lets the agent locate reuse targets and conventions by typed filter or HyDE-style code-shaped query, instead of grep-walking the repo. (2) **A mandatory "What already exists" preamble** — agent must name the reuse target, the closest near-miss it rejected, and the conventions to match before writing any code. (3) **A small, focused model with structured tools** — MiniMax-M2.7 + index access matches Opus-4.7 quality at 1/80th the price.

### Feature comparison

| Feature | Claude Code | Ember Code |
|---|---|---|
| Architecture | Single agent loop | Multi-agent teams (Agno) |
| Task routing | Manual sub-agent spawning | Automatic via Coordinate/Route modes |
| Code intelligence | Grep + file reads | CodeIndex semantic search (included free) |
| Knowledge base | None | ChromaDB vector store with custom embeddings |
| Planning | Plan mode (read-only) | Agno reasoning + Tasks mode |
| IDE integration | MCP server (stdio) | MCP server + client (Agno MCPTools) |
| Extensibility | Plugins, hooks, MCP | Agents + hooks + toolkits + MCP |
| Agent evals | Not built-in | Built-in regression testing framework |
| Memory | File-based MEMORY.md | Agno Memory + DB-backed storage |
| Learning | None | Agno LearningMachine (user profiles, entity memory) |
| Guardrails | None | PII detection, prompt injection, moderation |
| HITL | Implicit | Explicit confirmation/input requirements |
| Default model | Anthropic Claude | MiniMax M2.7 (model-agnostic, swappable) |

## Quick Start

```bash
brew install ignite-ember/tap/ignite-ember  # or: pip install ignite-ember
ignite-ember /login              # sign up for hosted models (MiniMax M2.7)
ignite-ember                     # start coding
```

Or bring your own model (OpenAI, Anthropic, Groq, Ollama, etc.):

```bash
export OPENAI_API_KEY=sk-...
```

```yaml
# .ember/config.yaml
models:
  default: gpt-4o
  registry:
    gpt-4o:
      provider: openai_like
      model_id: gpt-4o
      url: https://api.openai.com/v1
      api_key: sk-...              # direct key in config
      # api_key_env: OPENAI_API_KEY  # or from env var
      # api_key_cmd: "op read ..."   # or from shell command
```

See [Quickstart](QUICKSTART.md) for the full setup guide.

### Quickstart (new setup)

1. **Install** — `brew install ignite-ember/tap/ignite-ember` (or `pip install ignite-ember`)
2. **Authenticate** — `ignite-ember /login` for hosted models, or set `OPENAI_API_KEY` + add model to `.ember/config.yaml` for your own
3. **Run** — `ignite-ember`

See [Quickstart](QUICKSTART.md) for the full guide.

## Upgrading

**v0.5.0** is the "Beat Claude Code" release. Cumulative changes since v0.3.8:

### Benchmarks vs Claude Code (new in v0.5.0)

- **Head-to-head benchmark suite.** 12 cases × 5 runs vs Claude Code on the same target codebase. Programmatic deterministic grading. Ember Code wins **49 / 60 ✅** vs **31 / 60 ✅** at one-eightieth of the per-run cost. Full breakdown in [docs/BENCHMARKS.md](docs/BENCHMARKS.md).
- **CodeIndex-first specialist agents.** New `*.codeindex.md` variants for `architect`, `debugger`, `explorer`, `reviewer`, `security`, `simplifier` — each with prompts tuned to query the index first instead of grepping. Auto-selected when a populated CodeIndex exists for the current commit.
- **Mandatory "What already exists" preamble.** The main-agent prompt now requires every code-write response to start with a four-bullet section naming (a) the reuse target, (b) the closest near-miss it considered and rejected, (c) the conventions to match, (d) the parallel infrastructure it will *not* introduce. Forces contrastive reasoning before code is written.
- **Encapsulation rule.** When a service class wraps a resource (db client, cache, queue, storage), new code must call methods on that class instead of inlining the raw client. Catches the "copy a private prefix into a new file and instantiate the client myself" anti-pattern.

### Plan-and-Align workflow (v0.4.2)

- **HITL approval for complex action work.** Main agent now plans first and waits for user approval before executing non-trivial tasks. Read-only review/audit work bypasses this and goes direct to the agent loop.
- **"Working with the User" framing.** Prompt restructured around "the human owns the bigger picture" — agent surfaces trade-offs, asks when uncertain, takes pushback seriously instead of defending its plan.
- **Strengthened safety rules.** Explicit handling for `.env` files, raw SQL, destructive shell commands, and blind-edit prevention.
- **Eval framework rebuilt.** Per-case Session isolation, per-case timeout overrides, `tool_arg_assertions` (e.g. asserts `spawn_team(mode="...")`), `prior_messages` for multi-turn cases, work-dir-aware file assertions. New eval suites for bulk_edits, file_safety, mcp, team_modes, team_tasks, knowledge_corner, knowledge_proactive, memory_knowledge, multiturn, error_recovery, long_running, schedule, web_discipline, background_notifications.

### Async shell + background notifications (v0.4.1)

- **Pure async shell tool.** `run_shell_command` / `read_process_output` / `watch_process` / `stop_process` / `list_processes` are now `async def` using `asyncio.create_subprocess_exec`. Previously blocked the event loop for up to the timeout (and a hard 3s on every `background=True` call), starving the HITL multiplexer drain and the FE message stream.
- **Background-process completion notifications.** When a backgrounded shell command finishes, the agent receives a queued `BACKGROUND PROCESS COMPLETED` notice with the output tail; the TUI also gets a `PushNotification`.
- **Idempotent `read_process_output`.** Multiple reads with different tails work; eviction moved to a 10-min TTL after the most recent read.
- **Schedule eval suite.** 12-case suite verifies the agent reaches for `Schedule` exactly when the user describes deferred/recurring work — and not otherwise. Achieves 12/12.

### Sub-agent HITL + run resumption (v0.4.0)

- **Sub-agent HITL bridge.** Specialist sub-agents (e.g. architect) can now request user confirmation through the same TUI flow as the main agent. Multiplexer was extracted from `run_message` and `resolve_hitl` so parent pauses no longer drop the specialist's pause requirements.
- **`acontinue_run` "No runs found" fix.** Session DB is now threaded into pool specialists and the `Team(...)` constructor so paused runs are persisted and can be resumed.
- **Concurrent-spawn race fix.** Per-spawn `copy.copy()` of pool specialists in `spawn_agent` / `spawn_team`. Without this, two concurrent spawns of the same specialist raced on shared `Agent` per-run state and Agno errored with "No runs found for run ID".
- **BE/TUI cleanup robustness.** Signal handlers, `atexit`, process-group kill, and an `EMBER_PARENT_PID` watchdog. Closes the runaway-BE failure mode where the TUI crash left a zombie backend.
- **`httpx` timeout actually applied.** Was previously shadowed by the SDK timeout when an `http_client` was provided.
- **Eval harness.** Auto-approve HITL drain, `--case-timeout` (default 60 s), `--case-retries` (default 2), `--spawn-timeout`, `retries=0` for determinism, WebSearch stub, ENV vs FAIL output classification, default concurrency 5.
- **New tests.** `test_subagent_hitl_e2e` (8 cases, bridge in isolation), `test_orchestrate_real_agno` (real Agno Agent + AsyncSqliteDb).
- **1363 tests pass.**

### Earlier v0.3.x fixes still relevant

**v0.3.8** includes the following changes:

- **Problem:** Escape key did not close the MCP panel (`/mcp`) or the task panel.
  - **Solution:** Added all dialog/panel widgets to the Escape handler, including MCPPanelWidget and TaskPanel.

**v0.3.7** includes the following changes:

- **Problem:** Edit/Write/Bash tools never asked for user confirmation, even when permissions were set to "ask".
  - **Solution:** `requires_confirmation` flag is now set on each function after registration in all custom toolkits.

**v0.3.6** includes the following changes:

- **Problem:** Debug logs grow unbounded and can fill up disk space.
  - **Solution:** Switched to RotatingFileHandler with 10MB limit and 2 backup files.

- **Problem:** Shell command output can be extremely large, causing issues when sending to the LLM.
  - **Solution:** Truncate tool results to 30,000 characters, keeping start and end of output.

- **Problem:** TUI escape key only closes the login dialog, leaving other panels open.
  - **Solution:** Now closes any open panel: login, help, model picker, or session picker.

- **Problem:** Learning features get stuck and never complete (caused by httpx connection pool issues in threads).
  - **Solution:** Run learning extraction as an async task on the main event loop instead of a separate thread.

To upgrade Ember Code to the latest version:

```bash
brew upgrade ignite-ember/tap/ignite-ember
```

If `brew upgrade` doesn't work or you encounter issues, manually reinstall:

```bash
brew uninstall ignite-ember && brew untap ignite-ember/tap && brew tap ignite-ember/tap && brew install ignite-ember
```

## TUI Mode

`ignite-ember` launches the Textual-based terminal UI by default. The backend runs as a separate process, connected via Unix socket.

Features: streaming responses, agent tree visualization, token tracking, session picker, keyboard shortcuts, HITL confirmation dialogs.

## IDE Integration

Ember Code integrates with IDEs via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/):

```json
{
  "mcpServers": {
    "ignite-ember": {
      "type": "stdio",
      "command": "ignite-ember",
      "args": ["mcp", "serve"]
    }
  }
}
```

Works with **VS Code**, **JetBrains** (IntelliJ, PyCharm, etc.), **Cursor**, and **Windsurf**. See [MCP docs](docs/MCP.md) for details.

## Key Features

### Knowledge Base

Built-in vector knowledge base powered by ChromaDB and the Ember embeddings API:

```yaml
knowledge:
  enabled: true
  collection_name: "my_project"
  embedder: "local"             # local SentenceTransformer (or "ember" for cloud)
```

Add content via slash commands: `/knowledge add <url|path|text>`, search with `/knowledge search <query>`. Agents can search the knowledge base automatically during execution.

### Learning & Reasoning

- **Learning** — Agno LearningMachine builds user profiles, entity memory, and session context across conversations
- **Reasoning tools** — `think` and `analyze` tools for step-by-step reasoning during complex tasks

### Guardrails

Built-in safety guardrails via Agno's pre-hook system:

```yaml
guardrails:
  pii_detection: true          # detect and flag PII in prompts
  prompt_injection: true       # detect injection attempts
  moderation: true             # OpenAI moderation API
```

### Human-in-the-Loop (HITL)

Agents can pause execution to request confirmation or user input before proceeding with sensitive operations. The TUI shows interactive approval dialogs.

## Documentation

- **[Quickstart](QUICKSTART.md)** — Get up and running in under 5 minutes
- [Architecture](docs/ARCHITECTURE.md) — System design and agent topology
- [Agents](docs/AGENTS.md) — Specialized agents and their roles
- [Skills](docs/SKILLS.md) — Reusable prompted workflows (`/deploy`, `/resolve-issues`, etc.)
- [Onboarding](docs/ONBOARDING.md) — First-run setup, CodeIndex, and agent proposals
- [Tools](docs/TOOLS.md) — Available toolkits and capabilities
- [MCP](docs/MCP.md) — IDE integration via Model Context Protocol
- [Configuration](docs/CONFIGURATION.md) — Settings, permissions, and customization
- [CodeIndex](docs/CODEINDEX.md) — Semantic code intelligence engine
- [Benchmarks](docs/BENCHMARKS.md) — Head-to-head comparison vs Claude Code on a 12-case suite
- [Evals](docs/EVALS.md) — Agent evaluation framework and regression testing
- [Hooks](docs/HOOKS.md) — Pre/post tool execution hooks
- [Migration](docs/MIGRATION.md) — Coming from Claude Code or Codex
- [Security](docs/SECURITY.md) — Threat model, permissions, and enterprise hardening
- [Development](docs/DEVELOPMENT.md) — Contributing and extending Ember Code

## License

Apache 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
