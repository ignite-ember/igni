# Agents

igni takes a **definition-driven, dynamic** approach to agents. There are no hardcoded agent hierarchies or fixed teams. Instead:

1. Agents are **defined as `.md` files** with YAML frontmatter
2. All agent definitions form an **agent pool**
3. An **Orchestrator** analyzes each task and **dynamically assembles a team** from the pool
4. The Orchestrator picks the **team mode** (Route, Coordinate, Broadcast, Tasks) that best fits the task
5. Users can **drop new `.md` files** into the `agents/` folder — they're automatically included in the pool

```
User Message
    │
    ▼
┌──────────────┐       ┌───────────────────────────────┐
│ Orchestrator │──────▶│        Agent Pool             │
│              │       │                               │
│ • Analyzes   │       │  explorer.md    architect.md  │
│   the task   │       │  planner.md     editor.md     │
│ • Selects    │       │  simplifier.md  reviewer.md   │
│   agents     │       │  security.md    qa.md         │
│ • Picks team │       │  debugger.md    git.md        │
│   mode       │       │  conversational.md            │
│              │       │  your-custom-agent.md         │
│              │       └───────────────────────────────┘
│              │
│              │──────▶ Assembles Team dynamically
│              │        (mode: coordinate | route | tasks | broadcast)
└──────────────┘
         │
         ▼
    Team executes task
         │
         ▼
    Response to user
```

---

## Agent Definition Format

Agent `.md` files use **the same format as Claude Code** — YAML frontmatter with `name`, `description`, `tools`, `model`, plus the markdown body as the system prompt. Claude Code agent files work in igni out of the box. igni extends the format with optional fields for orchestration.

### Claude Code Compatible (works in both)

```markdown
---
name: code-explorer
description: Deeply analyzes existing codebase features by tracing execution paths, mapping architecture layers, and documenting dependencies
tools: Glob, Grep, LS, Read, NotebookRead, WebFetch, WebSearch
color: yellow
---

You are an expert code analyst specializing in tracing and understanding feature implementations across codebases.

## Core Mission
Provide a complete understanding of how a specific feature works by tracing its implementation from entry points to data storage.
```

### With Ember Extensions

```markdown
---
name: code-explorer
description: Deeply analyzes existing codebase features by tracing execution paths, mapping architecture layers, and documenting dependencies
tools: Glob, Grep, LS, Read, NotebookRead, WebFetch, WebSearch
color: yellow

# Ember extensions (ignored by Claude Code, used by igni)
reasoning: false
tags:
  - search
  - read-only
  - exploration
can_orchestrate: false   # opt out of orchestration for this agent
---

You are an expert code analyst...
```

### Frontmatter Fields

**Claude Code compatible fields:**

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Unique identifier for this agent |
| `description` | string | yes | What this agent does — the Orchestrator reads this to decide when to use it |
| `tools` | string/list | no | Comma-separated string or YAML list. Uses Claude Code tool names (`Read`, `Write`, `Edit`, `Bash`, etc.) |
| `model` | string | no | Model ID (`MiniMax-M2.7`, `MiniMax-M2.7-highspeed`) or any Agno-supported model. Defaults to config default |
| `color` | string | no | UI color for this agent (`yellow`, `red`, `green`, `blue`, etc.) |

**igni extensions** (ignored by Claude Code if the file is used there):

| Field | Type | Required | Description |
|---|---|---|---|
| `reasoning` | bool | no | Enable Agno's chain-of-thought reasoning. Default: `false` |
| `reasoning_min_steps` | int | no | Min reasoning steps (when reasoning=true). Default: `1` |
| `reasoning_max_steps` | int | no | Max reasoning steps (when reasoning=true). Default: `10` |
| `tags` | list | no | Semantic tags that help the Orchestrator understand capabilities |
| `mcp_servers` | list | no | MCP servers this agent can access (by name from `.mcp.json`) |
| `max_turns` | int | no | Maximum conversation turns. Default: unlimited |
| `temperature` | float | no | Model temperature. Default: model default |
| `can_orchestrate` | bool | no | If `true`, this agent can access the agent pool and spawn sub-teams. Default: `true` |

### Tool Names

igni uses the **same tool names as Claude Code**. Agent files are fully cross-compatible.

