# Configuration

Ember Code is configured through a layered system of config files, environment variables, and CLI flags.

## Configuration Hierarchy

(Highest priority first)

1. **CLI flags** — `--model`, `--no-web`, etc.
2. **Project local config** — `.ember/config.local.yaml` (gitignored, personal overrides)
3. **Project config** — `.ember/config.yaml` (committed to repo, shared with team)
4. **User config** — `~/.ember/config.yaml` (global, generated on first run)
5. **Defaults** — built-in sensible defaults

Each level deep-merges over the previous. The `~/.ember/config.yaml` file is automatically generated from defaults on first run via `yaml.dump(DEFAULT_CONFIG)` — a single source of truth.

## Models & Authentication

Ember Code needs an LLM to run. Models are resolved through a **config-driven registry** — agent `.md` files reference models by name (e.g., `model: MiniMax-M2.7`), and the registry maps that name to a provider, endpoint URL, model ID, and API key.

### Model Registry

The registry has two layers:

1. **Built-in models** — ship with Ember Code, route through the Ember hosted endpoint
2. **Custom models (BYOM)** — user-defined entries that override or extend the built-ins

```yaml
# Built-in registry (hardcoded in defaults.py, shown here for reference)
models:
  registry:
    MiniMax-M2.7:
      provider: openai_like
      model_id: MiniMax-Text-01
      url: https://api.ignite-ember.sh/v1
      api_key: cloud_token    # uses Ember Cloud login credentials
      context_window: 204800
      vision: false
```

Agent `.md` files just reference the registry name:

```yaml
model: MiniMax-M2.7           # → looks up "MiniMax-M2.7" in the registry
```

### Resolution Order

When an agent references `model: <name>`:

```
1. Is <name> in models.registry (user config)?     → use it
2. Is <name> in built-in registry?                  → use it (Ember hosted endpoint)
3. Does <name> contain ":" (e.g., "openai:gpt-4o")? → parse as provider:model_id
4. None of the above?                               → error: unknown model
```

### Option 1: Ember Code Account (default, zero-config)

Sign up at **https://ignite-ember.sh**. All built-in models route through the Ember hosted endpoint. Free tier available.

```bash
ignite-ember /login         # opens browser for device-flow login
```

**Device-flow login:** Running `/login` opens your browser to the Ember portal. After you authenticate, the CLI automatically receives your access token and model credentials. Platform credentials are saved to `~/.ember/credentials.json` (token, email, expiry). Model credentials (API key, URL) are saved to `~/.ember/config.yaml`.

No manual model configuration needed — the built-in registry handles everything.

#### Cloud model auto-discovery

When you're logged in, Ember Code fetches the deduplicated `(model, base_url)` catalogue from your Ember Cloud key pool (`GET /v1/chat/models`) and merges each entry into the local registry on session start. Opening the model picker (`/model`) refreshes the catalogue so models added on the portal show up without restarting the CLI.

Cloud-discovered entries always use `api_key: cloud_token` (your login credentials) and are tagged with `source: "cloud"`. **User-defined entries always win** — if your config already defines `gpt-4o`, the cloud entry with the same name is skipped, so pinned timeouts and provider overrides survive.

Failure modes are all soft: missing token, network error, timeout (3 s), and non-200 responses each degrade silently to whatever's already in the local registry.

### Option 2: Bring Your Own Model (BYOM)

Add entries to `models.registry` in your config. These override built-in entries with the same name, or add entirely new models.

```yaml
# .ember/config.yaml
models:
  registry:
    # Ember Cloud model — uses login credentials
    MiniMax-M2.7:
      provider: openai_like
      model_id: MiniMaxAI/MiniMax-M2.7
      url: https://api.ignite-ember.sh/v1
      api_key: cloud_token
      context_window: 204800
      vision: false

    # Direct API key — vision-capable model
    GPT-5.4:
      provider: openai_like
      model_id: gpt-5.4
      api_key: sk-proj-...your_key_here
      context_window: 1048576
      vision: true

    # Environment variable for API key
    gpt-4o:
      provider: openai_like
      model_id: gpt-4o
      api_key_env: OPENAI_API_KEY
      vision: true

    # Or use a shell command (e.g., 1Password, vault)
    claude-sonnet:
      provider: openai_like
      model_id: claude-sonnet-4-6
      url: https://api.anthropic.com/v1
      api_key_cmd: "op read op://Dev/anthropic/api-key"
```

