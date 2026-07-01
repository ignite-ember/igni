# First-Run Onboarding

> **Status: Planned — not yet implemented.** This document describes the design for a future release. Currently, first-run setup is handled by `init.py` which copies default agents, skills, and hooks into `.ember/` without an interactive onboarding flow.

When a user starts igni for the first time in a project, it doesn't just drop them into a blank prompt. It runs a guided onboarding flow that sets up agents tailored to their specific project.

## Flow Overview

```
First Run Detected
    │
    ▼
┌─────────────────────────────────┐
│  1. Create default agents       │  ← write built-in .md files
│     in .ember/agents/           │     to the project
└──────────┬──────────────────────┘
           │
           ▼
┌─────────────────────────────────┐
│  2. Explain the system          │  ← what agents are, how they
│                                 │     work, how to customize
└──────────┬──────────────────────┘
           │
           ▼
┌─────────────────────────────────┐
│  3. Ask about the user          │  ← role, expectations,
│     and their work              │     preferences, workflow
└──────────┬──────────────────────┘
           │
           ▼
┌─────────────────────────────────┐
│  4. Read project context        │  ← CodeIndex cloud summaries
│     from CodeIndex              │     + local files (README, etc.)
│                                 │     If not ready, retry next session
└──────────┬──────────────────────┘
           │
           ▼
┌─────────────────────────────────┐
│  5. Propose tailored agents     │  ← generate custom agent .md
│     for this project            │     files based on all context
└──────────┬──────────────────────┘
           │
           ▼
┌─────────────────────────────────┐
│  6. User reviews & approves     │  ← accept, modify, or skip
└──────────┬──────────────────────┘
           │
           ▼
    Ready to work
```

---

## Step 1: Create Default Agents

On first run, igni copies the built-in agent `.md` files into `.ember/agents/`:

```
.ember/
└── agents/
    ├── explorer.md
    ├── architect.md
    ├── planner.md
    ├── editor.md
    ├── simplifier.md
    ├── reviewer.md
    ├── security.md
    ├── qa.md
    ├── debugger.md
    ├── git.md
    └── conversational.md
```

These are working agents — the system is functional immediately. The rest of the onboarding improves them.

**Why copy instead of reference?** Making them local means:
- Users can see and edit them directly
- They're committed to the repo (team gets them)
- The project isn't dependent on the igni version for agent definitions

---

## Step 2: Explain the System

igni introduces itself and explains the agent system:

```
 ◆ igni — ignited and ready.

I've sparked 11 agents in .ember/agents/:

  explorer.md        searches and reads your codebase
  architect.md       designs component architecture
  planner.md         designs implementation plans
  editor.md          creates and modifies files
  simplifier.md      post-edit code polish
  reviewer.md        reviews code for quality
  security.md        vulnerability analysis
  qa.md              test generation and review
  debugger.md        bug diagnosis and root cause analysis
  git.md             handles version control
  conversational.md  answers questions

These are Markdown files — open them, read them, change them.
When you ask me something, I assemble a team on the fly.

Drop new .md files into .ember/agents/ to add your own.
```

---

## Step 3: Ask About the User

The onboarding agent asks a short set of questions to understand the user's context:

```
A few questions to tailor things to your workflow:

1. What's your role? (e.g., backend dev, full-stack, data engineer, DevOps)
2. What are you primarily working on right now?
3. Any tools or workflows I should know about? (CI/CD, testing frameworks,
   deployment targets, etc.)
4. How do you prefer to work with AI? (e.g., "just do it" vs "show me a
   plan first", verbose vs concise)
5. Anything that's off-limits? (files not to touch, patterns to avoid, etc.)
```

Answers are saved to Agno's user memory (persists across sessions) and influence:
- How the Orchestrator phrases its team instructions
- Which agents get prioritized
- The tone and detail level of responses
- Safety constraints (off-limits files/paths)

The user can skip this step (`--skip-onboarding` or just pressing Enter).

---

## Step 4: Read Project Context from CodeIndex

igni connects to **CodeIndex** cloud to pull project intelligence:

