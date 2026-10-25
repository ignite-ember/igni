# Tools

igni leverages Agno's toolkit system to give agents capabilities. Each agent gets only the tools it needs — no more.

## Main team toolkit (shell-first)

> ⚠️ **Read this first.** The tool catalog is intentionally narrow and
> **shell-first** — `Bash` covers read / search / list / find, and only
> `Write` / `Edit` are dedicated file-mutation toolkits. There are **no
> `Read`, `Grep`, `Glob` or `LS` toolkits at all** — not on the main team,
> not in the registry, not available to sub-agents. They were removed
> because when the model had both `Read` and `Bash(cat)` available it picked
> inconsistently between them (v0.4.0, commit `7e50705`). Naming one of them
> in an agent's `tools:` frontmatter is not a no-op: it raises
> `Unknown tool: 'Read'` when the agent is built. This is a deliberate
> divergence from Claude Code's tool catalog; see `CLAUDE_CODE_PARITY.md`
> row 22.

The actual tool list registered on the main team at session boot:

| Tool Name | Agno Toolkit | Description |
|---|---|---|
| `Bash` | `EmberShellTools` (custom) | Non-blocking shell with process management. Handles read (`cat`), search (`rg`), list (`ls`/`find`), and arbitrary commands. |
| `Write` | `FileTools()` | Create/overwrite files. Kept as a dedicated toolkit because `heredoc` rewrites are fragile. |
| `Edit` | `EmberEditTools` (custom) | Targeted string-replacement editing. Kept dedicated because `sed` is fragile. |
| `Schedule` | `ScheduleTools` (custom) | Schedule tasks for later or recurring execution. |
| `NotebookEdit` | `NotebookTools` (custom) | Read and edit Jupyter notebook cells. |
| `WebSearch` | `DuckDuckGoTools` | Web search (when `permissions.web_search != "deny"` and the `web` extra — `pip install ignite-ember[web]` — is installed). |
| `WebFetch` | `WebTools` (custom) | Fetch and extract URL content (when `permissions.web_fetch != "deny"`). |
| `Orchestrate` | `OrchestrateTools` (custom) | Spawn sub-teams from the agent pool. |

### The complete catalog — what a `tools:` list may name

This is the whole set of registry tool names. There is nothing else: a name
not on this list raises `Unknown tool` when the agent is built.

| Tool Name | Requestable by sub-agents | Requestable by `create_agent` | Notes |
|---|---|---|---|
| `Write` | yes | yes | |
| `Edit` | yes | yes | |
| `Bash` | yes | yes | `BashOutput` is an accepted alias for the same toolkit |
| `Schedule` | yes | yes | Also appended automatically to every sub-agent that has any tools |
| `NotebookEdit` | yes | yes | |
| `WebSearch` | yes | yes | Needs the `web` extra; without it the name resolves to nothing |
| `WebFetch` | yes | yes | |
| `Visualize` | yes | **no** | Used by the bundled `visualizer` sub-agent; not offered to ephemeral agents |

Two names are accepted in a `tools:` list but are not registry toolkits —
they are pseudo-tools the agent builder wires up itself, so the registry
skips them silently: `Orchestrate` and `Knowledge`. `MCP:*` entries are
likewise passed through untouched, because MCP servers connect after the
registry is built.

The two lists differ because they are validated by different code.
Sub-agent `.md` frontmatter goes through `ToolRegistry.resolve`, which
accepts every catalog name. `create_agent` and `/agents create` go through
`ToolRegistry.normalize_agno_names`, which validates against the smaller
*ephemeral-visible* set — that is where `Visualize` drops out.

### Hooks: matcher gotcha

Hook `matcher` strings are matched against the **internal tool function name**
(e.g. `run_shell_command`), not the friendly catalog name (`Bash`). A hook
with `"matcher": "Bash"` will never fire. Use the internal names:

| Friendly name | Matcher to use |
|---|---|
| `Bash` | `run_shell_command` |
| `Write` | `save_file` |
| `Edit` | `edit_file` / `edit_file_replace_all` / `create_file` |