Now agents can reference `model: gpt-4o` or `model: claude-sonnet` in their `.md` files.

Works with MiniMax, OpenAI, Anthropic, Groq, Together AI, OpenRouter, Ollama, or any OpenAI-compatible API.

### Registry Entry Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `provider` | string | yes | Agno model class to use. `openai_like` for any OpenAI-compatible API |
| `model_id` | string | yes | Model identifier sent to the API (e.g., `MiniMax-Text-01`, `gpt-4o`) |
| `url` | string | yes | API base URL (e.g., `https://api.openai.com/v1`). Omit only for OpenAI models that use the default endpoint |
| `api_key` | string | no | API key value, or `cloud_token` to use Ember Cloud login credentials |
| `api_key_env` | string | no | Environment variable name containing the API key |
| `api_key_cmd` | string | no | Shell command that outputs the API key (e.g., `op read ...` for 1Password) |
| `context_window` | int | no | Context window size in tokens. Falls back to 128k if not set |
| `vision` | bool | no | Whether the model supports image input. Default `false`. When `true`, file references (images, PDFs) are attached as multimodal content. When `false`, file paths are resolved so the AI can read them via tools |
| `temperature` | float | no | Default temperature for this model |
| `max_tokens` | int | no | Default max output tokens |
| `timeout` | int | no | Request timeout in seconds. Default 120 |

> **API key resolution order:** `api_key` (direct) → `api_key_env` (env var) → `api_key_cmd` (shell command). The special value `cloud_token` resolves to your Ember Cloud login credentials (from `/login`).

### Comparison with Claude Code

Claude Code uses a simple alias map (`"sonnet"` → `"claude-sonnet-4-6"`) because it only supports Anthropic models. Ember Code needs a full registry because it's multi-provider — each model name must resolve to a provider class, endpoint URL, model ID, and credentials.

| Aspect | Claude Code | Ember Code |
|---|---|---|
| Model resolution | Alias map (string → string) | Config-driven registry (name → provider + URL + key) |
| First-party API | `ANTHROPIC_API_KEY` | `/login` device-flow (Ember hosted MiniMax M2.7) |
| Hosted endpoint | `api.anthropic.com` | `api.ignite-ember.sh` |
| AWS Bedrock | `CLAUDE_CODE_USE_BEDROCK` | BYOM registry entry with Bedrock URL |
| Google Vertex | `CLAUDE_CODE_USE_VERTEX` | BYOM registry entry with Vertex URL |
| Custom base URL | `ANTHROPIC_BASE_URL` | `url` field per registry entry |
| Adding new models | Not supported (Anthropic only) | Add a registry entry in config |
| Key helper script | `apiKeyHelper` | `api_key_env` or `api_key_cmd` per model |

The key difference: Claude Code only supports Anthropic models through different providers. Ember Code supports **any model from any provider** — MiniMax, OpenAI, Anthropic, Groq, local Ollama, etc. The config-driven registry means adding a new provider is a config change, not a code change.

---

## Config File Format

