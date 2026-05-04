# Ember Code

**One spark ignites a team.** An AI coding assistant built with [Agno](https://github.com/agno-agi/agno) orchestration.

[ignite-ember.sh](https://ignite-ember.sh)

Inspired by [Claude Code](https://github.com/anthropics/claude-code), Ember Code is a terminal-based coding agent that assembles specialized AI teams on the fly. Describe your task — the Orchestrator picks the right agents, the right team mode, and runs them.

## Why Ember Code?

Claude Code uses a single agent loop — powerful but monolithic. Ember Code takes a different approach: **dynamic multi-agent orchestration**. Instead of one agent doing everything, Agno's team system decomposes tasks, routes them to specialized agents, and synthesizes results — all automatically.

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
- [Skills](docs/SKILLS.md) — Reusable prompted workflows (`/deploy`, `/review-pr`, etc.)
- [Onboarding](docs/ONBOARDING.md) — First-run setup, CodeIndex, and agent proposals
- [Tools](docs/TOOLS.md) — Available toolkits and capabilities
- [MCP](docs/MCP.md) — IDE integration via Model Context Protocol
- [Configuration](docs/CONFIGURATION.md) — Settings, permissions, and customization
- [CodeIndex](docs/CODEINDEX.md) — Semantic code intelligence engine
- [Evals](docs/EVALS.md) — Agent evaluation framework and regression testing
- [Hooks](docs/HOOKS.md) — Pre/post tool execution hooks
- [Migration](docs/MIGRATION.md) — Coming from Claude Code or Codex
- [Security](docs/SECURITY.md) — Threat model, permissions, and enterprise hardening
- [Development](docs/DEVELOPMENT.md) — Contributing and extending Ember Code

## License

MIT