| Tool Name | Agno Toolkit | Description |
|---|---|---|
| `Read` | `FileTools(read_only=True)` | Read file contents |
| `Write` | `FileTools()` | Create/overwrite files |
| `Edit` | `EmberEditTools()` | Targeted string-replacement editing |
| `Grep` | `GrepTools()` | Regex content search |
| `Glob` | `GlobTools()` | File pattern matching |
| `Bash` | `EmberShellTools` | Non-blocking shell with process management |
| `BashOutput` | `EmberShellTools` | Shell execution (alias) |
| `LS` | `EmberShellTools` | List directory contents |
| `WebSearch` | `DuckDuckGoTools()` | Web search |
| `WebFetch` | `WebTools()` | Fetch URL content |
| `NotebookRead` | `NotebookTools(read_only=True)` | Read Jupyter notebooks |
| `TodoWrite` | `TodoTools()` | Task/todo management |
| `KillShell` | `ProcessTools()` | Kill running shell processes |
| `Python` | `PythonTools()` | Execute Python code (igni addition) |
| `Orchestrate` | `OrchestrateTools()` | Spawn sub-teams (included by default; set `can_orchestrate: false` to disable) |
| `MCP:<server>` | `MCPTools(...)` | Tools from a named MCP server |

Drop a Claude Code agent file into `.ember/agents/` — it works immediately. All agents can orchestrate (spawn sub-teams) by default. Optionally add Ember extensions like `tags`, `reasoning`, or `can_orchestrate: false` to restrict an agent.

---

## Agent Pool

By default, the agent pool loads from **igni directories only**:

| Scope | Location | Format |
|---|---|---|
| Project | `.ember/agents/` (committed) | `.md` with YAML frontmatter |
| Project | `.ember/agents.local/` (gitignored) | `.md` with YAML frontmatter |
| Global | `~/.ember/agents/` | `.md` with YAML frontmatter |
| Ephemeral | `.ember/agents.tmp/` (session-scoped) | `.md` with YAML frontmatter |

> **Built-in agents** are copied from the package into `.ember/agents/` on first project init (checksum-merged on subsequent runs). They appear as project-level agents.

**Merging rule:** All agents are combined into one pool. The only conflict is when two agents share the same `name` — in that case, **project-level always wins over global**. Agents with different names never conflict — they all coexist in the pool.

### Cross-Tool Support (on by default)

Cross-tool support is enabled by default. igni scans Claude Code and Codex directories automatically. To disable, set `agents.cross_tool_support: false`.

Additional directories scanned:

| Scope | Location | Source | Format |
|---|---|---|---|
| Project | `.claude/agents/` | Claude Code | `.md` with YAML frontmatter (native) |
| Project | `AGENTS.md` / `.codex/` | Codex | Markdown + TOML (parsed) |
| Global | `~/.claude/agents/` | Claude Code | `.md` with YAML frontmatter (native) |
| Global | `~/.codex/` | Codex | Markdown + TOML (parsed) |

Within the same scope, igni directories take precedence over Claude Code, which takes precedence over Codex.

**Cross-tool compatibility:**

- **Claude Code agents** — loaded natively. Same `.md` + YAML frontmatter format. No conversion needed.
- **Codex agents** — Codex defines agent roles in `config.toml` and instructions in `AGENTS.md`. The loader parses these into the same internal representation.
- **igni agents** — native format. Claude Code compatible fields + Ember extensions (tags, reasoning, can_orchestrate).

If you're coming from Claude Code or Codex, your existing agents are picked up automatically — zero migration. See [Migration](MIGRATION.md) for details.

### Built-in Agents

igni ships with foundational agents in Claude Code compatible format plus Ember extensions. Override or extend them freely.

**explorer.md** — Read-only codebase search and analysis.
```yaml
tools: Glob, Grep, LS, Read, WebFetch, WebSearch
tags: [search, read-only, exploration]
```

**architect.md** — Designs component architecture, data flows, and interfaces.
```yaml
tools: Glob, Grep, LS, Read, WebSearch
reasoning: true
tags: [architecture, design, read-only]
```

**planner.md** — Analyzes tasks, produces structured implementation plans.
```yaml
tools: Glob, Grep, LS, Read, WebSearch
reasoning: true
tags: [planning, reasoning, read-only]
```

