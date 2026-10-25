# Development

Guide for contributing to igni and understanding the codebase.

## Project Structure

Packages, not files. A tree that names every module is wrong the week
after it is written — the last one listed 19 top-level paths and all 19
had been deleted. What follows is the set of directories that have
survived the refactors, with one line on what each is *for*. For the
current file list inside any of them, `ls` it.

```
ember-code/
├── src/ember_code/         # the Python package (import name stays `ember_code`)
│   ├── __main__.py         # entry point behind `igni`, `ignite-ember`, `python -m ember_code`
│   ├── cli/                # non-interactive command surface: flags, `-m`, pipe mode, resume
│   ├── backend/            # the long-lived server the GUI clients speak to — slash-command
│   │   │                   # and RPC dispatch, run streaming, HITL, session pool, panels
│   │   ├── panels/         # data behind the clients' agents / skills / hooks panels
│   │   └── rpc_handlers/   # one handler module per RPC domain (auth, mcp, loop, knowledge, …)
│   ├── core/               # everything that is not CLI, wire format, or server plumbing
│   │   ├── agents/         # agent `.md` discovery, frontmatter parsing, AgentPool, agent build
│   │   ├── auth/           # browser login, local callback server, credential storage
│   │   ├── config/         # Settings, model registry, permission evaluation, policy tiers
│   │   ├── db/             # SQLAlchemy engine + sessionmaker cache, migration entry point
│   │   ├── decisions/      # Laya, igni's decision model: the Bypass guard and `host.decide`
│   │   ├── evals/          # YAML eval loading, assertions, runner, reporter
│   │   ├── guardrails/     # one rule per file (PII, injection, moderation) behind one runner
│   │   ├── hooks/          # hook discovery, matcher, executor, tool-event pipeline
│   │   ├── init/           # first-run and per-project initialization, hook provisioning
│   │   ├── loop/           # `/loop` state — caps, stored prompt, per-iteration tool results
│   │   ├── lsp/            # language-server client and per-project manager
│   │   ├── mcp/            # MCP client, transports, `.mcp.json` config, approval, tool filter
│   │   ├── memory/         # Agno memory / DB setup
│   │   ├── monitors/       # plugin-declared background monitors (config, supervisor, handles)
│   │   ├── output_styles/  # output-style catalog loading
│   │   ├── plugins/        # plugin + marketplace discovery, install, enable/disable state
│   │   ├── prompts/        # the main-agent and plan-mode system prompts (`.md`)
│   │   ├── scheduler/      # `/schedule`: time-expression parsing, recurrence, store, runner
│   │   ├── session/        # the Session — wiring, run loop, persistence, compaction, commands
│   │   ├── skills/         # `SKILL.md` discovery, frontmatter parsing, invocation
│   │   ├── tools/          # every agent tool: edit, shell/process, orchestrate, plan, todo, web
│   │   └── utils/          # project context (`igni.md`), audit log, display, mentions, tips
│   ├── protocol/           # the BE↔FE wire contract — Pydantic message models under `schemas/`
│   ├── transport/          # how those messages move: unix socket, websocket, in-process, NDJSON
│   ├── bundled_agents/     # the agent `.md` files that ship inside the wheel
│   ├── bundled_skills/     # the `SKILL.md` directories that ship inside the wheel
│   └── migrations/         # Alembic environment + `versions/`
├── clients/                # the front-ends: `web`, `tauri`, `vscode`, `jetbrains`
├── agents/                 # repo-root copies of most agent `.md` files, read by a few
│                           # scripts and one parity test. `bundled_agents/` is what ships,
│                           # and the two have already drifted apart.
├── skills/                 # repo-root copies of the bundled skill directories
├── docs/                   # this file and its siblings — `ls docs/` is the list
├── evals/                  # YAML eval suites and their fixtures
├── scripts/                # dev + ops scripts: profiling, eval drivers, client builds
├── tests/                  # ~313 files, flat, one per behaviour.
│   │                       # Not listed here: a hand-kept sample reads as the
│   │                       # whole suite and goes stale silently — the last one
│   │                       # still named test_knowledge.py, test_permissions.py,
│   │                       # test_widgets.py and test_tui_handlers.py long after
│   │                       # all four were deleted. `ls tests/` is the list.
│   └── conftest.py         # Shared fixtures; .env loading; the `integration`
│                           # marker and its skip
├── Makefile                # Build commands (see below)
├── pyproject.toml          # Package metadata, deps, tool config
├── igni.md                 # Project instructions for self-use
├── README.md
└── LICENSE
```

There is no `tui/` package. The Textual TUI was removed; interactive
chat lives in the React clients under `clients/`, which talk to the
backend started by `python -m ember_code.backend`.

## Setup