```python
async def fetch_project_context(project_path: str) -> ProjectContext:
    """Fetch project summaries and metadata from CodeIndex."""
    client = CodeIndexClient()  # authenticated via igni login

    # Look up this project by repo URL or directory fingerprint
    project = await client.get_project(
        repo_url=get_git_remote_url(project_path),
        fingerprint=compute_project_fingerprint(project_path),
    )

    if not project:
        return ProjectContext.empty()

    return ProjectContext(
        summary=project.summary,              # high-level project description
        stack=project.tech_stack,             # languages, frameworks, tools
        architecture=project.architecture,     # module structure, patterns
        conventions=project.conventions,       # coding standards, naming
        team_context=project.team_context,     # who works on what
        recent_activity=project.recent_activity,  # recent focus areas
        known_issues=project.known_issues,     # open bugs, tech debt
    )
```

**What CodeIndex provides:**
- **Project summary** — what this project does, its purpose
- **Tech stack** — languages, frameworks, databases, infrastructure
- **Architecture** — module structure, design patterns, key abstractions
- **Conventions** — coding standards, naming conventions, PR process
- **Team context** — who works on which areas, ownership
- **Recent activity** — what the team has been focused on lately
- **Known issues** — tracked bugs, tech debt, areas of concern

**Fallback:** If CodeIndex is unavailable or the project isn't indexed yet, igni falls back to local analysis for now:
- Parse `README.md`, `package.json`, `pyproject.toml`, `Cargo.toml`, etc.
- Scan directory structure to infer architecture
- Read `ember.md` / `CLAUDE.md` / `AGENTS.md` if they exist
- Detect language/framework from file extensions and imports

The CodeIndex fetch is marked as pending and **retried automatically at the start of the next session**. Once CodeIndex data becomes available, igni merges the richer context into the existing agent pool and may suggest agent updates:

```
CodeIndex context is now available for this project.
Based on the full project analysis, I suggest updating these agents:
  editor.md — add knowledge of your Alembic migration patterns
  reviewer.md — add your team's PR review checklist

Want me to apply these updates? [Y/n]
```

This deferred approach means onboarding is never blocked by CodeIndex — the user can start working immediately with local context, and the experience improves once cloud analysis completes.

---

## Step 5: Propose Tailored Agents

Using all gathered context (user profile + CodeIndex data + local analysis), the onboarding agent proposes project-specific agents:

```
Based on your project (FastAPI + React + PostgreSQL + Redis):

I suggest adding these specialized agents:

  api-developer.md
    Tools: Read, Write, Edit, Bash, Grep, Glob
    "Specializes in FastAPI endpoint development. Knows your router
     patterns in src/api/, uses Pydantic models from src/schemas/,
     and follows your OpenAPI docstring conventions."

  database.md
    Tools: Read, Write, Edit, Bash, Grep, Glob
    MCP: postgres
    "Handles Alembic migrations, SQLAlchemy models in src/models/,
     and query optimization. Knows your naming conventions for
     migration files."

  frontend.md
    Tools: Read, Write, Edit, Bash, Grep, Glob
    "React component development in src/frontend/. Uses your custom
     hooks from src/frontend/hooks/, follows your Tailwind patterns,
     and knows your component testing setup with Vitest."

  ci-cd.md
    Tools: Read, Write, Edit, Bash, Grep
    "Manages GitHub Actions workflows in .github/workflows/.
     Knows your staging/production deployment pipeline and
     environment variable conventions."

Shall I create these? You can also:
  [Y] Yes, create all of them
  [E] Let me edit them first (opens in editor)
  [S] Skip for now (I'll add agents later)
  [C] Create but let me review each one
```

### How Agent Proposals Are Generated

The proposal agent uses reasoning to analyze all context and generate agents:

