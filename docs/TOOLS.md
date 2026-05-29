# Tools

Ember Code leverages Agno's toolkit system to give agents capabilities. Each agent gets only the tools it needs — no more.

## Core Toolkits

Ember Code uses the **same tool names as Claude Code**. Each name maps to an Agno toolkit under the hood:

| Tool Name | Agno Toolkit | Description |
|---|---|---|
| `Read` | `FileTools(read_only=True)` | Read file contents |
| `Write` | `FileTools()` | Create/overwrite files |
| `Edit` | `EmberEditTools` (custom) | Targeted string-replacement editing |
| `Bash` | `EmberShellTools` (custom) | Non-blocking shell with process management |
| `Grep` | `GrepTools` (custom) | Regex content search (ripgrep) |
| `Glob` | `GlobTools` (custom) | File pattern matching |
| `LS` | `EmberShellTools` (custom) | List directory contents |
| `WebSearch` | `DuckDuckGoTools` or `TavilyTools` | Web search |
| `WebFetch` | `WebTools` (custom) | Fetch and extract URL content |
| `Orchestrate` | `OrchestrateTools` (custom) | Spawn sub-teams from agent pool |
| `Schedule` | `ScheduleTools` (custom) | Schedule tasks for later or recurring execution |
| `Loop` | `LoopTools` (custom) | Drive the in-session `/loop` primitive (re-fire a prompt across turns) |
| `CodeIndex` | `CodeIndexTools` (custom) | Semantic search over pre-processed code intelligence |
| `NotebookEdit` | `NotebookTools` (custom) | Read and edit Jupyter notebook cells |
| `Python` | `PythonTools` | Execute Python code |

---

## CodeIndex — Semantic Code Intelligence

CodeIndex is the most important tool in Ember Code's arsenal. While other tools operate on raw text (grep for patterns, read for contents), CodeIndex provides **pre-processed, semantic understanding** of the entire codebase.

See [CodeIndex](CODEINDEX.md) for the full documentation on categories, hierarchical summaries, indexing pipeline, and self-hosting.

### What It Does

CodeIndex pre-analyzes every entity in the codebase (files, classes, functions, modules, packages) and generates rich summaries across **six categories**: Code, Security, Testability, Architecture, Performance, and Maintainability.

Summaries are built **bottom-up** (function → class → file → module → project), giving agents multi-resolution understanding.

### Functions

- `codeindex_search(query, item_type?, name?, file_extension?, tags?, limit?)` — semantic search across the indexed codebase with filters
- `codeindex_similar(item_id, item_type?, limit?)` — find semantically similar items to a given item
- `codeindex_item(item_id)` — get full details for a specific indexed item (content, summary, chunks, references)
- `codeindex_references(item_id)` — get incoming/outgoing reference graph for an item
- `codeindex_tree(parent_id?, item_type?, name?, query?, limit?)` — browse the indexed folder/file hierarchy
- `codeindex_tags(commit_id?)` — get all available tags (domain, concern, system, quality) for filtering

### Why This Matters

Traditional code search is syntactic — `grep "authenticate"` finds the string, not the concept. CodeIndex is semantic:

| Query | Grep | CodeIndex |
|---|---|---|
| "how does auth work?" | Finds files with "auth" in the name/content | Returns the auth module summary with flow description, security analysis, and references to all related files |
| "what's vulnerable?" | Can't answer this | Returns security-category summaries flagging issues across the codebase |
| "where should I add rate limiting?" | Can't answer this | Returns architecture summaries of request-handling modules with dependency analysis |
| "what needs more tests?" | Can't answer this | Returns testability-category summaries ranking under-tested areas |

### Configuration

CodeIndex works out of the box with zero configuration. Per-project customization (categories, indexing options, ignore patterns) is planned for a future release.

### Fallback: Local Mode

If CodeIndex cloud is unavailable, Ember Code falls back to local tools (Grep, Glob, Read). The experience degrades gracefully — agents still work, just without semantic understanding.

---

## File Operations

### Read / Write (FileTools)

Read and write files on the local filesystem.

```python
from agno.tools.file import FileTools

# Read-only mode for Explorer/Reviewer
FileTools(read_only=True, base_dir="/path/to/project")

# Full access for Editor
FileTools(base_dir="/path/to/project")
```

**Functions:** `read_file`, `write_file`, `list_files`

### Edit (EmberEditTools)