**editor.md** — Creates and modifies files. Can spawn sub-teams for exploration or review.
```yaml
tools: Read, Write, Edit, Bash, Glob, Grep
tags: [coding, editing, file-write]
```

**simplifier.md** — Post-edit code polish, dead code removal, complexity reduction.
```yaml
tools: Read, Edit, Glob, Grep, Bash
tags: [quality, refactoring, simplification]
```

**reviewer.md** — Reviews code for bugs, security issues, and style compliance.
```yaml
tools: Glob, Grep, LS, Read, WebFetch, WebSearch
color: red
reasoning: true
tags: [review, quality, read-only]
```

**security.md** — Vulnerability analysis, OWASP Top 10, auth and input validation review.
```yaml
tools: Glob, Grep, LS, Read, WebSearch
reasoning: true
tags: [security, audit, vulnerabilities, read-only]
```

**qa.md** — Test generation, test quality review, and coverage gap analysis.
```yaml
tools: Read, Write, Edit, Bash, Glob, Grep
tags: [testing, qa, coverage]
```

**debugger.md** — Bug diagnosis, stack trace analysis, root cause finding.
```yaml
tools: Read, Edit, Bash, Glob, Grep
reasoning: true
tags: [debugging, diagnostics, bug-fix]
```

**git.md** — Version control: commits, branches, PRs, diffs.
```yaml
tools: Bash, Read, Glob, Grep
tags: [git, github, version-control]
```

**conversational.md** — General Q&A and explanations, no tools.
```yaml
tags: [chat, explain, no-tools]
```

**diagnostician.md** — System diagnostics and issue diagnosis.
```yaml
tools: Read, Bash, Glob, Grep
reasoning: true
tags: [diagnostics, system, troubleshooting]
```

**docs.md** — Documentation writing and updates.
```yaml
tools: Read, Write, Edit, Glob, Grep
tags: [documentation, writing, docs]
```

---

## The Orchestrator

The Orchestrator is the only "hardcoded" component. It's a meta-agent that:

1. **Reads the agent pool** — knows every agent's name, description, tools, and tags
2. **Analyzes the user's message** — determines intent, complexity, and required capabilities
3. **Selects agents** — picks the minimal set of agents needed for the task
4. **Chooses a team mode** — picks the Agno team interaction mode that fits best
5. **Assembles and runs the team** — creates the team on-the-fly, executes, returns results

```python
# Simplified orchestrator logic
class Orchestrator:
    def __init__(self, agent_pool: AgentPool, config: Settings):
        self.pool = agent_pool
        self.planner = Agent(
            model=get_model(config.models.default),
            reasoning=True,
            instructions=[ORCHESTRATOR_SYSTEM_PROMPT],
            output_schema=TeamPlan,  # structured output
        )

    async def handle(self, message: str, context: ConversationContext) -> Response:
        # Step 1: Analyze the task and decide on a team
        plan: TeamPlan = await self.planner.arun(
            f"Agent pool:\n{self.pool.describe()}\n\n"
            f"Conversation context:\n{context.summary()}\n\n"
            f"User message: {message}"
        )

        # Step 2: Build the team dynamically
        agents = [self.pool.get(name) for name in plan.agent_names]
        team = Team(
            name=plan.team_name,
            mode=plan.team_mode,   # route | coordinate | broadcast | tasks
            members=agents,
            instructions=plan.team_instructions,
        )

        # Step 3: Execute
        return await team.arun(message, stream=True)
```

### TeamPlan (Structured Output)

The Orchestrator's decision is a structured object:

```python
class TeamPlan(BaseModel):
    """The Orchestrator's decision on how to handle a task."""

    team_name: str                          # descriptive name for this team
    team_mode: Literal[
        "route", "coordinate", "broadcast", "tasks"
    ]                                       # Agno team mode
    agent_names: list[str]                  # agents to include
    team_instructions: list[str]            # dynamic instructions for the team leader
    reasoning: str                          # why this configuration was chosen
```

### Team Mode Selection

The Orchestrator chooses the mode based on task characteristics:

| Mode | When Used | Example Tasks |
|---|---|---|
| **route** | Single-capability task, one agent is clearly best | "What does this function do?", "Commit my changes" |
| **coordinate** | Multi-step task requiring different capabilities | "Add an API endpoint with tests", "Refactor auth and update docs" |
| **broadcast** | Need multiple independent perspectives | "Review this PR from security, performance, and correctness angles" |
| **tasks** | Autonomous multi-step goal with iteration | "Migrate the entire test suite from unittest to pytest" |