```python
class OnboardingProposer:
    """Generates project-specific agent proposals."""

    def __init__(self, pool: AgentPool, config: Settings):
        self.agent = Agent(
            model=get_model(config.models.default),
            reasoning=True,
            reasoning_min_steps=3,
            output_schema=AgentProposals,
            instructions=[PROPOSAL_SYSTEM_PROMPT],
        )

    async def propose(
        self,
        user_profile: UserProfile,
        project_context: ProjectContext,
        existing_agents: list[str],
    ) -> AgentProposals:
        return await self.agent.arun(
            f"User profile:\n{user_profile}\n\n"
            f"Project context:\n{project_context}\n\n"
            f"Existing agents: {existing_agents}\n\n"
            "Propose specialized agents for this project."
        )

class AgentProposals(BaseModel):
    """Structured output for agent proposals."""
    agents: list[ProposedAgent]
    reasoning: str

class ProposedAgent(BaseModel):
    """A proposed agent definition."""
    filename: str          # e.g., "api-developer.md"
    name: str              # e.g., "api-developer"
    description: str
    tools: str             # e.g., "Read, Write, Edit, Bash, Grep, Glob"
    model: str             # e.g., "sonnet"
    tags: list[str]
    system_prompt: str     # the markdown body
    reasoning: str         # why this agent was proposed
```

### Proposal Guidelines

The proposal agent follows these rules:
- **Don't duplicate** — don't propose agents that overlap with existing built-ins
- **Be specific** — reference actual directories, files, and patterns from the project
- **Match the stack** — propose agents that know the project's specific frameworks
- **Consider the user** — a DevOps engineer gets different proposals than a frontend dev
- **Include MCP** — if relevant MCP servers are configured, propose agents that use them
- **Keep it practical** — 3-5 proposals max, each with a clear purpose

---

## Step 6: User Reviews & Approves

The user can:
- **Accept all** — agents are written to `.ember/agents/`
- **Edit first** — opens proposed agents in their editor for tweaking
- **Review one by one** — approve/reject/edit each proposal individually
- **Skip** — onboarding completes with just the default agents

Approved agents are written as `.md` files:

```markdown
---
name: api-developer
description: Specializes in FastAPI endpoint development, Pydantic schemas, and OpenAPI documentation for this project
tools: Read, Write, Edit, Bash, Grep, Glob
color: green

# Ember extensions
tags: [api, fastapi, backend, endpoints]
---

You are a FastAPI API developer for this project.

## Project-Specific Knowledge
- Routes are defined in src/api/routes/
- Pydantic schemas live in src/schemas/
- All endpoints must have OpenAPI docstrings
- Use dependency injection for authentication (src/api/deps.py)
- Database sessions are managed via src/db/session.py

## Conventions
- Route files are named after the resource (users.py, items.py)
- Use async def for all endpoint handlers
- Return Pydantic models, never raw dicts
- Include pagination for list endpoints (use src/api/pagination.py)

## Testing
- Tests go in tests/api/
- Use the test client fixture from conftest.py
- Every endpoint needs at least: happy path, auth failure, validation error
```

---

## Subsequent Runs

On subsequent runs, the onboarding flow is skipped. Instead:

1. Agent pool loads from existing directories (including previously created agents)
2. If CodeIndex context was pending from onboarding, retry the fetch — if now available, suggest agent updates
3. CodeIndex context is refreshed in the background (if configured)
4. If the project has changed significantly (new frameworks, major refactors), igni may suggest updating agent definitions:

```
I notice you've added GraphQL (graphene) to the project since the agents
were last configured. Want me to propose a graphql.md agent?
```

## Re-Running Onboarding

Use slash commands interactively:

```
/agents refresh   — re-scan agent directories and reload pool
```

---

## Configuration

```yaml
# .ember/config.yaml

onboarding:
  skip: false                    # Skip onboarding entirely
  auto_create_defaults: true     # Copy default agents on first run
  ask_questions: true            # Interactive Q&A step
  codeindex: true                # Fetch context from CodeIndex (uses igni login)
  propose_agents: true           # Generate project-specific agent proposals
  max_proposals: 5               # Max number of agents to propose
```

Environment variables:
```
EMBER_SKIP_ONBOARDING=true     # Skip onboarding (CI/CD)
```

---

## Project Structure

```
src/ember_code/
├── onboarding/
│   ├── __init__.py
│   ├── flow.py               # Main onboarding orchestration
│   ├── questionnaire.py      # User Q&A step
│   ├── proposer.py           # Agent proposal generation
│   ├── codeindex.py          # CodeIndex client
│   ├── local_analyzer.py     # Fallback local project analysis
│   └── defaults.py           # Default agent file contents
```