```bash
# Clone
git clone https://github.com/ignite-ember/ember-code.git
cd ember-code

# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install with all extras
make install
# or: pip install -e ".[dev,mcp,web]"
# The extras are whatever `[project.optional-dependencies]` in
# pyproject.toml defines. `knowledge` was listed here for a long time
# after it was removed; pip only warns about an extra that does not
# exist, so the line kept working and kept installing nothing.

# Verify
ignite-ember --version
make check
```

## Makefile

All common tasks are available via `make`:

```bash
make help           # Show all targets
make install        # Install package with all extras
make test           # Run tests (pytest)
make test-v         # Run tests with verbose output
make test-cov       # Run tests with coverage report
make lint           # Run ruff linter
make format         # Auto-format code (ruff)
make format-check   # Check formatting without changes
make typecheck      # Run mypy type checking
make check          # Run ALL checks (lint + format + typecheck + test)
make fix            # Auto-fix lint and formatting issues
make clean          # Remove build artifacts and caches
```

The `make check` target is the single command to verify everything is clean before committing.

## Dependencies

**Core:**
- `agno[openai]` — Agent orchestration framework
- `rich` — Terminal formatting and markdown rendering
- `textual` — Terminal UI framework
- `pyyaml` — Configuration file parsing
- `click` — CLI argument parsing
- `pydantic` — Settings and data models
- `httpx` — HTTP client (embeddings, web tools)

**Optional** — one bullet per extra in `[project.optional-dependencies]`, and no others. There is no `knowledge` extra, and no graph database or embedder in igni at all: CodeIndex's local index (Neo4j per commit, a sentence-transformer) is its extension's, installed from its bundle.

- `mcp` — Model Context Protocol (`pip install ember-code[mcp]`)
- `google-genai` — Gemini models (`pip install ember-code[gemini]`)
- `duckduckgo-search` — Web search (`pip install ember-code[web]`)

**Dev:**
- `pytest` + `pytest-asyncio` — Testing
- `ruff` — Linting and formatting
- `mypy` — Type checking

## Key Implementation Details

### 1. Agent Pool (pool.py)

The AgentPool discovers, parses, and manages agent definitions from `.md` files:

```python
class AgentPool:
    """Loads agent definitions from .md files and builds Agno Agent objects."""

    def _load_all(self, config: Settings):
        # Load in priority order (highest last, so they overwrite)
        dirs = [
            (Path.home() / ".igni" / "agents", 0),        # user global
            (Path(".igni/agents.local"), 1),               # project local
            (Path(".igni/agents"), 2),                     # project shared
        ]
```

### 2. Orchestration (tools/orchestrate.py)

The main agent has `OrchestrateTools` which provides `spawn_agent()`, `spawn_team()`, and `create_agent()`. There is no separate orchestrator — the main agent itself decides when to delegate via tool calls. Sub-teams are built from the `AgentPool` with all Agno features (session persistence, compression, knowledge, learning, guardrails) applied by `Session._build_main_agent()`.

### 5. Backend Architecture (backend/)

The Textual TUI this section used to describe no longer exists. Its
separation-of-concerns shape survived the move: the backend server is a
thin shell that delegates to focused classes, and the interactive
surface is now the React clients under `clients/`.

| Class | File | Responsibility |
|---|---|---|
| `BackendApp` | `backend/app.py` | Process shell: transport setup, wiring, lifecycle |
| `SessionPool` | `backend/session_pool.py` | Per-project `SessionRuntime` instances, switching |
| `RunController` | `backend/run_controller.py` | Execution pipeline, streaming, cancellation |
| `StreamEventDispatcher` | `backend/stream_event_dispatcher.py` | Agno run events → wire messages |
| `MessageDispatcher` | `backend/message_dispatcher.py` | Inbound FE action routing |
| `CommandHandler` | `backend/command_handler.py` | Slash-command dispatch |
| `HitlController` | `backend/hitl_controller.py` | Permission prompts, confirmations, user input |

### 6. Model Resolver (config/models.py)

Agent `.md` files specify models by name. The registry maps names to provider, endpoint, model ID, and credentials:

```python
def get_model(name: str, config: Settings) -> Model:
    # 1. User config registry (BYOM)
    # 2. Built-in registry (server-hosted)
    # 3. provider:model_id syntax (e.g., "openai_like:gpt-4o")
```

## Testing

```bash
# Run all checks
make check

# Or individually:
make test            # pytest
make lint            # ruff check
make format          # ruff format
make typecheck       # mypy

# Quick iteration
make fix             # auto-fix lint + format
make test            # verify tests still pass
```

### Test Strategy

- **Unit tests** — agent pool, .md parsing, tool registry, config merging, knowledge models, backend server
- **Integration tests** — Orchestrator decisions, team assembly, AgnoFeatures application
- **Mock LLM calls** — mock model responses to test orchestration logic
- **Agent definition tests** — validate all built-in `.md` files parse correctly
- **912 tests** across 53 test files, all passing

## Slash Commands