```yaml
# .ember/config.yaml

# Model configuration
#
# Model resolution order:
#   1. User-provided custom models (BYOM - Bring Your Own Model)
#   2. Ember Code hosted models (requires Ember Code account)
#
# If no custom model is configured, Ember Code uses its own hosted
# MiniMax M2.7 endpoint. You need an Ember Code account for this.
# Sign up at https://ignite-ember.sh (free tier available).

models:
  default: "MiniMax-M2.7"             # Default model for most agents
  fast: "MiniMax-M2.7-highspeed"      # Fast model (~100 TPS, 2x cost)
  max_context_window: 200000          # Hard ceiling for context usage (see below)

  # Model registry: maps model names (used in agent .md files) to providers.
  # Built-in entries (MiniMax-M2.7, MiniMax-M2.7-highspeed) route through
  # the Ember hosted endpoint and are always available.
  # Add entries here to override built-ins or register new models.
  registry:
    # Example: Gemini (direct API key)
    # gemini-2.5-flash:
    #   provider: openai_like
    #   model_id: gemini-2.5-flash
    #   url: https://generativelanguage.googleapis.com/v1beta/openai/
    #   api_key: AIzaSy...

    # Example: OpenAI (env var)
    # gpt-4o:
    #   provider: openai_like
    #   model_id: gpt-4o
    #   url: https://api.openai.com/v1
    #   api_key_env: OPENAI_API_KEY

    # Example: Anthropic (shell command)
    # claude-sonnet:
    #   provider: openai_like
    #   model_id: claude-sonnet-4-6
    #   url: https://api.anthropic.com/v1
    #   api_key_cmd: "op read op://Dev/anthropic/api-key"

    # Example: local Ollama model (no key needed)
    # local-llama:
    #   provider: openai_like
    #   model_id: llama3.3:70b
    #   url: http://localhost:11434/v1

    # Example: OpenRouter
    # openrouter-minimax:
    #   provider: openai_like
    #   model_id: minimax/minimax-m2.7
    #   url: https://openrouter.ai/api/v1
    #   api_key_env: OPENROUTER_API_KEY

# Permission levels
permissions:
  # "ask" = confirm before use, "allow" = auto-allow, "deny" = block
  file_write: "ask"
  file_read: "allow"
  shell_execute: "ask"
  shell_restricted: "allow"       # read-only commands (rg, find, tree)
  web_search: "allow"
  web_fetch: "allow"
  git_push: "ask"
  git_destructive: "ask"          # force-push, reset --hard, etc.

# Safety
safety:
  protected_paths:                 # Paths that cannot be written to
    - ".env"
    - ".env.*"
    - "*.pem"
    - "*.key"
    - "credentials.*"
    - "secrets.*"
  blocked_commands:                # Shell commands that are always blocked
    - "rm -rf /"
    - ":(){ :|:& };:"
  max_file_size_kb: 500            # Max file size for reads (KB)
  require_confirmation:            # Actions that always require confirmation
    - "git push"
    - "git push --force"
    - "npm publish"
    - "pip install"

# Memory & storage
storage:
  # Backend: "sqlite" (default, local), "postgres", "mongodb", "redis", "dynamodb", etc.
  # Agno supports 15+ storage backends. SQLite is the default for zero-config local use.
  # Use a remote backend (postgres, mongodb, etc.) to sync sessions across devices.
  backend: "sqlite"
  session_db: "~/.ember/sessions.db"   # SQLite path (when backend=sqlite)
  memory_db: "~/.ember/memory.db"      # SQLite path (when backend=sqlite)
  # Remote backend example (uncomment to sync across devices):
  # backend: "postgres"
  # db_url: "postgresql://user:pass@host:5432/ember_code"
  audit_log: "~/.ember/audit.log"      # Tool execution log
  max_history_runs: 10000              # Effectively unlimited (auto-compact handles context)

# Context compression
#
# Ember Code uses a two-layer compression strategy to keep conversations
# within a token budget:
#
# 1. **Tool result compression** (Agno CompressionManager)
#    Automatically compresses tool outputs (file contents, shell output, etc.)
#    when context usage reaches 80% of the effective context window.
#
# 2. **Conversation history compaction** (Ember's compact_if_needed)
#    At the same 80% threshold, Ember generates a session summary covering
#    older turns, then trims the verbatim history to keep only recent turns.
#    Subsequent compactions halve the kept turns (minimum 2).
#
# The effective context window is:
#
#     effective = min(model_context_window, max_context_window)
#
# So if a model reports a 1M-token window, compression still triggers at
# 80% of 200k (160k tokens) by default. If a model has a *smaller* window
# (e.g. 32k), that smaller value is used — the ceiling never inflates the
# actual model limit.
#
# Tune this based on your cost/quality trade-off:
# - Lower values (e.g. 100000) → more aggressive compression, lower cost
# - Higher values (e.g. 500000) → more context retained, higher cost
# - Set to a very large number to effectively disable the ceiling
#
# The setting is models.max_context_window (shown above in the models section).

# Project rules
# Controls cross-tool compatibility for project instruction files.
# When cross_tool_support is true, Ember Code reads CLAUDE.md files
# in addition to ember.md at every level (root + subdirectories).
rules:
  cross_tool_support: true         # also reads CLAUDE.md files (set false to disable)

# Project context
context:
  project_file: "ember.md"         # Project instructions file
  ignore_patterns:                 # Patterns to exclude from search
    - "node_modules/"
    - ".git/"
    - "__pycache__/"
    - "*.pyc"
    - ".venv/"
    - "dist/"
    - "build/"

# Orchestration
orchestration:
  max_nesting_depth: 5             # Max recursive sub-team depth
  max_total_agents: 20             # Max agents per request
  sub_team_timeout: 600            # Seconds before sub-team times out
  max_task_iterations: 10          # Max iterations for tasks-mode teams

# Task scheduling (background jobs)
scheduler:
  poll_interval: 30                # Seconds between checking for due tasks
  task_timeout: 300                # Max seconds per scheduled task (5 min)
  max_concurrent: 1                # Max tasks running at once (bounded by semaphore)

# Agents & Skills
# By default, Ember Code scans its own directories AND Claude Code / Codex directories.
# Set cross_tool_support: false to only scan Ember Code directories.
agents:
  cross_tool_support: true         # also scans .claude/agents/, .codex/, etc.
  # Ember dirs (always scanned):
  #   .ember/agents/              (project, committed)
  #   .ember/agents.local/        (project, gitignored)
  #   ~/.ember/agents/            (user global)
  # Cross-tool dirs (when cross_tool_support: true):
  #   .claude/agents/             (Claude Code project)
  #   ~/.claude/agents/           (Claude Code user global)
  #   AGENTS.md / .codex/         (Codex project)
  #   ~/.codex/                   (Codex user global)

skills:
  cross_tool_support: true         # also scans .claude/skills/
  auto_trigger: true               # Allow Orchestrator to auto-trigger skills
  # Ember dirs (always scanned):
  #   .ember/skills/              (project, committed)
  #   .ember/skills.local/        (project, gitignored)
  #   ~/.ember/skills/            (user global)
  # Cross-tool dirs (when cross_tool_support: true):
  #   .claude/skills/             (Claude Code project)
  #   ~/.claude/skills/           (Claude Code user global)

# Embeddings — BYOM registry (same pattern as models.registry)
# Resolution: user registry → built-in → provider:model_id
embeddings:
  default: "local"                 # Default embedder name
  registry:
    # Example: use Voyage AI
    # voyage:
    #   provider: openai_compatible
    #   model_id: voyage-3
    #   url: https://api.voyageai.com/v1
    #   api_key_env: VOYAGE_API_KEY
    #   dimensions: 1024

    # Example: use OpenAI native embedder
    # openai-embed:
    #   provider: openai
    #   model_id: text-embedding-3-small
    #   api_key_env: OPENAI_API_KEY
    #   dimensions: 1536

    # Example: local Ollama embeddings
    # local-embed:
    #   provider: openai_compatible
    #   model_id: nomic-embed-text
    #   url: http://localhost:11434/v1
    #   dimensions: 768

# Knowledge base (requires: pip install ember-code[knowledge])
knowledge:
  enabled: true                    # Enable ChromaDB knowledge base
  collection_name: "ember_knowledge"  # ChromaDB collection name
  chroma_db_path: "~/.ember/chromadb" # Path to ChromaDB storage
  max_results: 10                  # Max search results returned
  embedder: "local"                # Embedder registry name (or provider:model_id)
  share: true                      # Sync knowledge to a git-friendly YAML file
  share_file: ".ember/knowledge.yaml"  # Knowledge file path (relative to project)
  auto_sync: true                  # Auto-sync on session start/end

# Learning
learning:
  enabled: true                    # Enable learning across sessions
  user_profile: true               # Build user preference profiles
  user_memory: true                # Persist user-specific memories
  session_context: true            # Carry session context forward
  entity_memory: false             # Track entities across conversations
  learned_knowledge: false         # Accumulate learned knowledge

# Reasoning tools
reasoning:
  enabled: false                   # Add think/analyze tools to agents
  add_instructions: true           # Include reasoning instructions in prompt
  add_few_shot: false              # Include few-shot examples

# Guardrails
guardrails:
  pii_detection: true              # Detect PII in prompts (pre-hook)
  prompt_injection: false          # Detect injection attempts (pre-hook)
  moderation: false                # OpenAI moderation API (pre-hook)

# Agent evaluations
evals:
  judge_model: "MiniMax-M2.7"       # Model for AccuracyEval LLM-as-judge
  num_iterations: 3                  # Default AccuracyEval iterations per case
  accuracy_threshold: 7.0            # Default passing score (0-10 scale)
  timeout_per_case: 30               # Seconds per eval case

# Display
display:
  markdown: true                   # Render markdown in terminal
  show_tool_calls: true            # Show which tools agents are using
  show_routing: false              # Show Router agent decisions
  show_reasoning: false            # Show reasoning chain steps
  color_theme: "auto"              # auto, dark, light
```