Note `create_file` belongs to `Edit`, not `Write`. See [Hooks](HOOKS.md) for
the full matcher convention and the rest of the translation table.

---

## CodeIndex — an extension's tool

CodeIndex is an extension, not part of igni. Its extension runs the local
index — a Neo4j graph per commit, filled from the CodeIndex service — and
serves one agent-facing tool over MCP, `codeindex_cypher`, a **read-only**
Cypher query over that graph. Its manifest registers the tool with the
agent (`permissions.tools`), and while its index covers HEAD it overrides
the main prompt and some of igni's agents, edits others and plan mode, and
adds an agent of its own, `data-architect`.

Without it — or before its index covers HEAD — igni's agents search with
`Bash` (`rg` / `find` / `cat`). The tool's contract and the graph schema are
the extension's documentation (`igni__code-index`).

---

## File Operations

### Write (FileTools)

Write files on the local filesystem. `Write` is Agno's `FileTools` with
every capability except saving switched **off** at construction — there is
no read half and no list half to opt into. Reading and listing go through
`Bash` (`cat`, `ls`, `find`).

```python
from pathlib import Path

from agno.tools.file import FileTools

FileTools(
    base_dir=Path("/path/to/project"),
    enable_save_file=True,
    enable_read_file=False,
    enable_list_files=False,
    enable_search_files=False,
    enable_read_file_chunk=False,
    enable_replace_file_chunk=False,
    enable_search_content=False,
)
```

**Functions:** `save_file` — and only `save_file`.

### Edit (EmberEditTools)