Targeted string-replacement editing (inspired by Claude Code's Edit tool). Instead of rewriting entire files, it replaces specific text spans — producing minimal, reviewable diffs.

```python
from ember_code.tools.edit import EmberEditTools

# Performs old_string → new_string replacement
edit_tools = EmberEditTools(base_dir="/path/to/project")
```

**Functions:**
- `edit_file(path, old_string, new_string)` — replace a specific string in a file
- `edit_file_replace_all(path, old_string, new_string)` — replace all occurrences
- `create_file(path, content)` — create a new file (fails if exists)

**Why custom?** Agno's built-in `FileTools.write_file` overwrites the entire file. For coding, targeted edits are safer — they produce smaller diffs, reduce merge conflicts, and are easier to review.

---

## Search

### Grep (GrepTools)

Content search using ripgrep (`rg`), providing fast regex search across the codebase.

```python
from ember_code.tools.search import GrepTools

grep_tools = GrepTools(base_dir="/path/to/project")
```

**Functions:**
- `grep(pattern, path?, glob?, file_type?, context_lines?, max_results?)` — search file contents with regex
- `grep_files(pattern, path?, glob?)` — return only matching file paths
- `grep_count(pattern, path?)` — return match counts per file

### Glob (GlobTools)

File pattern matching for finding files by name/path.

```python
from ember_code.tools.search import GlobTools

glob_tools = GlobTools(base_dir="/path/to/project")
```

**Functions:**
- `glob_files(pattern, path?, max_results?)` — find files matching a glob pattern (e.g., `**/*.py`)

---

## Shell Execution

### Bash (EmberShellTools)

Non-blocking shell execution with background process management. Replaces Agno's built-in `ShellTools` with an implementation that handles long-running commands (servers, watchers) without blocking the agent.

```python
from ember_code.core.tools.shell import EmberShellTools

shell = EmberShellTools(base_dir="/path/to/project")
```

**Functions:**

- `run_shell_command(args, timeout?, background?, tail?)` — Run a command. Short-lived commands block up to `timeout` seconds (default 7s). Long-running commands should use `background=True` to return immediately with a PID. If a foreground command exceeds the timeout, it is automatically backgrounded.
- `watch_process(pid, seconds?)` — Watch a background process for up to `seconds` (default 10, max 30) and return new output produced during that window. Call repeatedly to keep monitoring.
- `read_process_output(pid, tail?)` — Read the last `tail` lines of output from a background process.
- `stop_process(pid)` — Kill a background process and its children.
- `list_processes()` — List all running background processes with PID, command, and elapsed time.

**Background processes:**

Servers and long-running commands should always use `background=True`:

```
run_shell_command(["python", "-m", "uvicorn", "main:app"], background=True)
```

The tool waits 3 seconds after starting to capture initial output (e.g. "Serving on port 8000" or crash errors), then returns the PID. Use `watch_process(pid)` to monitor and `stop_process(pid)` to stop.

**Cancellation:** When the user presses Escape, any active foreground process is killed immediately. Background processes survive cancellation — they're only cleaned up on session exit or explicit `stop_process`.

**Safety:** Commands are validated against blocked patterns and confirmation requirements. See [Configuration](CONFIGURATION.md) for details.

---

## Web Access

### WebSearch (DuckDuckGoTools)

Web search without API keys.

```python
from agno.tools.duckduckgo import DuckDuckGoTools

web_search = DuckDuckGoTools()
```

**Functions:** `duckduckgo_search(query)`, `duckduckgo_news(query)`

### WebFetch (WebTools)

Fetch and extract content from URLs.

```python
from ember_code.tools.web import WebTools

web_tools = WebTools()
```

**Functions:**
- `fetch_url(url)` — fetch URL content, extract text
- `fetch_json(url)` — fetch and parse JSON

---

## Python Execution

### Python (PythonTools)

Execute Python code in a sandboxed environment.

```python
from agno.tools.python import PythonTools

python_tools = PythonTools(
    base_dir="/path/to/project",
    pip_install=True,  # allow installing packages
)
```

**Functions:** `run_python_code(code)`, `pip_install(package)`, `read_file(path)`, `list_files(path)`

---

## Notebook Editing

### NotebookEdit (NotebookTools)

Read and edit individual cells in Jupyter notebooks (`.ipynb`). Operates on the notebook's JSON structure directly -- no `nbformat` dependency required. Preserves all metadata, outputs, and formatting.

```python
from ember_code.core.tools.notebook import NotebookTools

notebook_tools = NotebookTools(base_dir="/path/to/project")
```

**Functions:**
- `notebook_read(file_path)` -- read a notebook and return a summary of all cells (index, type, line count, preview)
- `notebook_read_cell(file_path, cell_index)` -- read a specific cell's full source and outputs
- `notebook_edit_cell(file_path, cell_index, new_source)` -- replace a cell's source content (clears outputs for code cells)
- `notebook_add_cell(file_path, cell_index, cell_type, source)` -- insert a new cell at the given index (`-1` to append). `cell_type` is one of "code", "markdown", "raw".
- `notebook_remove_cell(file_path, cell_index)` -- remove a cell by index

---

## Git & GitHub

Git operations are handled via `EmberShellTools` with git/gh commands. The Git Agent wraps these with safety checks:

- **Pre-push confirmation** — always asks before pushing
- **Force-push protection** — warns and requires explicit confirmation
- **Destructive operation guards** — `reset --hard`, `clean -f`, `branch -D` require approval

---

## Knowledge Base

### Knowledge (KnowledgeManager)

Built-in vector knowledge base powered by ChromaDB and the Ember embeddings API. Unlike CodeIndex (which provides pre-processed semantic code intelligence), the Knowledge system is a general-purpose document store that users can populate with any content.

```yaml
knowledge:
  enabled: true
  collection_name: "my_project"
  embedder: "ember"            # uses Ember server's /v1/embeddings (384-dim)
```

**Slash commands:**
- `/knowledge` — show knowledge base status (document count, collection info)
- `/knowledge add <url|path|text>` — add content to the knowledge base
- `/knowledge search <query>` — search the knowledge base

**How it works:**
1. `EmberEmbedder` calls the Ember server's `/v1/embeddings` endpoint (proxying to text2vec-transformers, 384 dimensions)
2. Documents are chunked and stored in ChromaDB with vector embeddings
3. Agents can search the knowledge base automatically during execution via Agno's `Knowledge` integration

**Data models (Pydantic):** `KnowledgeAddResult`, `KnowledgeSearchResponse`, `KnowledgeFilter`, `KnowledgeStatus`

**Requires:** `pip install ember-code[knowledge]` (installs `chromadb`)

---

## Task Scheduling

### Schedule (ScheduleTools)

Enables agents to schedule tasks for later or recurring execution. All agents with tools automatically get scheduling capabilities.

**Functions:**
- `schedule_task(description, when)` — schedule a task for later execution
- `list_scheduled_tasks(include_done?)` — check scheduled tasks and their status
- `cancel_scheduled_task(task_id)` — cancel a pending or recurring task

**Time formats:**
- One-shot: `"in 30 minutes"`, `"at 5pm"`, `"tomorrow"`, `"2026-12-25 14:00"`
- Recurring: `"daily"`, `"daily at 9am"`, `"hourly"`, `"every 2 hours"`, `"weekly"`

**Configuration:**
```yaml
scheduler:
  poll_interval: 30       # seconds between checking for due tasks
  task_timeout: 300       # max seconds per task (5 min default)
  max_concurrent: 1       # bounded concurrency (sequential by default)
```

Scheduled tasks run in the background via `SchedulerRunner` with bounded concurrency (`asyncio.Semaphore`) and per-task timeout (`asyncio.wait_for`). Toast notifications appear in the TUI when tasks complete or fail.

---

## In-Session Loops

### Loop (LoopTools)

The in-session loop primitive: the same prompt re-fires as the next user turn over and over until a cap is reached, the user types non-`/loop` input (treated as an interrupt), or the loop is explicitly stopped. Useful when the user describes work that repeats across turns — *"keep fixing failures until the suite passes"*, *"go through these one at a time"*, *"do X for each of A, B, C"* — without the user having to re-paste the prompt each time.

The same state (`session.pending_loop_prompt`) is reachable from two surfaces:

- **User-facing slash command** — `/loop <prompt>`, `/loop <N> <prompt>` (explicit cap), `/loop stop`, `/loop` (status). See [DEVELOPMENT.md](DEVELOPMENT.md) for the slash-command listing.
- **Agent-facing toolkit** — when the user describes the loop in plain language (*"keep going for each item"*), the agent calls `loop_start()` to arm the same state.

**Agent functions:**

- `loop_start(prompt, max_iterations=30)` — arm the loop. The first iteration fires automatically as the next agent turn. `max_iterations` is a safety cap; hard ceiling 200.
- `loop_stop()` — clear the pending prompt. The current turn finishes normally; no further iterations fire. Idempotent — safe to call when no loop is active.
- `loop_status()` — report whether a loop is active and how many iterations remain. Use when the user asks *"are we still looping?"*.

**How a loop ends:**

| Trigger | Behavior |
|---|---|
| `max_iterations` hit | Loop stops; final turn completes normally. |
| `/loop stop` (user) | Pending prompt cleared at the next hook check. |
| `loop_stop()` (agent) | Same effect from inside an iteration. |
| Any non-`/loop` user input | Treated as an interrupt — the user took control back. |

Loops never run in the background. Each iteration is a real conversation turn, so iteration outputs stream into the same session and the agent sees the cumulative history. If the user wants persistent, durable recurring execution that survives the session closing, `/schedule` is the right primitive instead.

**Not to be confused with the in-turn `loop` skill.** Earlier versions of Ember Code shipped a `loop` skill (`bundled_skills/loop`) for the *different* pattern of "apply this task to a list of items within a single turn." That skill was removed in `0885ec0` because its name collided with `/loop` and the patterns are unrelated. The slash command and toolkit described above are the only loop primitive now.

---

## Orchestration

### Orchestrate (OrchestrateTools)

Enables agents to spawn sub-teams at runtime. Any agent with `can_orchestrate: true` (the default) gets access to this tool.

```python
from ember_code.tools.orchestrate import OrchestrateTools

orchestrate = OrchestrateTools(pool=agent_pool, config=settings)
```

**Functions:**
- `spawn_team(task, agent_names, mode?)` — spawn a sub-team to handle a task. `agent_names` is a required comma-separated string of agent names. Mode defaults to "coordinate".
- `spawn_agent(task, agent_name)` — spawn a single agent for a focused sub-task.
- `create_agent(name, description, system_prompt, tools?)` — create an ephemeral agent with a custom system prompt (only available when `orchestration.generate_ephemeral` is enabled). `tools` defaults to "Read,Write,Edit,Bash,Grep,Glob".

**Depth limits:** Configurable via `orchestration.max_nesting_depth` (default: 5) and `orchestration.max_total_agents` (default: 20). See [Security](SECURITY.md) for details.

---

## Custom Tools

You can add custom tools using Agno's `@tool` decorator:

```python
from agno.tools import tool

@tool(description="Run the project's test suite")
def run_tests(test_path: str = "") -> str:
    """Run tests, optionally filtering by path."""
    import subprocess
    cmd = ["pytest", "-v"]
    if test_path:
        cmd.append(test_path)
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=project_dir)
    return result.stdout + result.stderr
```

Place custom tools in `~/.ember/tools/` or `.ember/tools/` for project-level tools. They're automatically discovered and available to agents.

---

## Plugins — Claude-Code-compatible bundles

A **plugin** is a directory that bundles skills, agents, hooks, MCP servers, and custom tools into a single distributable unit. Plugins built for Claude Code work in Ember Code unchanged.

```text
my-plugin/
├── .claude-plugin/plugin.json    # required manifest
├── skills/<name>/SKILL.md        # → SkillPool, namespaced <plugin>:<name>
├── agents/<name>.md              # → AgentPool, namespaced <plugin>:<name>
├── hooks/hooks.json              # → HookLoader, prepended (project hooks run last)
├── .mcp.json                     # → MCPConfigLoader, servers prefixed <plugin>:<server>
└── tools/<name>.py               # → CustomToolkit, named custom_<plugin>_<name>
```

Discovery roots: `~/.claude/plugins/`, `~/.ember/plugins/`, `<project>/.claude/plugins/`, `<project>/.ember/plugins/`. Install via `/plugin install <git-url>` or `/plugin install @<marketplace>/<plugin>`. The `/plugins` slash command opens the Textual panel for browsing, toggling, updating, and installing from registered marketplaces.

See [Plugins](PLUGINS.md) for the full guide.

---

## Tool Access by Built-in Agent

Each built-in agent's tools are declared in its `.md` file. This table shows the defaults:

| Tool | Explorer | Architect | Planner | Editor | Simplifier | Reviewer | Security | QA | Debugger | Git | Conversational |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `Read` | yes | yes | yes | yes | yes | yes | yes | yes | yes | yes | - |
| `Write` | - | - | - | yes | - | - | - | yes | - | - | - |
| `Edit` | - | - | - | yes | yes | - | - | yes | yes | - | - |
| `Grep` | yes | yes | yes | yes | yes | yes | yes | yes | yes | yes | - |
| `Glob` | yes | yes | yes | yes | yes | yes | yes | yes | yes | yes | - |
| `Bash` | - | - | - | yes | yes | - | - | yes | yes | yes | - |
| `LS` | yes | yes | yes | - | - | yes | yes | - | - | - | - |
| `WebSearch` | yes | yes | yes | - | - | yes | yes | - | - | - | - |
| `WebFetch` | yes | - | - | - | - | yes | - | - | - | - | - |
| `Orchestrate` | yes | yes | yes | yes | yes | yes | yes | yes | yes | yes | - |

Since agents are `.md` files, you can change any agent's tools by editing its definition or overriding it in `.ember/agents/`.