## Environment Variables

Authentication is handled by `/login` — no API key environment variables needed for Ember hosted models. The default model is set via `models.default` in your config, and agents can override it per-agent in their `.md` files.

> **Note:** BYOM API keys (OpenAI, Anthropic, etc.) are configured per model in your registry via `api_key_env`, `api_key`, or `api_key_cmd` — you choose the variable name yourself.

## CLI Flags

```bash
# Model selection
ignite-ember --model MiniMax-M2.7
ignite-ember --model MiniMax-M2.7-highspeed  # faster variant
ignite-ember --model gpt-4o                  # use OpenAI

# Safety modes
ignite-ember --no-web                # disable web access
ignite-ember --read-only             # no file modifications

# Display
ignite-ember --verbose               # show routing + reasoning
ignite-ember --quiet                 # minimal output
ignite-ember --no-color              # disable colors

# TUI mode (default — use --no-tui to fall back to plain Rich CLI)
ignite-ember --no-tui                # disable TUI, use plain Rich CLI output

# Session
ignite-ember --resume                # resume last session
ignite-ember --resume <session-id>   # resume specific session

# Direct execution
ignite-ember -m "add tests for auth module"   # non-interactive single task
cat src/auth.py | ignite-ember -p              # pipe stdin as context
```

## Project Instructions (Hierarchical Rules)

