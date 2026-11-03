# Quickstart

Get up and running with igni in under 5 minutes.

## Install

**Homebrew (recommended):**

```bash
brew install ignite-ember/tap/ignite-ember
```

**pip:**

```bash
pip install ignite-ember
```

**From source** (Python 3.12 — `pyproject.toml` pins `>=3.12,<3.13`; `uv`
fetches it if you do not have it):

```bash
git clone https://github.com/ignite-ember/igni.git
cd igni
uv venv                         # .venv on Python 3.12
uv pip install -e ".[dev]"      # `uv pip` needs the venv: without it, "No virtual environment found"
source .venv/bin/activate       # puts `igni` on your PATH
```

## What `igni` is on the command line

`igni` in a terminal runs **one task and exits** (`igni -m "…"`, or a pipe).
There is no terminal chat UI any more: run with no arguments and it says so.
The interactive chat — confirmations, the session list, `/login` — is the
desktop app and the VS Code and JetBrains extensions, which all run the same
backend and share `~/.igni`.

## Authenticate

**Option A: your igni server**

Point `api_url` at the server your team runs — in `~/.igni/config.yaml`; a
project's config may not name a server. Set `portal_url` too when the portal
(the sign-in page) is not on the API's origin, for example the API on port
7777 and the portal on 3000:

```yaml
# ~/.igni/config.yaml
api_url: https://igni-api.acme.example
portal_url: https://igni.acme.example   # only if it can't be derived from api_url
```

Then sign in once from the desktop app or an IDE extension (**Sign in to your
igni server**, or `/login` in its chat). It opens the portal in your browser
and writes `~/.igni/credentials.json`, which `igni -m` then uses — the models
your server routes to appear without any `models:` entry.

`/login` is not a shell command (`igni /login` answers `Error: No such
command`), and `igni -m /login` cannot wait for a browser, so it only says
where to sign in.

<!-- TODO: a terminal-only sign-in (`igni login`) — until it exists, a machine
with no desktop app or IDE needs the credentials file from one that has. -->

**Option B: Bring your own model**

Use OpenAI, Anthropic (Claude, natively), or any OpenAI- or Anthropic-compatible API. Two steps:

1. Set your API key:

```bash
export OPENAI_API_KEY=sk-...
```

