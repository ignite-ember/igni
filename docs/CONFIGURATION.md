# Configuration

igni is configured through a layered system of config files, environment variables, and CLI flags.

## Configuration Hierarchy

(Highest priority first)

1. **CLI flags** — `--model`, `--no-web`, etc.
2. **Project local config** — `.igni/config.local.yaml` (gitignored, personal overrides)
3. **Project config** — `.igni/config.yaml` (committed to repo, shared with team)
4. **User config** — `~/.igni/config.yaml` (global, generated on first run)
5. **Defaults** — built-in sensible defaults

A project's config counts only once you trust the folder, and some keys it may never set — see [Workspace trust](#workspace-trust).

Each level deep-merges over the previous. The `~/.igni/config.yaml` file is automatically generated from defaults on first run via `yaml.dump(DEFAULT_CONFIG)` — a single source of truth.

### What in `.igni/` goes into git

`.igni/` holds both what a team shares and what is one person's. In a trusted
folder igni keeps a marked block in `.igni/.gitignore` (rewritten on every
start; lines outside it are left alone) that ignores only the personal and
runtime files:

| Ignored | Why |
|---|---|
| `*.local`, `*.local.*` — `agents.local/`, `skills.local/`, `settings.local.json`, `config.local.yaml` | one person's overrides |
| `agents.tmp/` | ephemeral agents; `/agents promote` keeps one ([AGENTS.md](AGENTS.md#the-lifecycle-create-promote-discard)) |
| `*.db` (and `-wal`/`-shm`/`-journal`) | sessions, memory, state |
| `credentials.json`, `workspace-trust.json`, `mcp-approved.json`, `mcp-tool-state.json` | credentials and this machine's decisions |
| `*.log`, `logs/`, `process_logs/`, `attachments/`, `worktrees/`, `projects/` | what a run leaves behind |
| `.initialized`, `.checksums.json`, `.update-check`, `*.new` | igni's bookkeeping, and an update it could not apply over your edit |
| `group-policy/` | the organisation's policy, pulled from the server |

Everything else is meant to be committed: `agents/`, `skills/`, `hooks/`,
`workflows/`, `output-styles/`, `tools/`, `evals/`, `config.yaml`,
`settings.json`, `rules.md`, `.mcp.json`, `plugins.json`, and the
`.gitignore` itself. Most runtime files live under `~/.igni`, not the
project's `.igni`; they are listed anyway, for a home directory kept in git.

## Models & Authentication

igni needs an LLM to run. Models are resolved through a **config-driven registry** — agent `.md` files reference models by name (e.g., `model: MiniMax-M2.7`), and the registry maps that name to a provider, endpoint URL, model ID, and API key.

### Model Registry

The registry has two layers:

1. **Models your server advertises** — fetched from `/v1/chat/models` on
   the igni server you are signed in to, and merged into the registry at
   session start. Yours, not a vendor's: the URL comes from `api_url`,
   which has no default.
2. **Custom models (BYOM)** — entries you write, which always win. A
   same-named entry from the server is a no-op, so pinning a different
   timeout or provider survives every startup merge.

**There is no built-in registry.** This section said "Built-in registry
(hardcoded in defaults.py, shown here for reference)" and showed a
MiniMax entry pointing at a vendor host. There is no `defaults.py`,
`Settings().models.registry` is empty, and nothing ships a model
pointing anywhere — which is what being self-hosted means here. An
install that has never signed in has whatever you wrote and nothing
else.

A fetched entry looks like this once merged:

```yaml
# .igni/config.yaml
models:
  registry:
    MiniMax-M2.7:
      provider: openai_like
      model_id: MiniMax-Text-01
      url: https://api.your-igni-server.example/v1
      api_key: cloud_token    # use the credentials from `igni login`
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
2. Is <name> in built-in registry?                  → use it (your igni server)
3. Does <name> contain ":" (e.g., "openai:gpt-4o")? → parse as provider:model_id
4. None of the above?                               → error: unknown model
```

### Option 1: igni Account (default, zero-config)

Sign up at **https://ignite-ember.sh**. All built-in models route through the your igni server. Free tier available.

```bash
igni                        # then type /login at the prompt (device-flow login)
```

**Device-flow login:** Running `/login` opens your browser to your igni server's portal. After you authenticate, the CLI automatically receives your access token and model credentials. Platform credentials are saved to `~/.igni/credentials.json` (token, email, expiry). Model credentials (API key, URL) are saved to `~/.igni/config.yaml`.

No manual model configuration needed — the built-in registry handles everything.

#### Cloud model auto-discovery

When you're logged in, igni fetches the deduplicated `(model, base_url)` catalogue from your your igni server key pool (`GET /v1/chat/models`) and merges each entry into the local registry on session start. Opening the model picker (`/model`) refreshes the catalogue so models added on the portal show up without restarting the CLI.

Cloud-discovered entries always use `api_key: cloud_token` (your login credentials) and are tagged with `source: "cloud"`. **User-defined entries always win** — if your config already defines `gpt-4o`, the cloud entry with the same name is skipped, so pinned timeouts and provider overrides survive.

Failure modes are all soft: missing token, network error, timeout (3 s), and non-200 responses each degrade silently to whatever's already in the local registry.

### Option 2: Bring Your Own Model (BYOM)

Add entries to `models.registry` in your config. These override built-in entries with the same name, or add entirely new models.

```yaml
# .igni/config.yaml
models:
  registry:
    # your igni server model — uses login credentials
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

    # Claude, native Messages API, key from a shell command (e.g., 1Password, vault)
    claude:
      provider: anthropic
      model_id: claude-opus-5-5
      api_key_cmd: "op read op://Dev/anthropic/api-key"
      context_window: 1000000
      vision: true

    # Claude through an Anthropic-compatible gateway inside your network
    claude-internal:
      provider: anthropic
      model_id: claude-opus-5-5
      url: https://llm-gateway.bank.internal/anthropic
      api_key_env: LLM_GATEWAY_KEY
      context_window: 1000000
      thinking: {type: adaptive, display: summarized}
      effort: high
      max_tokens: 32000
```

Now agents can reference `model: gpt-4o` or `model: claude` in their `.md` files.

There are three providers:

- **`openai_like`** — any OpenAI-compatible chat-completions API: MiniMax, OpenAI, Groq, Together AI, OpenRouter, Ollama, vLLM, llama.cpp.
- **`anthropic`** — Claude over Anthropic's native Messages API, or any Anthropic-compatible API ("Anthropic-like": a gateway or proxy that serves `POST /v1/messages`). Native means prompt caching, extended thinking, and images and PDFs sent as Claude's own content blocks, none of which survive Anthropic's OpenAI-compatibility layer. Use it for Claude rather than `openai_like` with Anthropic's `/v1` URL.
- **`gemini`** — Google's Gemini API through `google-genai`.

#### The `anthropic` provider

- **`url`** is optional. Without it requests go to `https://api.anthropic.com`; with it they go to your gateway instead. Give the base URL — the SDK adds `/v1/messages` itself, and a trailing `/v1` is dropped if you write one.
- **Keys** resolve exactly as for `openai_like` (`api_key`, `api_key_env`, `api_key_cmd`, `cloud_token`), and the key is sent as `x-api-key`. With `cloud_token`, igni server's current token also goes as `Authorization: Bearer`, read at each request.
- **Nothing is taken from the machine's environment.** The Anthropic SDK would otherwise read `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` and an `ant` CLI profile; igni passes the endpoint and the key explicitly, so the registry entry is the whole story. To use `ANTHROPIC_API_KEY`, say so: `api_key_env: ANTHROPIC_API_KEY`.
- **Prompt caching** is on by default: the system prompt and the tool list carry `cache_control`, so each turn re-reads them from the cache. `prompt_caching: false` turns it off.
- **Thinking** is off unless the model does it by default. `thinking` is passed to the API unchanged — `{type: adaptive}` on current models (Opus 4.6 and later), `{type: enabled, budget_tokens: N}` only on older ones that still take it. `effort` (`low`, `medium`, `high`, `xhigh`, `max`) sets `output_config.effort`. Current models reject `temperature`; leave it unset for them.
- **`max_tokens`** defaults to 8192 (Agno's default) when unset.
- **`context_window`**: set it. Without it igni assumes 128k.

### Registry Entry Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `provider` | string | yes | `openai_like` for any OpenAI-compatible API, `anthropic` for Claude or an Anthropic-compatible API, `gemini` for Gemini |
| `model_id` | string | yes | Model identifier sent to the API (e.g., `MiniMax-Text-01`, `gpt-4o`, `claude-opus-5-5`) |
| `url` | string | yes* | API base URL (e.g., `https://api.openai.com/v1`). Omit only for OpenAI models that use the default endpoint. *Optional for `anthropic`, which defaults to `https://api.anthropic.com` |
| `api_key` | string | no | API key value, or `cloud_token` to use your igni server login credentials |
| `api_key_env` | string | no | Environment variable name containing the API key |
| `api_key_cmd` | string | no | Shell command that outputs the API key (e.g., `op read ...` for 1Password) |
| `context_window` | int | no | Context window size in tokens. Falls back to 128k if not set |
| `vision` | bool | no | Whether the model supports image input. Default `false`. When `true`, file references (images, PDFs) are attached as multimodal content. When `false`, file paths are resolved so the AI can read them via tools |
| `temperature` | float | no | Default temperature for this model |
| `max_tokens` | int | no | Default max output tokens |
| `timeout` | int | no | Request timeout in seconds. Default 60 |
| `thinking` | object | no | `anthropic` only. The Messages API `thinking` object, sent as written (e.g. `{type: adaptive}`) |
| `effort` | string | no | `anthropic` only. `output_config.effort`: `low`, `medium`, `high`, `xhigh` or `max` |
| `prompt_caching` | bool | no | `anthropic` only. `cache_control` on the system prompt and tools. Default `true` |

> **API key resolution order:** `api_key` (direct) → `api_key_env` (env var) → `api_key_cmd` (shell command). The special value `cloud_token` resolves to your igni server login credentials (from `/login`), and is sent only to your igni server: an entry whose requests go to any other origin than `api_url` (scheme, host and port) gets no token, whichever config file it came from.

### Comparison with Claude Code

Claude Code uses a simple alias map (`"sonnet"` → `"claude-sonnet-4-6"`) because it only supports Anthropic models. igni needs a full registry because it's multi-provider — each model name must resolve to a provider class, endpoint URL, model ID, and credentials.

| Aspect | Claude Code | igni |
|---|---|---|
| Model resolution | Alias map (string → string) | Config-driven registry (name → provider + URL + key) |
| First-party API | `ANTHROPIC_API_KEY` | `/login` device-flow (server-hosted MiniMax M2.7) |
| Hosted endpoint | `api.anthropic.com` | `api.ignite-ember.sh` |
| AWS Bedrock | `CLAUDE_CODE_USE_BEDROCK` | BYOM registry entry with Bedrock URL |
| Google Vertex | `CLAUDE_CODE_USE_VERTEX` | BYOM registry entry with Vertex URL |
| Custom base URL | `ANTHROPIC_BASE_URL` | `url` field per registry entry |
| Adding new models | Not supported (Anthropic only) | Add a registry entry in config |
| Key helper script | `apiKeyHelper` | `api_key_env` or `api_key_cmd` per model |

The key difference: Claude Code only supports Anthropic models through different providers. igni supports **any model from any provider** — MiniMax, OpenAI, Anthropic (natively, through `provider: anthropic`), Groq, local Ollama, etc. The config-driven registry means adding a new provider is a config change, not a code change.

---

## Config File Format

```yaml
# .igni/config.yaml

# Model configuration
#
# Model resolution order:
#   1. User-provided custom models (BYOM - Bring Your Own Model)
#   2. igni hosted models (requires igni account)
#
# If no custom model is configured, igni uses its own hosted
# MiniMax M2.7 endpoint. You need an igni account for this.
# Sign up at https://ignite-ember.sh (free tier available).

models:
  default: "MiniMax-M2.7"             # Default model for most agents
  fast: "MiniMax-M2.7-highspeed"      # Fast model (~100 TPS, 2x cost)
  max_context_window: 200000          # Hard ceiling for context usage (see below)

  # Model registry: maps model names (used in agent .md files) to providers.
  # Built-in entries (MiniMax-M2.7, MiniMax-M2.7-highspeed) route through
  # the your igni server and are always available.
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

    # Example: Claude, native Messages API (shell command)
    # claude:
    #   provider: anthropic
    #   model_id: claude-opus-5-5
    #   api_key_cmd: "op read op://Dev/anthropic/api-key"

    # Example: Claude through an Anthropic-compatible gateway
    # claude-internal:
    #   provider: anthropic
    #   model_id: claude-opus-5-5
    #   url: https://llm-gateway.bank.internal/anthropic
    #   api_key_env: LLM_GATEWAY_KEY

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
  file_read: "allow"             # no effect: igni has no read tool (SECURITY.md)
  shell_execute: "ask"
  shell_restricted: "allow"       # no effect: reads ask in default mode (SECURITY.md)
  web_search: "ask"               # a request out of the network: asked each time
  web_fetch: "ask"
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
  session_db: "~/.igni/sessions.db"   # SQLite path (when backend=sqlite)
  memory_db: "~/.igni/memory.db"      # SQLite path (when backend=sqlite)
  # Remote backend example (uncomment to sync across devices):
  # backend: "postgres"
  # db_url: "postgresql://user:pass@host:5432/ember_code"
  audit_log: "~/.igni/audit.log"      # Tool execution log
  max_history_runs: 10000              # Effectively unlimited (auto-compact handles context)

# Context compression
#
# igni uses a two-layer compression strategy to keep conversations
# within a token budget:
#
# 1. **Tool result compression** (Agno CompressionManager)
#    Automatically compresses tool outputs (file contents, shell output, etc.)
#    when context usage reaches 80% of the effective context window.
#
# 2. **Conversation history compaction** (igni's compact_if_needed)
#    At the same 80% threshold, igni generates a session summary covering
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
# When cross_tool_support is true, igni reads CLAUDE.md files
# in addition to igni.md at every level (root + subdirectories).
rules:
  cross_tool_support: true         # also reads CLAUDE.md files (set false to disable)

# Project context
context:
  project_file: "igni.md"         # Project instructions file
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
# By default, igni scans its own directories AND Claude Code / Codex directories.
# Set cross_tool_support: false to only scan igni directories.
agents:
  cross_tool_support: true         # also scans .claude/agents/, .codex/, etc.
  # igni dirs (always scanned):
  #   .igni/agents/              (project, committed)
  #   .igni/agents.local/        (project, gitignored)
  #   ~/.igni/agents/            (user global)
  # Cross-tool dirs (when cross_tool_support: true):
  #   .claude/agents/             (Claude Code project)
  #   ~/.claude/agents/           (Claude Code user global)
  #   AGENTS.md / .codex/         (Codex project)
  #   ~/.codex/                   (Codex user global)

skills:
  cross_tool_support: true         # also scans .claude/skills/
  auto_trigger: true               # Allow Orchestrator to auto-trigger skills
  # igni dirs (always scanned):
  #   .igni/skills/              (project, committed)
  #   .igni/skills.local/        (project, gitignored)
  #   ~/.igni/skills/            (user global)
  # Cross-tool dirs (when cross_tool_support: true):
  #   .claude/skills/             (Claude Code project)
  #   ~/.claude/skills/           (Claude Code user global)

# Embeddings are not configurable, and not igni's. igni has no embedder:
# the CodeIndex extension embeds its local index with all-MiniLM-L6-v2
# (384 dimensions), fixed, because its vector index is fixed to match.
#
# This section previously documented an `embeddings:` block with a
# `default` and a BYOM `registry`, mirroring `models.registry`. No such
# setting exists, and `Settings` drops unknown keys in silence — so a
# config file written from that documentation did nothing at all, with
# no error to say so.

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
  timeout_per_case: 600              # Seconds per eval case; 0 disables

# Display
display:
  markdown: true                   # Render markdown in terminal
  show_tool_calls: true            # Show which tools agents are using
  show_routing: false              # Show Router agent decisions
  show_reasoning: false            # Show reasoning chain steps
  color_theme: "auto"              # auto, dark, light
```

## Organisation group policy

An organisation's admins publish settings, agents, hooks and MCP servers for
each group on igni server. igni fetches the signed-in person's pack at start,
at sign-in and every `group_policy.poll_seconds`, caches it under
`<storage.data_dir>/group-policy/`, and applies its `settings` above CLI flags
and below the managed-settings file at the next start.

### How a pack is verified

igni server signs each pack as a compact JWS, using the key it signs igni
tokens with and publishes at `<api_url>/.well-known/jwks.json` (`RS256`, `kid`
in the header, `typ: igni-group-policy+jwt`). The payload carries the pack
(`policy`), `iss` (the server's public URL), `sub` (the person it was served
to), `org`, `iat` and a monotonic `version`. It has no expiry, so a cached pack
keeps working offline.

igni stores the JWS exactly as received (`pack.jws`) and verifies it both when
it is fetched and every time it is read at start-up, before any of it is
merged:

- the signature, against a trusted key (below);
- `iss` against the configured `api_url`;
- `sub` against the person signed in (`~/.igni/credentials.json`);
- `version`: not older than the newest pack this install has accepted (a
  rollback is refused);
- `org`: when the signed-in session names an organisation, the pack's must be
  the same one.

A pack that is missing, unsigned, edited, signed by another key, issued by
another server, person or organisation, or older than one already applied is
**never merged**.

A group's hooks run on your machine outside the agent's permissions. Whether
they run is yours to decide with `hooks.group` (`run`, `ask` or `refuse`) in
your own `~/.igni/config.yaml` or with `--strict` (at least `ask`); the pack
cannot set it. See [Hooks](HOOKS.md#your-organisations-hooks-hooksgroup).

The pack's agents, hooks, MCP servers, scripts and plugin entries are files
under `group-policy/` that their loaders read directly. At every start, once
the pack has verified, igni rebuilds them from it: a deleted or edited org
agent or hook is written back, and a file or plugin directory added beside
them is removed. With no pack this install may apply, they are removed. A
plugin installed from a git `source_url` is not re-cloned at start-up; the
next fetch does that. If the rebuild fails, the settings still apply, unless
`group_policy.required` is on, in which case no session starts.

When a person has no pack (no organisation, or no group), igni server says
so in a signed answer (`no_pack: true`) rather than an empty response. igni
verifies it like a pack, then removes the cached pack and everything derived
from it. Because the answer carries a `version`, an older pack put back
afterwards is refused as a rollback. A verified "no pack" satisfies
`group_policy.required`. Only the signed content is applied; the plain fields in the
same response are ignored. The old unsigned `group_settings.json` is no longer
written or read, by igni or by the desktop app, which verifies the same JWS
itself before it reads `air_gapped`, `update_endpoint`, `update_check_ttl` or
`runtime_python` from a group's policy.

### Trusted keys, and what pinning buys

```yaml
# managed-settings.yaml only (or the environment, below)
group_policy:
  trusted_keys:          # RFC 7638 JWK thumbprints of igni server's signing keys
    - 3q2-7wABCDEF...    # igni server's `kid` for a key it generated is its thumbprint
  required: true         # no verified pack, no agent session
```

1. **Pinned keys**, `group_policy.trusted_keys`: thumbprints, computed by igni
   from the key itself (never read from the `kid`). The key material still comes
   from igni server's JWKS, but only a key with a pinned thumbprint is used.
2. **Otherwise**, igni server's JWKS, fetched over TLS when the pack is fetched
   (an unpinned JWKS over plain HTTP is trusted only from loopback), and cached
   beside the pack (`jwks.json`) for offline starts.

Without a pin, the cached JWKS lives in the same user-writable directory as the
pack. It stops a repository, a stray file or a forged pack on its own, but
**not the person who owns the machine** or malware running as them: whoever can
write the directory can replace the pack and the keys together. A pinned key
closes that: a forged pack needs a key whose thumbprint the administrator
pinned, which only igni server holds. Pin every key igni server publishes,
including a newly staged one before it activates (igni server stages a
rotated key before it signs with it), or packs signed after a rotation will not verify.

Deleting the pack is something no signature can prevent. That is what
`group_policy.required` is for.

### `group_policy.required`

- **On:** a missing or invalid pack is not ignored. If somebody is signed in,
  igni first tries to fetch a valid pack from igni server. If that fails, no
  agent session starts: `igni -m` exits with a message, and the desktop backend
  still comes up (so the person can sign in) but refuses every run, scheduled
  task and workflow agent step, saying to connect to igni server, sign in and
  restart igni.
- **Off (default):** an unusable pack is ignored with a warning and igni runs
  on local settings.

The desktop app's own four settings never fail closed: with no verified pack
it uses the managed-settings file and `~/.igni/config.yaml` alone. Put a
setting that must hold for the desktop shell in the managed-settings file.

### Managed-only settings

`group_policy.trusted_keys` and `group_policy.required` are honoured **only**
from the managed-settings file and the environment:

- `IGNI_GROUP_POLICY_TRUSTED_KEYS` — comma- or space-separated thumbprints.
  Replaces the managed file's list when set.
- `IGNI_GROUP_POLICY_REQUIRED=1` (also `true`, `yes`, `on`) — turns
  `required` on; it never turns it off.

The same keys in `~/.igni/config.yaml`, a project's config, a CLI flag or the
group pack itself are ignored with a warning: whoever could set them could pin
a key of their own, or make a missing policy harmless.

`workspace_trust` and `telemetry` (below) are organisation-only too, but the
group pack may set them as well as the managed file and the environment.

## Workspace trust

Opening a folder never runs its code or loosens igni's safety until you trust
the folder. A repository is written by whoever can commit to it, so the things
it brings that run code — or that would switch safety off — wait for trust.

**What an untrusted folder does not get**

| Gated | Where it comes from |
|---|---|
| Custom tools (imported = run) | `.igni/tools/*.py` |
| Hooks | `.igni/settings*.json`, `.claude/settings*.json` |
| Workflows (discovery evaluates each file) | `.igni/workflows/*.mjs`, and the deprecated fallback `.claude/workflows/*.mjs` |
| Language servers | `.lsp.json` |
| Monitors | `.monitors.json` |
| Project plugins | `.igni/plugins/`, `.claude/plugins/` |
| Markdown commands' `` !`cmd` `` tokens | `.igni/commands/`, `.claude/commands/` (the token becomes a "not run" marker; the command itself still loads) |
| `permissions.*` (rules, categories, `mode`) and `safety.*` (safety lists, Bypass guard) | `.igni/config.yaml`, `config.local.yaml`, `settings.json`, `settings.local.json` |
| A model's `api_key_cmd` (a program igni runs for the key) and `api_key_env` (a variable of your environment, sent to the URL the same file names) | `models.registry.*` in `.igni/config.yaml`, `config.local.yaml` |
| The key your own config gives a model the project re-points (a different `url` or `provider`): the entry keeps only a key the project itself gives | `models.registry.*` in `.igni/config.yaml`, `config.local.yaml` |

Keys a project may never set, trusted or not, stay as before: `network`,
`air_gapped`, `api_url`, `portal_url`, `update_endpoint`,
`plugins.marketplaces`, `storage.data_dir` — and now `workspace_trust`.

Everything else works untrusted: reading files, `igni.md` and rules, markdown
agents, skills and commands, and the rest of the project's config. A
project's `.mcp.json` is still governed by its own first-use approval
([MCP](MCP.md)). Your own `~/.igni` tools, hooks and settings, and your
organisation's, are not the folder's and are unaffected.

A trusted folder behaves as before, except that **managed values are final**:
a permission field set in the managed-settings file or the group policy cannot
be loosened by any lower tier, a trusted project included. A managed
`shell_execute: ask` holds against a project's `allow: [Bash]` — the category
is enforced even though `ask` is its default, and a lower tier's looser rules
for its tools are dropped.

**Where the decision is kept.** `~/.igni/workspace-trust.json` — per user,
never in the repository. Each entry is keyed by the folder's canonical path
(symlinks resolved) *and* its git remote, normalised (`git@github.com:acme/app.git`
→ `github.com/acme/app`, credentials removed). The remote is read from
`.git/config` directly; igni does not run `git` in a folder it does not trust.
So a moved or renamed clone asks again, and so does a different repository
checked out at a trusted path. A folder with no remote is keyed by its path.

**Being asked.** A decision is one of *Trust*, *Don't trust* or *This time
only* (trusted until the process exits). Until someone answers, the folder
runs untrusted.

- Desktop and web: the app asks when a folder nobody has decided about opens,
  showing the path and git remote. Answering *Trust* loads the folder's tools,
  hooks and settings without reopening it.
- `igni -m` on a terminal: asks the first time.
- Headless (`-p`, `-m` with no terminal, CI): never asks. The folder is
  untrusted unless you trusted it before, pass `--trust` (trusted for this run,
  if policy allows), or policy trusts it (`auto_trust_remotes`, below).
  `--worktree` carries the original folder's trust to its worktree for the run.

```bash
igni trust              # trust the current folder
igni trust ~/src/app    # …or another one
igni trust --revoke     # forget the decision; igni asks again
igni trust --list       # every folder you have decided about
igni -m "fix the build" --trust   # trusted for this run only
```

**Organisation policy.** Managed-only: honoured from the managed-settings file,
the group policy pack, or the environment. A `workspace_trust` block in
`~/.igni/config.yaml`, a project's config or on the CLI is ignored with a
warning.

```yaml
# managed-settings.yaml
workspace_trust:
  mode: trusted_remotes        # ask (default) | trusted_remotes | never
  trusted_remotes:             # shell wildcards; scheme, user and .git ignored
    - github.com/acme/*
    - gitlab.acme.internal/platform/*
  auto_trust_remotes: false    # true: matching remotes are trusted without asking (CI)
```

- `ask` — the person decides, per folder.
- `trusted_remotes` — only a folder whose remote matches may be trusted;
  any other (including one with no remote) stays untrusted, `--trust` and
  *Trust* are refused, and the prompt says why.
- `never` — every folder is untrusted.

`IGNI_WORKSPACE_TRUST_MODE` and `IGNI_WORKSPACE_TRUSTED_REMOTES`
(comma-separated) can only tighten: the mode applies when it is stricter than
the configured one, the remotes when no tier named any. An invalid policy
block fails closed (`never`).

## Air-gapped mode

One switch for a deployment whose network has no route out. Once set, igni
makes no connection outside the customer's network.

```yaml
# ~/.igni/config.yaml, a managed-settings file, or a group policy's settings
air_gapped: true

# Optional: a release mirror inside your network for the desktop app.
update_endpoint: https://releases.example.internal/igni/latest.json

# Desktop app only: the interpreter its backend runs on, provisioned by you
# (see "Providing the desktop runtime" below).
runtime_python: /opt/igni/venv/bin/python

# Optional: your own plugin marketplaces (local paths or internal git URLs).
plugins:
  marketplaces:
    - /srv/igni/plugin-marketplace
    - https://git.example.internal/tools/igni-plugins.git
```

Three ways to set it, any of which turns it on:

- **The key**, in `~/.igni/config.yaml` or a project's `.igni/config.yaml`.
  The desktop app's updater has no project when it checks, so for it use the
  user file, managed settings or group policy.
- **`IGNI_AIR_GAPPED=1`** in the environment (also `true`, `yes`, `on`). It
  only turns the mode on; an unset or false variable leaves the files' choice.
- **Managed settings or group policy**, for an organisation: `air_gapped: true`
  in the managed-settings file (`/Library/Application Support/igni/managed-settings.yaml`
  on macOS, `/etc/igni/managed-settings.yaml` on Linux,
  `%PROGRAMDATA%\igni\managed-settings.yaml` on Windows), which a user cannot
  override, or in a group's `settings` policy from igni server.

What it turns off:

| Flow | Without the switch | With `air_gapped: true` |
|---|---|---|
| Update check (PyPI) | `pypi.org`, every `update_check_ttl` seconds | No request |
| Desktop updater | `github.com/ignite-ember/igni/releases`, or `update_endpoint` | `update_endpoint` only; no request if none is set |
| Desktop first launch | Downloads uv, CPython, `ignite-ember` and the decision model | No download; the app runs on `runtime_python` (below), or refuses with a message naming it |
| Decision model (Laya, Hugging Face) | Downloaded on first use if not cached | Loaded only from the local cache, with the hub offline; refused with a message saying how to pre-seed it |
| Plugin marketplace at session start | Only `plugins.marketplaces`; `github.com/anthropics/claude-plugins-official` too if `plugins.default_marketplace: true` (off by default) | Not registered or refreshed; only local paths and `plugins.marketplaces` |
| `/plugin marketplace add`, `/plugin install`, `/plugin update`, the plugins panel, group-policy plugins | Any git URL | Local paths and hosts inside the network (below); others are refused before git runs |
| `igni ext install <url>` | Clones any git URL | Hosts inside the network only; others are refused before git runs |
| MCP servers reached by URL | Connected | Hosts inside the network only; others are refused as a failed connection |
| HTTP hooks | Sent | Hosts inside the network only; others are not sent and count as a failed hook |
| WebFetch, WebSearch (DuckDuckGo) | Asks before each request (`allow` to skip asking) | Denied; neither toolkit is built |
| Agno's run telemetry (`os-api.agno.com`) | Off; an explicit `AGNO_TELEMETRY=true` is honoured | Off; an explicit `AGNO_TELEMETRY=true` is overruled with a warning |
| OpenTelemetry export (`telemetry.otlp`) | None unless an administrator names a collector | Only to a collector inside the network; another is refused with a warning |

A host is **inside the network** if it is a local path or `file://`, loopback,
the host of `api_url`, `portal_url`, `update_endpoint` or `plugins.marketplaces`,
or listed under `network.internal_hosts` (exact hosts, or `*.suffix`
patterns):

```yaml
network:
  internal_hosts: ["git.acme.internal", "*.svc.acme.internal"]
```

A project's `.igni/config.yaml` and `config.local.yaml` may not set
`air_gapped`, `network`, `api_url`, `portal_url`, `update_endpoint` or
`plugins.marketplaces`: a repository is whoever wrote it, so it may neither
switch the mode off nor name a host as inside. Those keys are ignored there,
with a warning.

The plugin clone also has its own switch, independent of air-gapped mode and
off by default: `plugins.default_marketplace: true` has igni register
Anthropic's marketplace at session start, a clone from github.com. Air-gapped
mode ignores it.

`/plugin update` checks the URL a plugin was installed from (recorded in
`plugins.json` since this release; for older installs, the plugin's git
remote) against the same rule. A plugin with neither is not updated while
air-gapped: reinstall it from a local path or an internal host.

Not covered by the switch: what the agent runs as an ordinary program — shell
commands (`curl`, `pip`, `git`), MCP servers started as local processes, and
command hooks — and the model endpoints an administrator configures. Your
network limits those as it limits any program; run igni behind a deny-all
egress rule.

### Providing the desktop runtime

On first launch the desktop app downloads uv, CPython and `ignite-ember`,
which an air-gapped network cannot do. Provision them yourself and tell the app
where they are:

1. From your package mirror, install `ignite-ember[decisions]` at the app's
   version into a Python 3.12 interpreter, e.g.
   `python3.12 -m venv /opt/igni/venv && /opt/igni/venv/bin/pip install --index-url https://pypi.example.internal/simple "ignite-ember[decisions]==<version>"`.
2. Set `runtime_python: /opt/igni/venv/bin/python` in `~/.igni/config.yaml`
   or the managed-settings file, or `IGNI_RUNTIME_PYTHON` in the environment
   (which wins). No `IGNITE_EMBER_DEV` is needed. The app uses it as is and
   downloads nothing; an interpreter that is missing or cannot import
   `ember_code` is refused with a message naming the setting.
3. Pre-seed the decision model (next section).

### Pre-seeding the decision model

igni's decision model (Laya, `convaiinnovations/laya` on Hugging Face) answers
Bypass mode's last check and `host.decide`. Air-gapped, it is loaded only from
the local Hugging Face cache. On a machine that can reach huggingface.co, run
`HF_HOME=/tmp/laya python -m ember_code.prefetch_models` (with
`ignite-ember[decisions]` installed), then copy
`/tmp/laya/hub/models--convaiinnovations--laya` into the target machine's
cache (`~/.cache/huggingface/hub` by default), or set `HF_HOME` or
`HF_HUB_CACHE` to a local copy for the decisions service. A missing or
incomplete copy is refused with a message saying this; Bypass mode then asks
rather than allows.

## OpenTelemetry

igni can send traces and metrics to **your own** OpenTelemetry collector, over
OTLP. It is off by default and there is **no default endpoint**: with none
configured nothing is exported, and the OpenTelemetry SDK is not set up at all.
It needs the `otel` extra (`pip install 'ignite-ember[otel]'`); without it a
configured endpoint is ignored with a warning.

```yaml
# The managed-settings file, or a group policy's settings — not ~/.igni/config.yaml
telemetry:
  otlp:
    endpoint: http://otel-collector.acme.internal:4318   # the collector's base URL
    protocol: http/protobuf        # or grpc (then the endpoint is the gRPC address)
    headers:                       # sent with every export, for the collector's auth
      authorization: Bearer <token>
    service_name: igni
    capture_content: false         # the default; see below
```

**Who may set it.** Only the managed-settings file, the organisation's group
policy, and the environment. The same block in `~/.igni/config.yaml`, a
project's `.igni/config.yaml` or on the command line is ignored with a warning:
a repository that could name the collector could send whoever opens it's
traces to a host of its choosing. The environment uses the standard variables,
and fills only what the organisation left unset:

| Variable | Field |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `endpoint` |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `protocol` (`grpc` or `http/protobuf`) |
| `OTEL_EXPORTER_OTLP_HEADERS` | `headers`, as `key=value,key2=value2` (values URL-encoded) |
| `OTEL_SERVICE_NAME` | `service_name` |
| `IGNI_OTEL_CAPTURE_CONTENT=true` | `capture_content` |

**Air-gapped.** Under `air_gapped: true` the endpoint must be inside the network
— loopback, a host igni is configured to use, or one in `network.internal_hosts`
— or nothing is exported and a warning says why. The endpoint must be an
`http://` or `https://` URL.

**What is exported.** Spans for each agent or team run, model call and tool
call (OpenInference's Agno instrumentation), and for each httpx request igni
makes — which carry the trace context (`traceparent`) to igni server, so one
trace follows a turn across the deployment. Span attributes: the agent or team
name, the model id and provider, token counts, timings, the tool name, the
outcome, igni's permission decision for a tool call
(`igni.permission.decision`: `allow` or `deny`), the session id, and on a run
span the organisation and producer (`igni.org.id`, `igni.producer.id`). Three
metrics: `igni.agent.runs` (with `igni.agent.run.duration`), `igni.llm.tokens`
by direction, and `igni.tool.calls` by outcome (`ok`, `error`, `denied`).

**What is not, unless `capture_content: true`:** prompts, responses, file
content, tool arguments and results, error messages and stack traces, and full
URLs. Spans keep only an allow-listed set of attributes; anything else the
instrumentation records is dropped before export. With `capture_content: true`
spans are exported as recorded — prompts, code and tool arguments included —
so set it only for a collector that may hold your source.

This is not Agno's own telemetry. Agno posts a metadata event for every run to
`os-api.agno.com` unless told not to; igni sets `AGNO_TELEMETRY=false` in every
process before Agno is imported, passes `telemetry=False` to every agent and
team it builds, and hands the variable to the extension services it starts. An
explicit `AGNO_TELEMETRY=true` is honoured outside air-gapped mode, and
overruled (with a warning) under it.

## Environment Variables

Authentication is handled by `/login` — no API key environment variables needed for server-hosted models. The default model is set via `models.default` in your config, and agents can override it per-agent in their `.md` files.

> **Note:** BYOM API keys (OpenAI, Anthropic, etc.) are configured per model in your registry via `api_key_env`, `api_key`, or `api_key_cmd` — you choose the variable name yourself.

`IGNI_WORKSPACE_TRUST_MODE` / `IGNI_WORKSPACE_TRUSTED_REMOTES` tighten the
organisation's workspace-trust policy ([Workspace trust](#workspace-trust)).

`OTEL_EXPORTER_OTLP_ENDPOINT` and the other OTLP variables name your
OpenTelemetry collector ([OpenTelemetry](#opentelemetry)). `AGNO_TELEMETRY` is
`false` unless set; `true` sends Agno's own run telemetry to `os-api.agno.com`,
and is overruled under air-gapped mode.

`IGNI_AIR_GAPPED=1` turns on air-gapped mode (above). A backend whose settings
say `air_gapped: true` sets it for the processes it starts, so igni's decisions
service follows the same switch. `IGNI_RUNTIME_PYTHON` names the desktop app's
provisioned interpreter (above).

**igni's decisions service** (Laya, for Bypass mode's last check and `host.decide`):

| Variable | Where | Meaning |
|---|---|---|
| `IGNI_DECISIONS_URL` | backend | The node's shared decisions service. Unset: the backend starts its own on first use (needs `ignite-ember[decisions]`). |
| `IGNI_DECISIONS_TOKEN` | backend | The secret sent to that service. |
| `IGNI_DECISIONS_SECRET` | service | The secret the service requires; it will not start without one. |

Run the shared service with `python -m ember_code.decisions_server --host 0.0.0.0 --port 8730` (installed with the `decisions` extra). A backend image needs neither the extra nor torch.

**The project database.** A backend migrates a project's `state.db` itself when it is new or behind, so on a desktop there is nothing to do. Where a backend's volume is prepared ahead of time (the cloud), run `igni migrate` in the project directory (or with `--project-dir`, `--data-dir`) at provisioning: the backend then starts with a current database and never imports alembic.

## CLI Flags

```bash
# Model selection
ignite-ember --model MiniMax-M2.7
ignite-ember --model MiniMax-M2.7-highspeed  # faster variant
ignite-ember --model gpt-4o                  # use OpenAI

# Safety modes
ignite-ember --no-web                # disable web access
ignite-ember --read-only             # reads files and the web; anything that changes is refused

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

# Workspace trust
ignite-ember -m "…" --trust          # trust this folder for this run
ignite-ember trust [--revoke|--list] # record, forget or list decisions
```

## Project Instructions (Hierarchical Rules)

igni loads project instructions from multiple levels, merging them top-down. This is similar to Claude Code's `CLAUDE.md` system.

### Rules Hierarchy

(Most general → most specific)

1. **User-level** — `~/.igni/rules.md` (global rules for all projects)
2. **Project root** — `igni.md` at the project root
3. **Subdirectory** — `igni.md` in any parent directory between the current working file and the project root

At each level, rules are merged additively. Subdirectory rules add specificity without overriding root rules.

### CLAUDE.md Compatibility

When `rules.cross_tool_support` is `true`, igni also reads `CLAUDE.md` files at every level (root and subdirectories), in addition to `igni.md`. If both files exist in the same directory, their contents are merged.

```yaml
# .igni/config.yaml
rules:
  cross_tool_support: true   # read CLAUDE.md files alongside igni.md
```

### Example

```
my-project/
├── igni.md                  # root rules (always loaded)
├── CLAUDE.md                 # loaded when rules.cross_tool_support: true
├── src/
│   ├── igni.md              # subdirectory rules for src/
│   └── auth/
│       ├── igni.md          # subdirectory rules for src/auth/
│       └── middleware/
│           └── handler.py    # ← working file
└── ~/.igni/rules.md         # user-level global rules
```

When editing `handler.py`, the merged context includes:
1. `~/.igni/rules.md` (user rules)
2. `my-project/igni.md` + `CLAUDE.md` (project root)
3. `my-project/src/igni.md` (subdirectory)
4. `my-project/src/auth/igni.md` (subdirectory, most specific)

### Root-Level Example

```markdown
# igni.md — Project: My API

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
# my-project/src/auth/igni.md

## Auth Module Rules
- All auth endpoints must validate JWT tokens
- Never log token values, only token IDs
- Rate limiting is required on all login endpoints
```

## Progress Tracking (TODO.md)

igni uses a two-level TODO system for persistent progress tracking across sessions.

### Two Levels

- **Root `.igni/TODO.md`** — high-level goals and milestones. Automatically loaded into agent context at session start. Tracks *what* needs to happen, not *how*.
- **Subdirectory `.igni/TODO.md`** (e.g., `my-project/src/auth/.igni/TODO.md`) — detailed implementation steps for that specific area. Not auto-loaded; agents read them when working in that directory.

The root TODO is the map. Subdirectory TODOs are the turn-by-turn directions.

### Example

**Root** (`.igni/TODO.md`):
```markdown
# TODO — Add authentication module

> Started: 2026-03-28 | Last updated: 2026-04-01

- [x] User model and migration
- [ ] Auth endpoints (login, logout, refresh)
- [ ] Integration tests
- [ ] API documentation
```

**Subdirectory** (`my-project/src/auth/.igni/TODO.md`):
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

| | `.igni/TODO.md` | Agno task mode (`spawn_team` with `mode="tasks"`) |
|---|---|---|
| **Lifetime** | Persistent — survives across sessions, commits, context resets | Ephemeral — exists only for the current team run |
| **Scope** | Big-picture progress across days/weeks | Task decomposition within a single run |
| **Visibility** | Human-readable file in `.igni/`, can be committed | In-memory, visible only during execution |
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

# Strict — denies the agent anything without an allow rule, reads included,
# and asks before a hook your organisation's group policy ships runs
# (hooks.group: ask; headless, an unapproved one does not run)
ignite-ember --strict

# CI/CD — auto-allows everything (use with caution)
ignite-ember --auto-approve
```

## Custom Agents

Drop Python files in `.igni/agents/` to add project-specific agents:

```markdown
# .igni/agents/deploy.md
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

Drop Python files in `.igni/tools/` to add project-specific tools. They are imported — run — only once the folder is trusted ([Workspace trust](#workspace-trust)):

```python
# .igni/tools/docker_helpers.py
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