Ember Code loads project instructions from multiple levels, merging them top-down. This is similar to Claude Code's `CLAUDE.md` system.

### Rules Hierarchy

(Most general → most specific)

1. **User-level** — `~/.ember/rules.md` (global rules for all projects)
2. **Project root** — `ember.md` at the project root
3. **Subdirectory** — `ember.md` in any parent directory between the current working file and the project root

At each level, rules are merged additively. Subdirectory rules add specificity without overriding root rules.

### CLAUDE.md Compatibility

When `rules.cross_tool_support` is `true`, Ember Code also reads `CLAUDE.md` files at every level (root and subdirectories), in addition to `ember.md`. If both files exist in the same directory, their contents are merged.

```yaml
# .ember/config.yaml
rules:
  cross_tool_support: true   # read CLAUDE.md files alongside ember.md
```

### Example

```
my-project/
├── ember.md                  # root rules (always loaded)
├── CLAUDE.md                 # loaded when rules.cross_tool_support: true
├── src/
│   ├── ember.md              # subdirectory rules for src/
│   └── auth/
│       ├── ember.md          # subdirectory rules for src/auth/
│       └── middleware/
│           └── handler.py    # ← working file
└── ~/.ember/rules.md         # user-level global rules
```

When editing `handler.py`, the merged context includes:
1. `~/.ember/rules.md` (user rules)
2. `my-project/ember.md` + `CLAUDE.md` (project root)
3. `src/ember.md` (subdirectory)
4. `src/auth/ember.md` (subdirectory, most specific)

### Root-Level Example

```markdown
# ember.md — Project: My API

## Stack
- Python 3.12, FastAPI, SQLAlchemy, PostgreSQL
- Tests: pytest with fixtures in conftest.py

## Conventions
- Use snake_case for all Python identifiers
- All endpoints must have OpenAPI docstrings
- Database models go in src/models/
- API routes go in src/routes/

## Important
- Never modify migration files after they've been applied
- The .env file contains production credentials — never read or log it
- Run `make test` to execute the full test suite
```