Targeted string-replacement editing (inspired by Claude Code's Edit tool). Instead of rewriting entire files, it replaces specific text spans — producing minimal, reviewable diffs.

```python
from ember_code.tools.edit import EmberEditTools

# Performs old_string → new_string replacement
edit_tools = EmberEditTools(base_dir="/path/to/project")
```

**Functions:**
- `edit_file(file_path, old_string, new_string)` — replace a specific string in a file. `old_string` must appear exactly once.
- `edit_file_replace_all(file_path, old_string, new_string)` — replace all occurrences
- `create_file(file_path, content)` — create a new file

**Why custom?** Agno's built-in `FileTools.save_file` overwrites the entire file. For coding, targeted edits are safer — they produce smaller diffs, reduce merge conflicts, and are easier to review.

---

## Search

Search has no toolkit of its own. Content search, file-pattern matching and
directory listing are all `Bash`:

```
run_shell_command("rg -n 'def authenticate' src/")     # content search
run_shell_command("find src -name '*.py'")             # file patterns
run_shell_command("ls -la src/")                       # listing
```

This is the deliberate shell-first choice described at the top of this
document — the `GrepTools` / `GlobTools` toolkits this section used to
document do not exist in the codebase.

---

## Shell Execution

### Bash (EmberShellTools)

Non-blocking shell execution with background process management. Replaces Agno's built-in `ShellTools` with an implementation that handles long-running commands (servers, watchers) without blocking the agent.

```python
from ember_code.core.tools.shell import EmberShellTools

shell = EmberShellTools(base_dir="/path/to/project")
```

**Functions:**

- `run_shell_command(command, timeout?, background?, tail?)` — Run a command. `command` is a **single shell command string**. Short-lived commands block up to `timeout` seconds (default 7s). Long-running commands should use `background=True` to return immediately with a PID. If a foreground command exceeds the timeout, it is automatically backgrounded.
- `watch_process(pid, seconds?)` — Watch a background process for up to `seconds` (default 10, max 30) and return new output produced during that window. Call repeatedly to keep monitoring.
- `await_process(pid, timeout_seconds?)` — Sleep until a background process exits, then return its exit code, duration, and output tail. Use it when the next step depends on the result (a build, a test run, a migration); use `watch_process` to look in on something that is meant to keep running.
- `read_process_output(pid, tail?)` — Read the last `tail` lines of output from a background process.
- `stop_process(pid)` — Kill a background process and its children.
- `list_processes()` — List all running background processes with PID, command, and elapsed time.

**One string, not an argv list.** The parameter is `command: str`, and the
string is handed to `/bin/sh -c`. That is the whole point: the shell is what
gives the agent pipes (`|`), redirection (`>`, `2>&1`), chaining (`&&`,
`||`, `;`), `$VAR` expansion, `$(...)` substitution, globs, env-var prefixes
(`PATH=X cmd`), and builtins like `cd` and `export`. An argv list has no
shell to interpret any of that, so the tool's own docstring labels the list
form **Bad** and the string form **Good**:

```
run_shell_command("ls -la | wc -l")               # Good
run_shell_command("cd portal && npm run build")   # Good
run_shell_command(["ls", "-la"])                  # Bad — no shell, no pipes
```

A list that slips through is joined back into a string with spaces, which
silently mangles any argument containing a space — another reason to pass
the string yourself.

**Background processes:**

Servers and long-running commands should always use `background=True`:

```
run_shell_command("python -m uvicorn main:app", background=True)
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

# backend="auto" is forced by the registry — the "duckduckgo" backend
# raises "No results found." on every query in ddgs 9.x.
web_search = DuckDuckGoTools(backend="auto")
```

**Functions:** `web_search(query)`, `search_news(query)`

These are the names Agno registers, and therefore the names to use in a
hook `matcher` and in a `requires_confirmation_tools` list. The toolkit has
never registered `duckduckgo_search` / `duckduckgo_news`; a confirmation
gate written against those names matched nothing and web search ran with no
approval prompt.

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

There is no `Python` toolkit. Python runs through `Bash` like anything else:

```
run_shell_command("python -m pytest tests/ -q")
run_shell_command("python -c 'import sys; print(sys.version)'")
```

This keeps one execution surface under one set of blocked-command and
confirmation rules, instead of a second interpreter that bypasses them.

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

### Project documents

igni keeps no knowledge store of its own. Project documents — versioned,
with lineage, comments and their page — are the Artifacts extension's
(`igni__artifacts`): its archivist agent reads and writes them, and the
extension supplies the prompt text that teaches the main agent when to
delegate to it (`PROJECT_DOCUMENTS_*` anchors, shown only while it is
installed and READY).

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

`LoopTools` is wired onto the main agent directly, not through the tool
registry — `Loop` is not a name a `tools:` list can request.

**Agent functions:**

- `loop_start(prompt, max_iterations=30)` — arm the loop. The first iteration fires automatically as the next agent turn. `max_iterations` is a safety cap; hard ceiling 200.
- `loop_stop()` — clear the pending prompt. The current turn finishes normally; no further iterations fire. Idempotent — safe to call when no loop is active.
- `loop_status()` — report whether a loop is active and how many iterations remain. Use when the user asks *"are we still looping?"*.
- `loop_resume()` — resume a loop whose state was restored from disk on startup but has not fired an iteration yet ("pick up where we left off").
- `loop_set_total(total)` — announce the real iteration count once known, so the panel renders `N / total`. Informational; it does not change the cap.

A sibling `LoopProgressTool` is wired alongside it, giving the agent a
per-loop scratchpad: `loop_progress_get`, `loop_progress_set`,
`loop_progress_list`, `loop_progress_delete`, `loop_progress_clear`.

**How a loop ends:**

| Trigger | Behavior |
|---|---|
| `max_iterations` hit | Loop stops; final turn completes normally. |
| `/loop stop` (user) | Pending prompt cleared at the next hook check. |
| `loop_stop()` (agent) | Same effect from inside an iteration. |
| Any non-`/loop` user input | Treated as an interrupt — the user took control back. |

Loops never run in the background. Each iteration is a real conversation turn, so iteration outputs stream into the same session and the agent sees the cumulative history. If the user wants persistent, durable recurring execution that survives the session closing, `/schedule` is the right primitive instead.

**Not to be confused with the in-turn `loop` skill.** Earlier versions of igni shipped a `loop` skill (`bundled_skills/loop`) for the *different* pattern of "apply this task to a list of items within a single turn." That skill was removed in `0885ec0` because its name collided with `/loop` and the patterns are unrelated. The slash command and toolkit described above are the only loop primitive now.

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
- `create_agent(name, description, system_prompt, tools?)` — create an ephemeral agent with a custom system prompt (only available when `orchestration.generate_ephemeral` is enabled). `tools` is a comma-separated string and defaults to `"Write,Edit,Bash"`.

`create_agent` validates `tools` against the *ephemeral-visible* set and
refuses the whole call on any unknown name. That set is exactly:

```
Bash, BashOutput, Edit, NotebookEdit, Schedule, WebFetch, WebSearch, Write
```

`Visualize` is a real registry tool but is **not** on this
list — a sub-agent `.md` may name it, `create_agent` may not. Reading and
searching are not on it either, because they are not tools: they go through
`Bash` (`cat`, `rg`, `find`).

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

Place custom tools in `~/.igni/tools/` or `.igni/tools/` for project-level tools. They're automatically discovered and available to agents.

---

## Plugins — Claude-Code-compatible bundles

A **plugin** is a directory that bundles skills, agents, hooks, MCP servers, and custom tools into a single distributable unit. Plugins built for Claude Code work in igni unchanged.

```text
my-plugin/
├── .claude-plugin/plugin.json    # required manifest
├── skills/<name>/SKILL.md        # → SkillPool, namespaced <plugin>:<name>
├── agents/<name>.md              # → AgentPool, namespaced <plugin>:<name>
├── hooks/hooks.json              # → HookLoader, prepended (project hooks run last)
├── .mcp.json                     # → MCPConfigLoader, servers prefixed <plugin>:<server>
└── tools/<name>.py               # → CustomToolkit, named custom_<plugin>_<name>
```

Discovery roots: `~/.claude/plugins/`, `~/.igni/plugins/`, `<project>/.claude/plugins/`, `<project>/.igni/plugins/`. Install via `/plugin install <git-url>` or `/plugin install @<marketplace>/<plugin>`. The `/plugins` slash command opens the Textual panel for browsing, toggling, updating, and installing from registered marketplaces.

See [Plugins](PLUGINS.md) for the full guide.

---

## Tool Access by Built-in Agent

Each built-in agent's tools are declared in the `tools:` frontmatter of its
`.md` file. These are the shipped values, verbatim:

| Agent | Declared `tools:` |
|---|---|
| `explorer` | `WebFetch, WebSearch, Bash` |
| `architect` | `WebSearch, Bash` |
| `plan_researcher` | `Bash, WebFetch, WebSearch` |
| `editor` | `Write, Edit, Bash` |
| `docs` | `Write, Edit, Bash` |
| `qa` | `Write, Edit, Bash` |
| `simplifier` | `Edit, Bash` |
| `debugger` | `Edit, Bash` |
| `reviewer` | `WebFetch, WebSearch, Bash` |
| `security` | `WebSearch, Bash` |
| `visualizer` | `Visualize` |

Every one of them reaches files through `Bash` — none names a read, grep,
glob or list toolkit, because none exists. `visualizer` is the only one
holding `Visualize`. (CodeIndex's `data-architect` reaches its tool through
the extension's MCP server — `mcp_servers: [ext:igni.codeindex]`.)

An extension may override one of these agents, or edit it section by section
(`contributes.prompt`): CodeIndex does both, with versions that use its
index, while its index covers HEAD.

Two toolkits are appended at build time and so appear on no agent's list:
`Schedule` (added to every sub-agent that has any tools at all) and the
knowledge toolkit (added whenever a knowledge manager is configured).

Since agents are `.md` files, you can change any agent's tools by editing its definition or overriding it in `.igni/agents/`. A name outside the catalog above is not silently dropped — it raises `Unknown tool` when the agent is built.