2. Add the model to `~/.igni/config.yaml` (or a project's `.igni/config.yaml`):

```yaml
models:
  default: gpt-4o              # use this model by default

  registry:
    gpt-4o:
      provider: openai_like
      model_id: gpt-4o
      url: https://api.openai.com/v1
      api_key: sk-...              # or api_key_env: OPENAI_API_KEY
```

For Claude, the entry is `provider: anthropic` (the native Messages API; add `url` to go through a gateway):

```yaml
    claude:
      provider: anthropic
      model_id: claude-opus-5-5
      api_key_env: ANTHROPIC_API_KEY
```

That's it — agents will now use GPT-4o. See [Configuration](docs/CONFIGURATION.md) for more providers (Anthropic, Groq, Ollama, OpenRouter, etc.).

## First Run

```bash
cd your-project
igni -m "In one sentence, what does this project do?"
```

The first run in a folder:

1. Asks whether to **trust the folder** (on a terminal; headless it stays
   untrusted and says so). Until you trust it, the folder's own tools, hooks
   and permission settings are off — see
   [Workspace trust](docs/CONFIGURATION.md). `igni trust` records the answer
   outside a session; `--trust` trusts it for one run.
2. Writes `igni.md` (project instructions) and `.igni/` — `config.yaml`, the
   built-in agents in `.igni/agents/` and skills in `.igni/skills/`. Neither
   is gitignored for you; commit `igni.md`, and decide about `.igni/`.

### A headless run cannot ask you anything

`igni -m` has nobody to show a confirmation to, and igni reads files with
the shell, which asks by default. So out of the box the question above stops
at its first `cat` with *"Run paused for confirmation … the tool did not
run"* and exits 1. Decide up front what it may do:

```yaml
# ~/.igni/config.yaml — reads only: commands built entirely from read-only
# ones (cat, rg, ls, git diff, …) run; edits are refused; anything else stops
permissions:
  mode: plan
```

```yaml
# or name what may run, e.g. for "fix the failing test":
permissions:
  allow:
    - "Bash(python3 -m pytest *)"
    - "Bash(cat *)"
    - "Bash(rg *)"
    - "Bash(tail *)"      # models pipe into tail and head: `pytest -q 2>&1 | tail -20`
    - "Bash(head *)"
```

and pass `--accept-edits` to let it edit files. A rule covers one command: `Bash(cat *)`
does not allow `cat x && rm -rf y` — every command on the line must be
allowed, the ones after a `|` included. `--auto-approve` allows everything; do not use it in a repository you
care about.

---

## Basic Usage

### Single Message

```bash
igni -m "What does the auth middleware do?"
```

Runs one task and exits — non-zero if the turn did not happen (no model, or a
step that needed a yes nobody could give). Good for scripts and CI.

### Pipe Mode

```bash
cat error.log | igni -p -m "What caused this crash?"
```

Reads stdin, processes it with your message, writes to stdout.

### The desktop app and IDE extensions

The interactive chat: streaming replies, the agent tree, the session list,
approval dialogs and slash commands. `/help` lists the commands and
`/help shortcuts` the keys.

### Shell Mode

Type `!` or `$` to enter shell mode — the prompt changes from `>` to `$`. Commands run directly in your terminal with output shown in the conversation. The AI sees the output as context in your next message.

```
$ ls src/
  api/  models/  config.py  main.py

$ python -m pytest tests/ -q
  12 passed in 1.3s

> The tests pass. Now add a /health endpoint
  (AI sees the shell output as context)
```

- **Enter shell mode**: type `!` or `$`
- **Exit shell mode**: `Esc` or `Backspace` on empty input
- **One-off command**: `! git status` from chat mode (without entering shell mode)

### File References

Include file paths or bare filenames in your message — igni resolves them automatically:

```
> photo.jpg what's in here?
  Resolved: /Users/you/Downloads/photo.jpg

> Summarize ~/docs/report.pdf
  Resolved: /Users/you/docs/report.pdf
```

Bare filenames are searched in: project directory → `~/Downloads` → `~/Desktop` → `~/Documents` → `~`.

For vision-capable models (set `vision: true` in the model registry), images and files are attached as multimodal content. For text-only models, paths are resolved so the AI can read them via tools.

Supported formats: images (`.png`, `.jpg`, `.avif`, `.heic`, `.webp`, etc.), audio (`.mp3`, `.wav`, `.ogg`, etc.), video (`.mp4`, `.mov`, `.webm`, etc.), and documents (`.pdf`).

---

## Key Concepts

### Agents

Agents are `.md` files with YAML frontmatter. Each agent has a role, tools, and a system prompt:

```
.igni/agents/
├── architect.md        # plans + designs (always first)
├── editor.md           # creates and modifies files; owns git safety
├── explorer.md         # reads and searches the codebase
├── plan_researcher.md  # researches for a plan
├── reviewer.md         # reviews code for quality
├── simplifier.md       # post-edit code polish
├── security.md         # vulnerability analysis
├── qa.md               # test generation and review
├── debugger.md         # bug diagnosis and root cause analysis
├── docs.md             # documentation maintenance
└── visualizer.md       # diagrams
# the CodeIndex extension adds its own CodeIndex-first variants
```

Open them, read them, change them. Drop new `.md` files in to add your own agents.

### Orchestrator

You don't pick agents — the Orchestrator does. It analyzes each task and builds a purpose-fit team:

| Team Mode | When | Example |
|---|---|---|
| **Single** | One agent clearly fits | "What does this function do?" |
| **Route** | Pick one from several | "Fix the typo in README" |
| **Coordinate** | Multi-step, different skills | "Add endpoint with tests" |
| **Broadcast** | Parallel perspectives | "Review for security + performance" |
| **Tasks** | Large autonomous goals | "Migrate the test suite to pytest" |

### Tools

Agents get only the tools declared in their `.md` file. Built-in tools:

igni's main team is **shell-first**: `Bash` handles reads, search, and file listing (`cat`, `rg`, `find`, `ls`). `Read`/`Grep`/`Glob` exist in the registry but are not exposed to the main team — sub-agents can opt into them via their `tools:` allowlist. See [TOOLS.md](docs/TOOLS.md) for the full catalog and rationale.

| Tool | What It Does |
|---|---|
| `Bash` | Shell command execution — also covers read (`cat`), search (`rg`), list (`find`/`ls`) |
| `Write` | Create/overwrite files |
| `Edit` | Targeted string-replacement editing |
| `Schedule` | Schedule tasks for later or recurring execution |
| `NotebookEdit` | Read and edit Jupyter notebook cells |
| `WebSearch` | Web search |
| `WebFetch` | Fetch URL content |
| `Orchestrate` | Spawn sub-teams |
| `CodeIndex` | Semantic code search (when index is built) |

---

## Configuration

igni loads config from multiple layers (highest priority first):

1. CLI flags
2. `.igni/config.local.yaml` (project, gitignored)
3. `.igni/config.yaml` (project, committed)
4. `~/.igni/config.yaml` (user global)
5. Built-in defaults

### Minimal Config

With an igni server, `api_url` (and `portal_url` if it differs) in
`~/.igni/config.yaml` and a sign-in are all it needs — the models come from
the server.

For BYOM, the minimum is a model registry entry (see [Authenticate](#authenticate) above). You can also tune permissions:

```yaml
# .igni/config.yaml
permissions:
  file_write: ask           # ask before writing files (default)
  shell_execute: ask        # ask before running commands (default)
  web_search: allow         # search without asking (default: ask)
```

### Project Instructions

Create an `igni.md` in your project root (like `CLAUDE.md` for Claude Code):

```markdown
# Project: My App

- Python 3.12 + FastAPI backend
- React 18 frontend in client/
- Tests use pytest, run with `make test`
- Always run tests after editing code
- Never modify files in migrations/ directly
```

Agents receive these instructions as context on every request.

---

## Permission Modes

Control how much the agent can do without asking:

| Command | Behavior |
|---|---|
| `igni -m …` | Asks for file writes and shell commands — headless, that means it stops (see [A headless run cannot ask you anything](#a-headless-run-cannot-ask-you-anything)) |
| `igni --accept-edits -m …` | Auto-approves file edits, asks for shell |
| `igni --read-only -m …` | No file modifications — and no shell, so no reading the working tree either; `permissions.mode: plan` is the read-only mode that can read |
| `igni --strict -m …` | Denies the agent anything without an allow rule; asks before an organisation group hook runs |
| `igni --auto-approve -m …` | Auto-approves everything (use with caution) |

When the desktop app or an IDE extension asks for approval, you can:
- **[y] Allow once** — approve this specific call
- **[a] Always allow** — permanently allow this exact command
- **[s] Allow similar** — permanently allow the pattern (e.g., `pytest *`)
- **[n] Deny** — block this call

---

## Slash Commands

In the desktop app and IDE extensions, use `/` commands (`igni -m "/help"`
prints the full list; most also work as `igni -m "/<command>"`):

| Command | What It Does |
|---|---|
| `/help` | Show available commands |
| `/clear` | Clear conversation and reset session |
| `/config` | Show current settings |
| `/agents` | List loaded agents with their tools |
| `/sessions` | Browse and resume past sessions |
| `/rename` | Rename current session |
| `/memory` | List stored memories |
| `/plan`, `/accept`, `/bypass` | Switch permission mode |
| `/login`, `/logout`, `/whoami` | Your igni server session |
| `/commit`, `/pr`, `/explain`, `/test-plan`, `/migration` | The bundled skills |

Project knowledge (documents, decisions) is the Artifacts extension's and
CodeIndex's `/resolve-issues` is CodeIndex's — both arrive as extensions from
your igni server, not with igni.

---

## Resume Sessions

igni persists sessions to SQLite. Pick up where you left off:

```bash
igni -c -m "and now the tests"            # continue the last session
igni --session-id abc123 -m "carry on"    # continue a specific one
```

Or use `/sessions` in the desktop app to browse past sessions.

---

## Optional Features

### Documents

Project documents — runbooks, decisions, conventions the agent should not
re-derive — are the Artifacts extension's, with its own settings. It arrives
from your organisation's Artifacts service through igni server once you are
signed in and a super admin has approved that service; nothing in
`.igni/config.yaml` turns it on.

### MCP

igni is an MCP *client*: servers in `.mcp.json` (or added with `/mcp` in the
app) give the agent their tools. igni as an MCP *server* (`igni mcp serve`) is
not shipped — this section used to show its config, and the command does not
exist. For an IDE, use the VS Code or JetBrains extension.

### Web Tools

Web search and URL fetching ask before each request by default — each is a
request out of your network. To install them and let them run without asking:

```bash
pip install "ignite-ember[web]"
```

```yaml
permissions:
  web_search: allow
  web_fetch: allow
```

### Guardrails

Built-in AI safety pre-hooks:

```yaml
guardrails:
  pii_detection: true          # flag PII in prompts
  prompt_injection: true       # detect injection attempts
  moderation: true             # content moderation
```

---

## Custom Agents

Create a `.md` file in `.igni/agents/`:

```markdown
---
name: my-agent
description: Does a specific thing for my project
tools: Read, Write, Edit, Bash, Grep, Glob
tags: [backend, api]
reasoning: true
---

You are a specialist for this project.

## What You Know
- The API lives in src/api/
- Tests are in tests/
- Always run `make test` after changes
```

The Orchestrator will start including it in teams immediately.

---

## CLI Reference

All flags at a glance:

| Flag | Description |
|---|---|
| `--model <name>` | Override the default model |
| `--verbose` | Show routing decisions and reasoning |
| `--quiet` | Minimal output |
| `-m, --message <text>` | Single message mode (non-interactive) |
| `-p, --pipe` | Pipe mode: read stdin, write stdout |
| `-c, --continue` | Resume last session |
| `--session-id <id>` | Resume specific session |
| `--read-only` | No file modifications allowed |
| `--accept-edits` | Auto-approve file edits, ask for shell |
| `--auto-approve` | Auto-approve everything (use with caution) |
| `--strict` | Deny the agent anything without an explicit allow rule (reads included); run your organisation's group hooks only once you approve them (`hooks.group: ask`) |
| `--no-web` | Disable web search/fetch tools |
| `--no-color` | Disable color output |
| `--worktree` | Run in an isolated git worktree |
| `--add-dir <path>` | Include additional directory (repeatable) |
| `--trust` | Trust this folder for this run (its tools, hooks and settings load) |
| `--debug` | Enable debug logging to ~/.igni/debug.log |

---

## Tips

On startup, igni shows contextual tips based on your configuration. For example:

- *Create an `igni.md` in your project root to give agents project-specific context.*
- *Drop a `.md` file in `.igni/agents/` to create a project-specific agent — no code needed.*
- *Use `--verbose` to see which agents and team mode the Orchestrator picks.*

Tips adapt to your setup — if you haven't enabled the knowledge base or guardrails, you'll see suggestions for those.

---

## Coming from Claude Code?

Most things just work. See the [Migration Guide](docs/MIGRATION.md) for details, but the short version:

igni reads `CLAUDE.md`, `.claude/agents/*.md`, `.claude/skills/`, and `.mcp.json` out of the box — cross-tool support is on by default.

---

## Next Steps

- [Architecture](docs/ARCHITECTURE.md) — how dynamic team assembly works
- [Agents](docs/AGENTS.md) — agent format, built-in agents, creating your own
- [Configuration](docs/CONFIGURATION.md) — full settings reference
- [Tools](docs/TOOLS.md) — all available toolkits
- [Skills](docs/SKILLS.md) — reusable prompted workflows
- [Security](docs/SECURITY.md) — permissions, safety, audit logging
- [Development](docs/DEVELOPMENT.md) — contributing to igni