### Subdirectory Example

```markdown
# src/auth/ember.md

## Auth Module Rules
- All auth endpoints must validate JWT tokens
- Never log token values, only token IDs
- Rate limiting is required on all login endpoints
```

## Progress Tracking (TODO.md)

Ember Code uses a two-level TODO system for persistent progress tracking across sessions.

### Two Levels

- **Root `.ember/TODO.md`** — high-level goals and milestones. Automatically loaded into agent context at session start. Tracks *what* needs to happen, not *how*.
- **Subdirectory `.ember/TODO.md`** (e.g., `src/auth/.ember/TODO.md`) — detailed implementation steps for that specific area. Not auto-loaded; agents read them when working in that directory.

The root TODO is the map. Subdirectory TODOs are the turn-by-turn directions.

### Example

**Root** (`.ember/TODO.md`):
```markdown
# TODO — Add authentication module

> Started: 2026-03-28 | Last updated: 2026-04-01

- [x] User model and migration
- [ ] Auth endpoints (login, logout, refresh)
- [ ] Integration tests
- [ ] API documentation
```

**Subdirectory** (`src/auth/.ember/TODO.md`):
```markdown
# TODO — Auth endpoints

> Last updated: 2026-04-01

- [x] POST /login — validate credentials, return JWT + refresh token
- [x] POST /logout — revoke refresh token in Redis
- [ ] POST /refresh — rotate refresh token, return new JWT
- [ ] Rate limiting on /login (5 attempts per minute)
- [ ] Add 401 response schema to OpenAPI docs

## Notes
Using PyJWT with RS256. Refresh tokens stored in Redis with 7-day TTL.
```

### TODO.md vs Agno Task Mode

| | `.ember/TODO.md` | Agno task mode (`spawn_team` with `mode="tasks"`) |
|---|---|---|
| **Lifetime** | Persistent — survives across sessions, commits, context resets | Ephemeral — exists only for the current team run |
| **Scope** | Big-picture progress across days/weeks | Task decomposition within a single run |
| **Visibility** | Human-readable file in `.ember/`, can be committed | In-memory, visible only during execution |
| **Who updates it** | Agents check off items as they work | Agno manages task state automatically |
| **Use case** | "Implement auth module" (multi-session) | "Write 3 test files in parallel" (one run) |

Both can be used together: TODO.md tracks the overall feature, Agno task mode orchestrates the work within each session.

## Permission Modes

Quick permission presets for common scenarios:

```bash
# Default — asks for writes and shell commands
ignite-ember

# Permissive — auto-allows edits, asks for shell
ignite-ember --accept-edits

# Strict — asks for everything including reads
ignite-ember --strict

# CI/CD — auto-allows everything (use with caution)
ignite-ember --auto-approve
```

## Custom Agents

Drop Python files in `.ember/agents/` to add project-specific agents:

```markdown
# .ember/agents/deploy.md
---
name: deploy
description: Handles deployment to staging and production environments
tools: Bash, Read, Glob
color: cyan

tags: [deploy, infrastructure, devops]
---

You handle deployment operations.

## Rules
- Always confirm before deploying to production
- Run smoke tests after deployment
- Show the deployment plan before executing
```

Custom agents are auto-discovered and added to the agent pool. If no `model` is specified, the config default (`models.default`) is used. To override, add `model: <name>` — the name is resolved through the model registry.

## Custom Tools

Drop Python files in `.ember/tools/` to add project-specific tools:

```python
# .ember/tools/docker_helpers.py
from agno.tools import tool

@tool(description="Build and run the Docker dev environment")
def docker_dev_up() -> str:
    """Start the development Docker containers."""
    import subprocess
    result = subprocess.run(
        ["docker-compose", "-f", "docker-compose.dev.yml", "up", "-d"],
        capture_output=True, text=True
    )
    return result.stdout + result.stderr
```

Custom tools are available to all agents that have `ShellTools` or `PythonTools`.