**Single-agent shortcut:** When the Orchestrator determines only one agent is needed, it skips team creation entirely and runs the agent directly. This keeps simple interactions fast and cheap.

### Orchestrator System Prompt (Summary)

The Orchestrator's system prompt tells it:

- The full agent pool with descriptions, tools, and tags
- The current conversation context (what's been discussed, what files are open)
- Guidelines for mode selection:
  - Prefer `route` for simple, single-purpose tasks (lowest overhead)
  - Use `coordinate` when the task requires multiple capabilities in sequence
  - Use `broadcast` when independent parallel perspectives add value
  - Use `tasks` only for large autonomous goals
- Minimize team size — don't include agents that won't contribute
- Consider conversation history — if the user has been working with the editor, the editor likely needs to continue

---

## Recursive Nesting: Agents That Build Teams

Unlike Claude Code (which caps sub-agents at one level), igni places **no limit on nesting**. Every agent can access the full agent pool and spawn its own sub-teams at runtime by default. Set `can_orchestrate: false` on an agent to disable this.

```
Orchestrator
    │
    ▼
┌─────────────────────────────┐
│  Team (coordinate)          │
│                             │
│  ┌─────────┐  ┌──────────┐  │
│  │ Planner │  │ Editor   │  │
│  │         │  │ (can_    │  │
│  │         │  │ orchestr)│  │
│  └─────────┘  └─────┬────┘  │
│                     │       │
└─────────────────────┼───────┘
                      │ Editor decides it needs help
                      ▼
               ┌──────────────────────────┐
               │  Sub-team (coordinate)   │
               │                          │
               │  ┌──────────┐ ┌────────┐ │
               │  │ Explorer │ │Reviewer│ │
               │  └──────────┘ └───┬────┘ │
               │                   │      │
               └───────────────────┼──────┘
                                   │ Reviewer spawns its own team...
                                   ▼
                                  ...
```

### How It Works

By default, every agent gets the `orchestrate` tool. This tool provides two functions:

**`spawn_team(task, agent_names, mode)`** — Create and run a sub-team for a specific subtask:

```
Agent calls: spawn_team(
    task="Find all usages of deprecated API and assess impact",
    agent_names=["explorer", "reviewer"],
    mode="coordinate"
)
```

The sub-team executes autonomously and returns its result to the parent agent.

**`spawn_agent(task, agent_name)`** — Simpler: run a single agent from the pool on a subtask:

```
Agent calls: spawn_agent(
    task="Search for all files importing the old auth module",
    agent_name="explorer"
)
```

### When Agents Should Nest

Agents don't need to be told *when* to spawn sub-teams — they decide based on the task. An editor agent mid-way through a complex refactor might realize it needs to explore unfamiliar parts of the codebase, so it spawns an explorer sub-team. A reviewer might spawn a security-auditor for a specific concern.

The key insight: **the agent closest to the problem is best positioned to decide if it needs help.**

### Safety: Depth Limits

While nesting is unlimited by design, practical safeguards prevent runaway recursion:

| Safeguard | Default | Configurable |
|---|---|---|
| `max_nesting_depth` | 5 | Yes, in config |
| `max_total_agents` | 20 per request | Yes, in config |
| `sub_team_timeout` | 120 seconds | Yes, per agent |

If a depth limit is reached, the agent is informed and must proceed without sub-teams. These are guardrails, not hard constraints — adjust them in `config.yaml`:

```yaml
orchestration:
  max_nesting_depth: 10
  max_total_agents: 50
  sub_team_timeout: 300
```

### Example: Editor That Orchestrates

```markdown
---
name: editor
description: Creates and modifies code files with minimal focused changes. Can spawn sub-teams for exploration or review when the task requires it.
tools: Read, Write, Edit, Bash, Glob, Grep
color: blue

# Ember extensions
tags: [coding, editing, file-write]
# can_orchestrate is true by default — this agent can spawn sub-teams
---

You create and modify code. Make minimal, focused changes.

## Sub-teams
You can spawn sub-teams when you need help:
- Spawn an explorer if you need to understand unfamiliar code before editing
- Spawn a reviewer after making complex changes to validate your work
- Spawn specialist agents (database, security) when the task touches their domain

Only spawn sub-teams when genuinely needed. For simple tasks, just do the work yourself.
```

---

## Ephemeral Agents: Generated On-the-Fly

Sometimes no agent in the pool fits the task. Instead of forcing a generic agent, the Orchestrator can **generate a new agent definition on the fly** — tailored to the specific task, with the right tools, model, and system prompt.

These are **ephemeral agents** — they exist only for the duration of the task (or session). They live in a `.ember/agents.ephemeral/` directory that is:
- **Gitignored** — not committed to the repo
- **Session-scoped** — cleaned up when the session ends (or kept if the user promotes them)
- **Visible** — the user can see and inspect them like any other `.md` file

```
Orchestrator analyzes task
    │
    ├── Pool has a good fit?  → use it
    │
    └── No good fit?  → generate ephemeral agent
                            │
                            ▼
                    ┌──────────────────────────┐
                    │ Write to                 │
                    │ .ember/                  │
                    │   agents.ephemeral/      │
                    │     terraform-migrator.md│
                    └──────────────────────────┘
                            │
                            ▼
                    Agent joins the pool
                    for this session
```

### How It Works

The Orchestrator's `TeamPlan` output includes an optional `ephemeral_agents` field:

```python
class TeamPlan(BaseModel):
    team_name: str
    team_mode: Literal["single", "route", "coordinate", "broadcast", "tasks"]
    agent_names: list[str]
    team_instructions: list[str]
    reasoning: str
    ephemeral_agents: list[EphemeralAgent] = []  # NEW

class EphemeralAgent(BaseModel):
    """An agent generated on-the-fly for a specific task."""
    filename: str         # e.g., "terraform-migrator.md"
    name: str
    description: str
    tools: str
    model: str
    tags: list[str]
    system_prompt: str
    reasoning: str        # why this agent was generated
```

When the Orchestrator decides to generate an ephemeral agent, it:
1. Writes the `.md` file to `.ember/agents.ephemeral/`
2. Loads it into the pool
3. Includes it in the team for the current task

### Example

**User:** "Migrate our Terraform configs from AWS provider v4 to v5"

No built-in agent knows Terraform. The Orchestrator generates:

```markdown
---
name: terraform-migrator
description: Migrates Terraform configurations from AWS provider v4 to v5, handling breaking changes and deprecated resources
tools: Read, Write, Edit, Bash, Grep, Glob
color: orange

tags: [terraform, infrastructure, migration]
ephemeral: true
---

You are a Terraform migration specialist. Your task is to migrate AWS provider v4 configurations to v5.

## Key v4 → v5 Breaking Changes
- `aws_s3_bucket` resource split into multiple resources
- `aws_instance` attribute renames
- Provider-level `default_tags` behavior changes
- State migration requirements for split resources

## Process
1. Find all .tf files
2. Identify v4-specific patterns
3. Apply v5 equivalents
4. Update provider version constraints
5. Run `terraform validate` after changes

## Safety
- Never run `terraform apply` without user confirmation
- Always show a plan of changes before executing
- Back up state files before any state migration
```

### Promoting Ephemeral Agents

If an ephemeral agent turns out to be useful, the user can promote it to a permanent agent:

```
/agents promote terraform-migrator
```

This moves it from `.ember/agents.ephemeral/` to `.ember/agents/` — now it's a permanent part of the pool and available in future sessions.

```
/agents list               # shows all agents, marks ephemeral ones
/agents promote <name>     # move ephemeral → permanent
/agents discard <name>     # delete an ephemeral agent
/agents ephemeral          # list only ephemeral agents
```

### Configuration

```yaml
# .ember/config.yaml
orchestration:
  generate_ephemeral: true     # Allow Orchestrator to generate agents on-the-fly
  max_ephemeral_per_session: 5 # Limit per session
  auto_cleanup: true           # Delete ephemeral agents when session ends
```

---

## Dynamic Behavior Examples

### Example 1: Simple question

**User:** "What does the `authenticate` middleware do?"

**Orchestrator decides:**
```json
{
  "team_mode": "route",
  "agent_names": ["explorer"],
  "reasoning": "Single read-only question about existing code. Explorer is sufficient."
}
```

Result: Explorer agent searches and reads code, returns explanation. Fast, cheap.

### Example 2: Multi-step coding task

**User:** "Add rate limiting to the /api/users endpoint with Redis, and add tests"

**Orchestrator decides:**
```json
{
  "team_mode": "coordinate",
  "agent_names": ["architect", "editor", "qa", "security"],
  "reasoning": "Multi-step task: needs architecture design (new dependency, multiple files), implementation, test generation, and security review for the new endpoint."
}
```

Result: Planner designs approach → Editor implements → Reviewer checks. All coordinated by the team leader.

### Example 3: Comprehensive code review

**User:** "Review this PR thoroughly"

**Orchestrator decides:**
```json
{
  "team_mode": "broadcast",
  "agent_names": ["reviewer", "security"],
  "reasoning": "PR review benefits from independent parallel analysis. Reviewer checks code quality, Security analyzes for vulnerabilities. Broadcast gives independent perspectives."
}
```

Result: Both agents analyze the PR independently, leader synthesizes findings.

### Example 4: Large migration

**User:** "Convert all class components to functional components with hooks"

**Orchestrator decides:**
```json
{
  "team_mode": "tasks",
  "agent_names": ["explorer", "editor", "reviewer"],
  "reasoning": "Large autonomous goal requiring iteration. Tasks mode will decompose into per-file conversions, execute each, and track progress."
}
```

Result: Team autonomously finds all class components, converts them one by one, reviews each change.

### Example 5: Agent spawns a sub-team (recursive nesting)

**User:** "Refactor the payment module to use the strategy pattern"

**Orchestrator decides:**
```json
{
  "team_mode": "coordinate",
  "agent_names": ["planner", "editor"],
  "reasoning": "Refactoring needs a plan first, then implementation. Editor can spawn sub-teams for exploration or review if needed."
}
```

**During execution:** The Editor agent, mid-refactor, encounters an unfamiliar utility module and decides it needs context:

```
Editor calls: spawn_team(
    task="Analyze the payment gateway adapters in src/payments/gateways/ — what interface do they share?",
    agent_names=["explorer"],
    mode="route"
)
```

The explorer sub-team runs, returns its findings. The Editor continues with full context. Later, after completing the refactor:

```
Editor calls: spawn_team(
    task="Review the strategy pattern refactor for correctness and check tests still pass",
    agent_names=["reviewer"],
    mode="route"
)
```

Result: A multi-level execution where the Editor autonomously sought help when it needed it — no manual sub-agent spawning by the user.

### Example 6: Custom agent in the mix

User has added `database.md` to `.ember/agents/` with `tags: [database, sql, migration]`.

**User:** "Create a migration to add a `last_login` column to the users table"

**Orchestrator decides:**
```json
{
  "team_mode": "coordinate",
  "agent_names": ["database", "editor"],
  "reasoning": "Database schema task. The custom 'database' agent has database expertise and tools. Editor handles file creation. Coordinate mode for sequential execution."
}
```

Result: Custom database agent designs the migration, Editor writes the file.

---

## Creating Custom Agents

Drop a `.md` file in any agents directory. That's it — it joins the pool immediately.

### Minimal Example (Claude Code compatible)

```markdown
---
name: docker
description: Manages Docker containers, images, and docker-compose configurations
tools: Bash, Read, Glob
color: blue
---

You manage Docker environments. You can build images, run containers, manage compose stacks, and debug container issues.

Always check running containers before making changes.
Prefer docker-compose over raw docker commands when a compose file exists.
```

This file works in both Claude Code and igni. In igni, the Orchestrator uses the description to decide when to include this agent.

### With Ember Extensions (MCP + orchestration)

```markdown
---
name: database
description: Database operations including queries, migrations, schema design, and optimization. Connects to the project database via MCP.
tools: Read, Write, Grep, Glob, LS
color: green

# Ember extensions
reasoning: true
reasoning_max_steps: 5
tags: [database, sql, migration, schema]
mcp_servers: [postgres]
---

You are a database specialist. You handle schema design, migrations, query optimization, and data operations.

## Rules
- Always read existing migrations before creating new ones
- Use the project's migration framework (check for alembic, knex, prisma, etc.)
- Never run destructive queries (DROP, TRUNCATE, DELETE without WHERE) without explicit user confirmation
- For schema changes, always create a migration file — never modify the database directly
- Explain query plans when optimizing

## Migration Naming
Use the format: `YYYYMMDD_HHMMSS_description.sql` (or framework equivalent)
```

### Example: Specialist Agent

```markdown
---
name: security-auditor
description: Security-focused code review that checks for OWASP Top 10 vulnerabilities, dependency issues, and security anti-patterns with confidence-based scoring
tools: Glob, Grep, LS, Read, WebSearch
color: red

# Ember extensions
reasoning: true
tags: [security, review, audit, read-only]
---

You are a security auditor. You review code for vulnerabilities and security anti-patterns.

## What to Check
- SQL injection (parameterized queries?)
- XSS (output encoding?)
- CSRF (token validation?)
- Authentication flaws (session management, password hashing)
- Authorization gaps (access control on every endpoint?)
- Secrets in code (API keys, passwords, tokens)
- Dependency vulnerabilities (outdated packages)
- Insecure deserialization
- Path traversal

## Output Format
For each finding:
- **Severity**: Critical / High / Medium / Low
- **Location**: file:line
- **Issue**: What's wrong
- **Fix**: How to fix it
```

---

## Agent Loader

The agent loader is responsible for:

1. **Discovering** `.md` files across all agent directories
2. **Parsing** YAML frontmatter + markdown body
3. **Validating** required fields and tool references
4. **Resolving** tool identifiers to Agno toolkit instances
5. **Building** `agno.Agent` objects
6. **Registering** them in the agent pool

```python
# Simplified loader logic
class AgentPool:
    def __init__(self):
        self.agents: dict[str, Agent] = {}

    def load_directory(self, path: Path, priority: int):
        for md_file in path.glob("*.md"):
            definition = parse_agent_md(md_file)
            name = definition.frontmatter["name"]

            if name not in self.agents:
                self.agents[name] = AgentEntry(defn, scope, build_agent(defn))
            elif scope_beats(scope, self.agents[name].scope):
                # Same name: project beats global beats built-in
                self.agents[name] = AgentEntry(defn, scope, build_agent(defn))

    def describe(self) -> str:
        """Generate a summary of all agents for the Orchestrator."""
        lines = []
        for agent in self.agents.values():
            lines.append(
                f"- **{agent.name}**: {agent.description} "
                f"[tools: {agent.tools}] [tags: {agent.tags}]"
            )
        return "\n".join(lines)

    def get(self, name: str) -> Agent:
        return self.agents[name]
```

### Hot Reloading

When running in interactive mode, the agent pool watches the agent directories for changes. Adding, modifying, or removing a `.md` file updates the pool without restarting igni.

---

## Skills

Skills are reusable prompted workflows — task recipes invoked via `/skill-name`. While agents define **who** does the work, skills define **what** to do. See [Skills](SKILLS.md) for the full specification, format, and examples.

```
/deploy staging              — invoke the deploy skill
/resolve-issues main         — fix issues CodeIndex flagged on your branch
/explain src/auth/           — deep-dive explanation using CodeIndex
```

Skills use the same `SKILL.md` format as Claude Code — drop Claude Code skills into `.ember/skills/` and they work immediately.

---

## Comparison with Claude Code

| Aspect | Claude Code | igni |
|---|---|---|
| Agent file format | YAML frontmatter `.md` | **Same format** — files are cross-compatible |
| Skill file format | `SKILL.md` in named directory | **Same format** — Claude Code skills work as-is |
| Tool names | `Read`, `Write`, `Glob`, `Grep`, `Bash` | Same names accepted, mapped to Agno toolkits |
| Team composition | Static (manually spawn sub-agents) | Dynamic (Orchestrator assembles per-task) |
| Team interaction | Single agent loop, one-level sub-agents | Agno team modes (route, coordinate, broadcast, tasks) |
| Nesting depth | 1 level (sub-agents can't spawn sub-agents) | **Unlimited** — agents spawn sub-teams recursively |
| Adding agents | Drop `.md` in `.claude/agents/` | Drop `.md` in `.ember/agents/` |
| Adding skills | Drop `SKILL.md` in `.claude/skills/name/` | Drop `SKILL.md` in `.ember/skills/name/` |
| Agent selection | User or parent agent decides | Orchestrator decides automatically |
| Skill execution | Runs inline or forked subagent | Runs inline or Orchestrator assembles a team |
| Migration path | — | Drop Claude Code agents + skills into igni, they just work |
