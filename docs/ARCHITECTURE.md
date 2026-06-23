# Architecture

## Overview

igni is a terminal-based AI coding assistant built on [Agno](https://docs.agno.com/). Its core innovation is **dynamic team assembly**: instead of a fixed agent hierarchy, an Orchestrator analyzes each task and builds a purpose-fit team on the fly from a pool of agent definitions.

```
┌ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─┐
  FRONTEND PROCESS (Textual TUI)
│                                                     │
  ┌─────────────────────────────────────────────────┐
│ │               EmberApp (Textual)                │ │
  │  ConversationView │ InputHandler │ StatusTracker│
│ │  SessionManager │ HITLHandler │ RunController   │ │
  └──────────────────────┬──────────────────────────┘
│                        │                            │
                         │ BackendClient
│                        │                            │
└ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─│─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─┘
                         │
              Unix socket + NDJSON protocol
              (newline-delimited JSON messages)
                         │
┌ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─│─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─┐
  BACKEND PROCESS       │
│                        ▼                            │
  ┌─────────────────────────────────────────────────┐
│ │         BackendServer + CommandHandler           │ │
  │         (protocol dispatch, slash commands)      │
│ └──────────────────────┬──────────────────────────┘ │
                         │
│ ┌──────────────────────▼──────────────────────────┐ │
  │              Session Manager                    │
│ │      (conversation state, history, memory)      │ │
  └──────────────────────┬──────────────────────────┘
│                        │                            │
  ┌──────────────────────▼──────────────────────────┐
│ │              Orchestrator (meta-agent)           │ │
  │              → Agent Pool (.md files)           │
│ │              → Dynamic Team Assembly            │ │
  │                (route/coord/broadcast/tasks)    │
│ │              → Unlimited nesting depth          │ │
  └──────────────────────┬──────────────────────────┘
│                        │                            │
  ┌──────────────────────▼──────────────────────────┐
│ │              Guardrails Layer                    │ │
  │   PII Detection │ Prompt Injection │ Moderation │
│ └─────────────────────────────────────────────────┘ │
  ┌─────────────────────────────────────────────────┐
│ │              Tool Layer (Agno Toolkits)          │ │
  │   Shell │ File │ Edit │ Search │ Git │ Web      │
│ └─────────────────────────────────────────────────┘ │
  ┌─────────────────────────────────────────────────┐
│ │              Knowledge Layer                     │ │
  │   ChromaDB vector store │ EmberEmbedder (384d)  │
│ └─────────────────────────────────────────────────┘ │
  ┌─────────────────────────────────────────────────┐
│ │              MCP Layer                           │ │
  │   Server (expose tools to IDEs via stdio)       │
│ │   Client (consume external MCP servers)         │ │
  └─────────────────────────────────────────────────┘
│ ┌─────────────────────────────────────────────────┐ │
  │              Storage Layer                      │
│ │      SQLite (sessions) │ Memory (user prefs)    │ │
  └─────────────────────────────────────────────────┘
│                                                     │
└ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─┘
```

## Core Design Principles

### 1. Dynamic Team Assembly

Nothing is hardcoded. The Orchestrator is a reasoning-enabled meta-agent that reads the full agent pool (descriptions, tools, tags) and the user's message, then decides:

- **Which agents** to include (minimal set needed)
- **Which team mode** to use (route, coordinate, broadcast, tasks)
- **What instructions** to give the team leader

This means the system adapts to new agents automatically. Drop a `database.md` into the agents folder — the Orchestrator can start including it in teams immediately, without any code changes.

When no existing agent fits a task, the Orchestrator can **generate an ephemeral agent** on the fly — writing a new `.md` file to `.ember/agents.ephemeral/` with a task-specific system prompt and tools. Ephemeral agents are session-scoped and auto-cleaned, but can be promoted to permanent agents by the user.

### 2. Agents as Data, Not Code

Agents are `.md` files with YAML frontmatter, not Python classes. This makes them:

- **Easy to create** — anyone can write markdown
- **Easy to share** — commit to the repo, whole team gets them
- **Easy to override** — project-level definitions override built-ins
- **Inspectable** — read the file, understand the agent

The agent loader parses these files and constructs `agno.Agent` objects at runtime. See [Agents](AGENTS.md) for the full format specification.

### 3. Right-Sized Teams

Not every task needs a team. The Orchestrator's primary optimization is **minimizing overhead**:

- **Single agent** — for simple questions or single-capability tasks, skip team creation entirely. Run one agent directly.
- **Route mode** — when the task is clear but could go to different agents, use routing (one decision, then passthrough).
- **Coordinate mode** — for multi-step tasks needing different capabilities in sequence.
- **Broadcast mode** — when parallel independent perspectives add value (e.g., security + performance review).
- **Tasks mode** — for large autonomous goals requiring iteration and progress tracking.

| Mode | Overhead | When |
|---|---|---|
| Single agent | Lowest | One agent clearly fits |
| Route | Low | Need to pick one from several |
| Coordinate | Medium | Multi-step, different capabilities |
| Broadcast | High | Independent parallel perspectives |
| Tasks | Highest | Large autonomous goals |

### 4. Unlimited Nesting

Claude Code caps sub-agents at one level (sub-agents can't spawn sub-agents). igni has **no nesting limit**. Every agent can access the agent pool and spawn sub-teams by default (`can_orchestrate: true`). Opt out per-agent with `can_orchestrate: false`.

This matters because **the agent closest to the problem is best positioned to decide if it needs help**. An editor mid-refactor might spawn an explorer to understand unfamiliar code. A reviewer might spawn a security-auditor for a specific concern. Each sub-team picks its own mode.

Practical guardrails (configurable depth limits, agent caps, timeouts) prevent runaway recursion without restricting the design. See [Agents](AGENTS.md#recursive-nesting-agents-that-build-teams) for details.

### 5. Persistent Memory

Agno's built-in memory system replaces file-based memory:

- **User memory** — preferences, role, expertise level (persists across sessions)
- **Session storage** — conversation history, tool outputs (per-session, DB-backed)
- **Session state** — current task progress, open files, working context
- **Progress tracking** — `.ember/TODO.md` auto-loaded into context for cross-session task continuity

## Request Lifecycle

```
User Input
    │
    ▼
┌──────────────┐
│ Permission   │──── blocked? → inform user
│ Pre-check    │
└──────┬───────┘
       │
       ▼
┌──────────────────────────┐
│      Orchestrator        │
│                          │
│  1. Read agent pool      │
│  2. Analyze message +    │
│     conversation context │
│  3. Output: TeamPlan     │
│     • agent_names        │
│     • team_mode          │
│     • instructions       │
└──────────┬───────────────┘
           │
           ▼
    ┌──────────────┐
    │ Build Team   │──── instantiate agents from pool
    │ or single    │     set mode, instructions
    │ agent        │
    └──────┬───────┘
           │
           ▼
    ┌──────────────┐
    │ Execute      │──── team/agent runs with tools
    │              │     (may involve multiple turns)
    └──────┬───────┘
           │
           ▼
    ┌──────────────┐
    │ Post-process │──── format, log, audit
    └──────┬───────┘
           │
           ▼
     Display to User
```

### Orchestrator Shortcut

For obvious single-agent tasks, the Orchestrator can skip team assembly:

```
"What does auth.py do?"
  → Orchestrator recognizes: read-only question, one agent needed
  → Runs Explorer directly (no team overhead)
  → ~1 LLM call for orchestration + N calls for the agent
```

For complex tasks, the orchestration overhead (one extra LLM call) is negligible compared to the multi-step execution.

## Agent Pool Lifecycle

```
Startup
    │
    ├── Copy built-in agents → .ember/agents/   (first run, checksum-merged on update)
    ├── Copy built-in skills → .ember/skills/   (first run, checksum-merged on update)
    │
    ├── Scan ~/.ember/agents/              (global)
    ├── Scan ~/.ember/skills/              (global)
    │
    ├── Scan .ember/agents.local/          (project, gitignored)
    ├── Scan .ember/agents/                (project)
    ├── Scan .ember/skills.local/          (project, gitignored)
    ├── Scan .ember/skills/                (project)
    └── Scan .ember/agents.tmp/            (session-scoped, auto-cleaned)
           │
           │  With cross_tool_support: true, also scans:
           │  ├── ~/.claude/agents/        (Claude Code global)
           │  ├── ~/.claude/skills/        (Claude Code global)
           │  ├── ~/.codex/               (Codex global)
           │  ├── .claude/agents/          (Claude Code project)
           │  ├── .claude/skills/          (Claude Code project)
           │  └── AGENTS.md / .codex/      (Codex project)
           │
           ▼
    ┌──────────────────────────────────────────────┐
    │  Agent Pool + Skill Pool (combined)          │
    │                                              │
    │  Name conflict? → project wins over global   │
    │                   → global wins over built-in │
    └──────────────────────────────────────────────┘
           │
           ├── Hot reload: watch for file changes
           └── Orchestrator reads pool on each request
```

By default, igni loads agents and skills from both its own directories and Claude Code / Codex directories (`cross_tool_support` is on by default). Set `cross_tool_support: false` to only scan igni directories. See [Agents](AGENTS.md) and [Skills](SKILLS.md) for format details.

## Session Management

Each igni session:

1. **First run?** — if `.ember/agents/` doesn't exist, run the [onboarding flow](ONBOARDING.md): create default agents, ask about the user's work, fetch project context from CodeIndex, propose tailored agents
2. **Loads** — agent pool (from Ember/Claude/Codex directories), user memory, project context (`ember.md`), MCP servers, session history
3. **Runs** — interactive loop: user message → Orchestrator → team/agent → response
3. **Persists** — updated memory, session state to SQLite
4. **Cleans up** — MCP connections, temp files, background processes

By default, sessions are stored locally in `~/.ember/sessions.db` using Agno's `SqliteDb` backend. User memory lives in `~/.ember/memory.db`.

**Cross-device sync:** Claude Code stores sessions locally only — they don't sync across devices. igni defaults to the same (SQLite, local), but Agno's storage layer supports 15+ backends. Configure `storage.backend: "postgres"` (or MongoDB, Redis, DynamoDB, etc.) to sync sessions and memory across devices. See [Configuration](CONFIGURATION.md) for details.

## Knowledge System

igni includes a built-in vector knowledge base powered by ChromaDB and a custom embedder.

### EmberEmbedder

A custom Agno `Embedder` that calls the Ember server's `/v1/embeddings` endpoint, proxying to a text2vec-transformers model (384 dimensions). This keeps embedding generation server-side — no local GPU required.

### KnowledgeManager

Creates an Agno `Knowledge` instance backed by ChromaDB. Manages document ingestion, search, and status. All data models are Pydantic: `KnowledgeAddResult`, `KnowledgeSearchResponse`, `KnowledgeFilter`, `KnowledgeStatus`.

```yaml
knowledge:
  enabled: true
  collection_name: "my_project"
  embedder: "ember"
```

Add content via `/knowledge add <url|path|text>`, search with `/knowledge search <query>`. Agents can search the knowledge base automatically during execution.

## Learning & Reasoning

### Learning

Agno's `LearningMachine` builds user profiles, entity memory, and session context across conversations. Configured via:

```yaml
learning:
  enabled: true
  user_profiles: true
  entity_memory: true
```

### Reasoning

Agents can use `think` and `analyze` tools for step-by-step reasoning during complex tasks. Enable per-agent in `.md` frontmatter with `reasoning: true`, or globally:

```yaml
reasoning:
  enabled: true
  tools: ["think", "analyze"]
```

## Guardrails

Built-in safety guardrails run as Agno pre-hooks before each agent turn:

| Guardrail | What It Does |
|---|---|
| **PII Detection** | Flags personally identifiable information in prompts |
| **Prompt Injection** | Detects injection attempts in user input and tool output |
| **Moderation** | Content moderation via OpenAI's moderation API |

```yaml
guardrails:
  pii_detection: true
  prompt_injection: true
  moderation: true
```

Guardrails are applied via `AgnoFeatures.apply_to_agent()` — they attach as pre-hooks that run before the model is called. If a guardrail triggers, the agent is informed and can adjust.

## Human-in-the-Loop (HITL)

Agents can pause execution to request confirmation or user input before proceeding with sensitive operations. HITL is implemented via Agno's callback system and surfaces as interactive dialogs in the TUI.

Two HITL modes:
- **Confirmation** — agent asks "Should I proceed with X?" (yes/no)
- **User input** — agent asks an open-ended question and waits for a response

## Run Cancellation

Users can cancel a running agent or team mid-execution. In the TUI, press `Escape` to cancel. The `RunController` handles cleanup of spinners, partial output, and agent state.

## TUI Architecture

The TUI is built with [Textual](https://textual.textualize.io/) and follows a clean separation of concerns:

| Class | File | Responsibility |
|---|---|---|
| `EmberApp` | `app.py` | Textual shell: compose, mount, keybindings, event routing, scheduler |
| `ConversationView` | `conversation_view.py` | Widget append/clear operations |
| `RunController` | `run_controller.py` | Execution pipeline, streaming, cancellation, task visualization |
| `StatusTracker` | `status_tracker.py` | Token/context tracking, status bar |
| `HITLHandler` | `hitl_handler.py` | Confirmation dialogs, user input |
| `SessionManager` | `session_manager.py` | Session picker, switching, clearing |
| `InputHandler` | `input_handler.py` | History, autocomplete |

`CommandHandler` has moved to the backend process — slash commands are now dispatched server-side via protocol messages. See [Process Split Architecture](#process-split-architecture) below.

`EmberApp` is a thin shell that delegates to focused manager classes. Textual requires `action_*` methods and `@on` decorators on the App, but the logic lives in the managers. Each manager takes an `EmberApp` reference (via `TYPE_CHECKING` to avoid circular imports) and uses `query_one()` to interact with widgets.

### Task Visualization

When teams run in **tasks mode**, the TUI displays a live `TaskProgressWidget` showing:
- Task list with status icons (○ pending, ◉ in-progress, ● completed, ✗ failed, ◌ blocked)
- Assignees, dependencies, and iteration progress
- Summary line with done/running/failed counts

The widget updates in real-time as `TaskCreatedEvent`, `TaskUpdatedEvent`, and `TaskStateUpdatedEvent` stream in from Agno.

### Background Task Scheduling

`EmberApp` runs a `SchedulerRunner` in the background that polls for due tasks. Configuration:
- `poll_interval`: how often to check (default 30s)
- `task_timeout`: max duration per task (default 300s)
- `max_concurrent`: bounded concurrency via `asyncio.Semaphore` (default 1, sequential)

When scheduled tasks complete, toast notifications appear via Textual's `notify()` system.

## Process Split Architecture

igni runs as two OS processes connected by a Unix domain socket. This isolates the TUI rendering from heavy AI/tool execution, preventing long-running agent work from blocking the UI event loop.

```
┌──────────────┐   Unix socket   ┌──────────────┐
│   Frontend   │◄───────────────►│   Backend    │
│  (Textual)   │  NDJSON framing │  (Session +  │
│              │                 │   Agents)    │
└──────────────┘                 └──────────────┘
   PID A                            PID B
```

### Key Components

| Component | Location | Role |
|---|---|---|
| `BackendServer` | `backend/server.py` | Wraps Session; dispatches FE messages to Session/CommandHandler; streams Agno events back as protocol messages |
| `BackendClient` | `frontend/tui/backend_client.py` | FE-side proxy with the same public interface as BackendServer; serializes calls to protocol messages over the socket |
| `BackendProcess` | `frontend/tui/process_manager.py` | Spawns the BE subprocess (`python -m ember_code.backend`), manages its lifecycle, waits for READY, and returns a connected `BackendClient` |
| `CommandHandler` | `backend/command_handler.py` | Slash command dispatch (moved from TUI to backend so commands execute with direct Session access) |
| `UnixSocketClientTransport` / `UnixSocketServerTransport` | `transport/unix_socket.py` | Async Unix socket transport with NDJSON framing (one JSON object per `\n`) |

### Protocol Messages

All messages are Pydantic models defined in `protocol/messages.py` with plain types only (no Agno imports). This keeps the FE/BE contract serialization-safe.

**FE to BE (requests):**
- `RunRequest` — send user message for agent execution
- `CancelRequest` — cancel a running agent/team
- `CommandRequest` — execute a slash command
- `HITLResponse` — user's answer to a confirmation/input prompt

**BE to FE (events, streamed):**
- `ContentDelta` — streamed text chunk from the model
- `ToolStarted` / `ToolCompleted` / `ToolError` — tool lifecycle events
- `HITLRequest` / `RunPaused` — HITL prompts (permission dialog + run-level pause wrapper)
- `RunCompleted` / `RunError` — run lifecycle bookends
- `StatusUpdate` — token counts, context usage, model info

### Startup Sequence

1. `EmberApp` creates a `BackendProcess` with project dir and settings
2. `BackendProcess.start()` spawns `python -m ember_code.backend --socket /tmp/ember-code/<id>.sock`
3. The BE process creates `BackendServer`, binds to the socket, sends a `Ready` message
4. `BackendProcess` connects a `BackendClient` to the socket and returns it
5. The FE uses `BackendClient` as if it were a local `BackendServer` — all calls are transparent proxies

### Design Rationale

- **Non-blocking UI** — Textual's event loop stays responsive while agents run. Token streaming, tool progress, and HITL dialogs all arrive as async messages.
- **Crash isolation** — a backend crash (OOM from large context, tool segfault) does not kill the TUI. The FE can detect the lost connection and offer to restart.
- **Future flexibility** — the socket protocol is transport-agnostic (the `Transport` base class abstracts over Unix socket, TCP, or in-process). Remote backends and multi-session servers become possible without changing the FE.

## Error Handling

- **Tool failures** — agents retry with alternative approaches (Agno's built-in retry)
- **Model errors** — graceful fallback with user notification
- **Permission denied** — clear explanation of what was blocked and why
- **Context overflow** — two-layer compression (tool result compression + conversation history compaction) triggers at 80% of `min(model_window, models.max_context_window)`, default ceiling 200k tokens
- **Agent not found** — Orchestrator falls back to available agents with a warning
- **Team failure** — if a coordinated team fails mid-execution, partial results are preserved and shown

## Security Model

igni follows a defense-in-depth approach:

1. **Permission tiers** — configurable per-tool approval requirements
2. **Command blocking** — dangerous shell commands blocked, others require confirmation
3. **File guards** — sensitive paths (`.env`, credentials) protected from writes
4. **Confirmation prompts** — destructive/irreversible actions require explicit approval
5. **Audit log** — all tool executions logged to `~/.ember/audit.log`
6. **Agent isolation** — agents only get the tools declared in their definition
7. **Guardrails** — PII detection, prompt injection detection, and content moderation as pre-hooks

See [Configuration](CONFIGURATION.md) for permission settings.