Built-in commands available in interactive mode:

| Command | Description |
|---|---|
| `/help` | Show available commands and keyboard shortcuts |
| `/quit` or `/exit` | Exit igni |
| `/clear` | Clear conversation and reset session |
| `/config` | Show current settings (model, permissions, features) |
| `/agents` | List loaded agents with their tools |
| `/skills` | List loaded skills |
| `/hooks` | List loaded hooks |
| `/sessions` | Browse and resume past sessions |
| `/rename <name>` | Rename the current session |
| `/memory` | List stored memories |
| `/memory optimize` | Consolidate memories |
| `/knowledge` | Show knowledge base status |
| `/knowledge add <url\|path\|text>` | Add content to knowledge base |
| `/knowledge search <query>` | Search the knowledge base |
| `/evals [agent]` | Run agent evals (optionally filter by agent name) |
| `/sync-knowledge` | Sync knowledge between git file and vector DB |
| `/login` | Authenticate via device-flow (opens browser) |
| `/schedule add <when> <prompt>` | Schedule a one-off or recurring prompt (e.g. `/schedule add review code at 5pm`, `/schedule add run linter every 2 hours`) |
| `/loop <prompt>` | Re-fire the same prompt as the next user turn until cap, `/loop stop`, or any non-`/loop` input (default cap 30, hard cap 200). Forms: `/loop` (status), `/loop <N> <prompt>` (explicit cap), `/loop stop` (cancel) |
| `/plugins` | Open the Textual plugins panel (browse, toggle enable/disable, update/remove, browse marketplaces, install). See [Plugins](PLUGINS.md). |
| `/plugins enable <name>` / `/plugins disable <name>` | Toggle a plugin without opening the panel — takes effect on next session start. |
| `/plugin install <git-url\|@marketplace/plugin>` | Install a Claude-Code-compatible plugin into `~/.igni/plugins/`. Optional `--ref <branch\|tag\|sha>`. |
| `/plugin update <name>` / `/plugin remove <name>` | Update (fetch + reset to origin's HEAD or `--ref`) or uninstall. |
| `/plugin marketplace add\|list\|remove\|refresh` | Manage registered marketplaces — Claude-Code-compatible catalogs at the root of any git repo. |
| `/compact` | Manually compact session history (otherwise triggers at 80% of the context window) |
| `/bug` | Capture a bug report with session context |
| `/mcp` | List configured MCP servers and their connection status |
| `/model` | Switch the active model (or show current) |
| `/whoami` | Show the active your igni server login |
| `/logout` | Clear cached your igni server credentials |
| `/<skill-name> [args]` | Invoke a skill (e.g., `/commit`) |

## Architecture Decisions

### Why Agno over raw API calls?

- **Team orchestration** — dynamic task decomposition and routing
- **Memory management** — built-in persistent memory with DB backends
- **Tool ecosystem** — 100+ pre-built toolkits
- **Model agnostic** — swap models without code changes
- **Reasoning** — built-in chain-of-thought with `reasoning=True`
- **Knowledge** — ChromaDB vector stores with pluggable embedders
- **Learning** — LearningMachine for user profiles and entity memory
- **Guardrails** — PII detection, prompt injection, moderation as pre-hooks

### Why dynamic team assembly instead of a fixed hierarchy?

A fixed Router → Agent hierarchy forces you to pre-decide what agents exist and how they interact. The dynamic approach lets:
- Users add agents without code changes (just drop a `.md` file)
- The Orchestrator adapt team composition to each specific task
- Different tasks use different team modes (coordinate for multi-step, broadcast for review, etc.)
- Project teams customize behavior by overriding built-in agent definitions

### Why agents as .md files?

- **Accessibility** — anyone can write markdown, no Python needed
- **Portability** — commit to repo, share across team
- **Overridability** — project definitions override built-ins by name
- **Transparency** — read the file, understand exactly what the agent does
- **Claude Code compatibility** — same YAML frontmatter format, easy migration

### Why a backend server with separate controller classes?

The interactive surface is a GUI client, not a terminal app — `igni`
and `ignite-ember` are the non-interactive CLI, and chat happens in the
clients under `clients/`, which connect to the server started by
`python -m ember_code.backend`. Splitting the front-end out means the
same backend serves four clients (web, tauri, vscode, jetbrains) over
one wire protocol. Inside the backend, `BackendApp` is a thin shell that
delegates to the controllers listed above; each stays testable in
isolation and none of them becomes the god-class.

## Roadmap

See [GitHub Issues](https://github.com/ignite-ember/ember-code/issues) for the current roadmap.

**Planned features:**
- [ ] Centralized tracing (OpenTelemetry → igni server)
- [ ] Plugin system (installable agent/tool packages)
- [ ] Web UI (Agno Playground integration)
- [ ] Voice mode (speech-to-text input)
- [ ] Multi-repo support (workspaces)
