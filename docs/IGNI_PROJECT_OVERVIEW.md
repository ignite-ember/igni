# igni — Research Report

**Scope:** What's actually shipped, who it's for, how `ember-server` relates, and how it compares to alternatives — verified against both repositories.

---

## 1. Executive Summary

**igni is a multi-agent AI coding assistant built on the Agno orchestration framework — released as v1.0.0 on 2026-07-30 (`git tag v1.0.0`).** Its core architectural differentiator is **CodeIndex** — a pre-built semantic + metadata index over the codebase produced by a separate backend (`ember-server`) and consumed locally as a per-commit **Neo4j graph + vector store** (BE-spawned, no Docker), so search runs on the user's machine and the server never holds a copy of the index. Self-published benchmarks put it at 82% directly-mergeable outputs vs Claude Code's 52% on a 12-case suite, but at ~33% slower wall time and 1/80th the per-run cost (driven by a smaller default model, MiniMax-M2.7, plus the pre-computed index). It holds extensive Claude Code parity (33/61 rows ✅, 1 explicit gap, 5 partials, 17 igni-only extras) and ships across four surfaces (Tauri desktop + VSCode + JetBrains + browser) from a single React bundle.

---

## 2. What igni Does

### 2.1 Product capabilities (evidence-anchored)

| Capability | Evidence |
|---|---|
| **Multi-agent team orchestration** | Orchestrator dynamically assembles 5–13 specialist agents from `.md` files using five team modes (Single, Route, Coordinate, Broadcast, Tasks). Unlimited agent nesting depth (vs Claude Code's one-level cap). |
| **Pre-built semantic CodeIndex** | Per-commit JSONL changeset pipeline from `ember-server` → GCS → local **Neo4j** graph+vector store (BE-spawned, one DB per commit, no Docker). The agent-facing surface is a single tool in ``core/tools/codeindex/tool.py`` — ``codeindex_cypher(cypher=..., confirm_raw_cypher=True, ...)`` — read-only Cypher over the graph store, with ``confirm_raw_cypher=True`` + ``project_hash`` scoping + ``$param`` allowlist enforced by the toolkit. The ``data-architect`` agent is the curated owner; other agents come through it. Index access is the toolkit's single seam — agents never touch the Neo4j driver directly. |
| **Four shipping surfaces** | Tauri desktop (signed + notarized), VSCode (webview), JetBrains (JCEF), browser (Vite/React). All four surfaces load the same `clients/web` bundle. The earlier Textual TUI is no longer shipped. |
| **Built-in specialist agents** | 10 base agents (architect, data-architect, docs, editor, explorer, qa, reviewer, security, simplifier, debugger) + 6 `.codeindex.md` variants that auto-activate when the repo has a built index. |
| **Permission system** | 5 modes (`default`/`dontAsk`/`acceptEdits`/`bypassPermissions`/`plan`), 6-step evaluation pipeline, scope-prefixed deny rules that survive bypass mode (a `Bash(rm *)` deny holds even under `bypassPermissions`). |
| **Hook system** | 18 of 30 Claude Code hook events, 4 of 5 handler types, all 3 execution modes (sync / `async` / `asyncRewake`). `permissionDecision` envelope on `PreToolUse` honored. |
| **MCP client** | MCP client fully wired (consume external servers via `/mcp`). |
| **Output styles** | `default` / `explanatory` / `learning` markdown-defined styles; `/output-style` slash command hot-patches the live team. |
| **Plan mode** | Read-only sandbox plus user approval workflow; both `/plan` slash command and `enter_plan_mode(reason)` agent tool; agent cannot exit on its own (security invariant). |
| **Knowledge base** | Local Neo4j (BE-spawned, per-commit DB) + EmberEmbedder (384d) with `/knowledge add/search/sync-knowledge`. Per-project `.igni/knowledge.yaml` file for git-shareable knowledge sharing. |
| **Auto-memory** | Per-project `~/.igni/projects/<slug>/memory/MEMORY.md` (200 lines / 25KB cap), with cross-tool fallback to `~/.claude/.../memory/MEMORY.md` — Claude Code users don't need to migrate their memory bank. |
| **Scheduling, looping, forking** | `/schedule` (cron + one-shot), `/loop` (recurring), `/fork [name]` (clone history under new id). Claude Code now ships `/loop` and `/fork` and a `CronCreate`/`CronList`/`CronDelete` cron toolkit — igni's differentiator is the **explicit user-facing `/schedule` slash command + a `scheduler_tasks` SQLite table** that surfaces scheduled work as a first-class UI concept, rather than relying on the model picking up the agent-loop Tools. |
| **In-session chat search** | SQLite-backed `search_chat` RPC + UI bar (BE 23 cases, FE 24 cases — snippet windowing, case-insensitive, scroll formatting). |
| **Guardrails** | PII detection, prompt injection detection, OpenAI moderation API (configurable, off by default). |
| **Audit logging + managed policy** | `AuditLogger` with `jq`-compatible JSON lines, ISO-8601 timestamps, OSError-swallowing kill-switch. Enterprise: 5 settings tiers (managed > CLI > local > project > user), 4 plugin scopes (managed beats project), platform-managed CLAUDE.md/ember.md. |
| **Plugin primitives** | LSP server primitive (33 tests), monitor primitive with bounded exponential backoff (26 tests), plugin agent security envelope (force_isolation=worktree, no hooks/mcpServers for plugin agents). Only explicit gap: plugin theme primitive. |
| **Cross-tool compatibility** | Reads `CLAUDE.md`, `.claude/agents/*.md`, `.claude/skills/`, `.mcp.json` out of the box when `cross_tool_support` is enabled — explicit positioning for Claude Code refugees. |
| **Org-level Groups (zero-install departmental rollout)** | Org admins curate per-department override packs — agents (markdown), MCPs (JSON), plugins (git-source or YAML), settings (deep-merged). The end-user CLI fetches the pack at login and materializes it to `~/.igni/group-policy/` at priority 4.5. No per-machine deployment needed; admins push, users receive on next fetch. See [Groups](#org-level-groups-and-overrides) for the full story. |

Source: `README.md:1-251`, `QUICKSTART.md:1-483`, `CLAUDE_CODE_PARITY.md:13-66` (parity tally table), `docs/ARCHITECTURE.md:1-120`, `docs/CODEINDEX.md`, `docs/NEO4J_MIGRATION_DONE.md`, `pyproject.toml:38-67`. (Internal module is still `ember_code` and project-context file is still `ember.md` — preserved per the v0.7.0 rebrand for back-compat with existing installs.)

### 2.2 Architecture shape

```
┌── Frontend process (any of 4 surfaces) ─────────────────┐
│  Tauri desktop  │  VSCode webview  │  JetBrains JCEF    │
│  Browser (Vite/React)                                     │
│  All load clients/web → ONE React+TS bundle              │
└────────────────────────────┬─────────────────────────────┘
                             │ WebSocket / Unix socket
┌────────────────────────────▼─────────────────────────────┐
│ Backend process                                         │
│  BackendServer → CommandHandler → Session → Orchestrator│
│  Agno Agent pool (13 .md files + 6 .codeindex variants) │
│  Tool Layer: Bash / Write / Edit / Schedule / CodeIndex │
│  Knowledge: local Neo4j (BE-spawned) + EmberEmbedder    │
│  Hooks (18 events) + Permissions (5 modes, 6-step eval) │
└────────────────────────────┬─────────────────────────────┘
                             │ consumer side (ChangesetFetcher)
                             ▼
                Local Neo4j (BE-spawned, per-commit DB)
```

The local Neo4j instance (one BE-spawned DB per commit, no Docker) is the consumer endpoint of the producer/consumer split. `ember-server` writes JSONL; igni's consumer pulls over plain HTTPS and applies it. (`docs/ARCHITECTURE.md:1-67`, `docs/CODEINDEX.md`, `docs/NEO4J_MIGRATION_DONE.md`.)

---

## 3. Who It's For

### Primary persona: developers migrating from Claude Code or similar single-loop tools

The positioning evidence is unambiguous:

- **`README.md:11`** says: *"Claude Code uses a single agent loop — powerful but monolithic. igni takes a different approach: dynamic multi-agent orchestration."*
- **`docs/MIGRATION.md`** exists as a first-class doc addressing Claude Code and Codex users.
- **`CLAUDE_CODE_PARITY.md`** documents deliberate cross-tool compatibility: `cross_tool_support` setting reads `.claude/agents/*`, `.claude/skills/`, `~/.claude/projects/<slug>/memory/MEMORY.md`. A Claude Code user can drop igni in alongside without rewriting their memory bank.
- **Test counts** in the README footers (1363 → 2503 → 2619 BE suite, 473 vitest cases) signal a team that publishes test evidence rather than marketing claims.

### Secondary persona: every other IT role, curated per role via Groups (zero install)

igni's reach is not limited to software engineers. **Any knowledge worker with a structured workflow can be put behind a curated group** — the same way a security team deploys a security-tuned SIEM to the SOC, a design org deploys a design-tuned AI to the design team, and HR deploys a privacy-tuned AI to recruiters. The shape of a Group is intentionally narrow: four override kinds (`agents`, `mcps`, `plugins`, `settings`) over four surfaces — so the rollout is a manifest edit, not a per-laptop installer.

| Role / function | What a curated Group looks like | Admin effort |
|---|---|---|
| **Security / AppSec team** | Security-tuned agent set, audit-logging MCP, restrictive permissions, no shell-write for non-security groups. | One pack, one membership list. |
| **Frontend / Design Platform** | React/Vue/Tailwind component specialists, design-system knowledge pack, Figma MCP, read-only shell. | One pack, broadcast to the design org. |
| **Data / Analytics** | SQL/Python + warehouse-connected MCPs (Snowflake / BigQuery), knowledge entries = data dictionaries and runbook links. | One pack, provisioned to the data org. |
| **SRE / Platform** | bash + kubectl + Terraform specialists, incident-response skills, runbook knowledge, broad shell permissions scoped by guardrails. | One pack, pagers carry the right tools automatically. |
| **Support / Customer Success** | RAG over the KB, ticket-system MCPs (Zendesk / Intercom), no shell at all. | One pack, no engineering review per user. |
| **HR / People Ops** | Privacy-tuned agents, PII guardrails on by default (the `pii_detection` + `moderation` guardrails in `README.md:218-225`), knowledge entries = policy docs. | One pack, governed posture out of the box. |
| **New hires (any function)** | `onboarding.md` agent, paused `Shell` permission until training completes, restricted MCP set. | One pack on day one; graduated over time. |
| **Sales / GTM** | Outreach-comms-tuned agents, CRM MCPs (Salesforce / HubSpot), no shell, prompt-injection guardrails on. | One pack, sent to every AE / SE. |
| **Finance / Legal** | Knowledge-only mode (no shell, no file edit), contracts knowledge base, document-review MCPs. | One pack, audit-friendly by default. |
| **Engineering leadership** | Aggregated view: read-only blast radius, no shell, dashboards of agent activity from `AuditLogger`. | One pack, observability without action rights. |

The rollout mechanic — packs published from the portal, fetched at `igni /login` or surface activation, materialized to `~/.igni/group-policy/` at priority 4.5 — is identical for every row above. **No per-machine deployment, no per-user setup script, no per-role config handholding.** The org-level Groups feature means igni is not "a developer tool that admins tolerate"; it is **an IT-deliverable tool that admins can roll out to any department with a manifest edit**.

### Two deployment shapes

**Solo developer / OSS community path** — since the Textual TUI is no longer shipped, end users go straight to one of the four GUI surfaces: the **Tauri desktop app** (signed + notarized installer for macOS / Windows / Linux — CI-built from `clients/tauri`), the **VSCode extension** (one-command marketplace install; auto-bootstraps the Python backend on first activation), the **JetBrains plugin** (IntelliJ / PyCharm / WebStorm via the Marketplace, JCEF panel hosting the shared web UI), or the **browser bundle** (`clients/web` Vite/React served from any host). The backend comes embedded with each surface — there is no separate CLI mode. BYO model supported for any OpenAI-compatible API (OpenAI, Anthropic, Gemini, Groq, Ollama, OpenRouter per portal docs). The default hosted MiniMax-M2.7 model plus `igni /login` (or legacy `ignite-ember`) makes the first-run path zero-config. See `docs/CONFIGURATION.md`.

**Team / enterprise path** — self-hostable `ember-server` (FastAPI + Postgres + Celery + Redis + GCS), GKE-deployed via Helm with three environments (dev/stage/prod) and WIF-based GitHub Actions deploys. Enterprise-grade controls: 5-tier settings precedence including platform-managed `/Library/Application Support/Ember/managed-settings.yaml` (kept under the `Ember/` directory name for back-compat with org policies that already reference the legacy path) that wins over CLI flags; 4-tier plugin scopes including `/etc/ember/` managed scope; scoped deny rules that survive bypass mode. Apache 2.0 license permits commercial use. Source: `ember-server/README.md:178-216`, `README.md:241-249`.

### Use cases the docs actively support

Short list — focused on what a stakeholder will actually ask "can igni do X?" about:

| Use case | What igni does |
|---|---|
| **Long-form refactor on a real codebase** | Mandatory "What already exists" preamble forces reuse-naming first; agents come through the ``data-architect`` agent, which authors read-only ``codeindex_cypher(...)`` to identify the right target. |
| **Triage of large repos** | The ``data-architect`` agent walks the index via ``codeindex_cypher(...)`` (multi-hop walks, aggregates, ``EXPLAIN``). Other `*.codeindex.md` specialists route their graph questions through it. |
| **Multi-language codebase** | Producer side covers **58 languages** via tree-sitter. |
| **IDE work** | Four GUI surfaces — Tauri desktop, VSCode, JetBrains, browser — all load the same React bundle. |
| **Scheduled / recurring automation** | `/schedule` (cron + one-shot), `/loop`, `/fork`. Claude Code has `/loop`/`/fork`/`CronCreate`; igni's edge is the first-class `/schedule` UI and persistent `scheduler_tasks` table. |
| **Role-tuned rollout for any department** | See role list below and [Org-level Groups and overrides](#org-level-groups-and-overrides) — administered via the portal, delivered zero-install. |

### Roles igni is curated for (via Groups)

Each role ships as a Group in the portal — one pack, broadcast to the team, no per-machine install:

- **Software engineer** — primary persona. Multi-agent teams, `.codeindex.md` specialists, 5 surfaces, fast inference tiers.
- **Security / AppSec** — security-tuned agent set, audit-logging MCP, restrictive permissions, no shell-write for non-security groups.
- **Frontend / Design Platform** — React/Vue/Tailwind specialists, design-system knowledge pack, Figma MCP, read-only shell.
- **Data / Analytics** — SQL/Python specialists, warehouse MCPs (Snowflake / BigQuery), knowledge entries = data dictionaries and runbook links.
- **SRE / Platform** — bash + kubectl + Terraform specialists, incident-response skills, runbook knowledge, broad shell under guardrails.
- **Support / Customer Success** — RAG over the KB, ticket-system MCPs (Zendesk / Intercom), no shell.
- **HR / People Ops** — privacy-tuned agents, PII guardrails on by default, knowledge entries = policy docs.
- **New hires (any function)** — `onboarding.md` agent, paused `Shell` until training, restricted MCP set.
- **Sales / GTM** — outreach-comms-tuned agents, CRM MCPs (Salesforce / HubSpot), no shell, prompt-injection guardrails on.
- **Finance / Legal** — knowledge-only mode, contracts knowledge base, document-review MCPs.
- **Engineering leadership** — observability-only blast radius, dashboards of agent activity from `AuditLogger`, no shell.

*The list above is an illustrative proposal of common shapes — Groups are not limited to these roles. An org can define any department, squad, project, or workflow group; the four override kinds (`agents`, `mcps`, `plugins`, `settings`) compose freely.*

### Org-level Groups and overrides

This is the headline story for **admins who want a curated AI experience delivered to a department without touching any individual workstation**.

- **Group as a unit of curation.** `ember-server`'s portal exposes group CRUD (`endpoints/portal/groups.py:33+` — `POST /groups`, `GET /groups`, `PATCH /groups/<id>`, plus `OrgGroupMemberList`, `OrgGroupOverrideList`, `OrgGroupPack`). An org admin creates a group ("Frontend Platform", "Security", "Data Platform"), assigns members, then attaches override entries to the group.
- **Override kinds.** Each group's pack carries four kinds of overrides — `agents` (markdown), `mcps` (JSON), `plugins` (git-source URL or YAML), `settings` (JSON or YAML, deep-merged into the config). Each entry can be `enabled: True` (push in) or `enabled: False` (block). Schema: `core/config/group_policy.py:27-69` (`GroupPolicyOverrideEntry`) and `core/config/group_policy.py:72-119` (`GroupPolicyPack`).
- **Client materialization.** At startup, the CLI fetches the pack for the authenticated user's groups and materializes each entry:
  - Agents → `~/.igni/group-policy/agents/<name>.md` (picked up by the agent loader at priority 4.5)
  - MCPs → `~/.igni/group-policy/mcps/<name>.json`
  - Plugins → installed via `PluginInstaller` into `<data_dir>/group-policy/plugins/` (priority 4.5 — between project-ember and managed-ember), so the plugin loader sees them naturally
  - Settings → deep-merged into the existing settings tier stack via `core/config/merge_plan.py:250` (`GroupPolicyTier`)
- **Zero install at the workstation.** The end user's machine only needs `brew install ignite-ember` (or the desktop app). When they `igni /login`, the appropriate group packs are pulled automatically — they get the curated agent set, MCP servers, plugin roster, and merged config without anyone running a setup script. A backend/security team can flip the group's overrides today; every member of the group sees the change on next fetch (5-min TTL on the cache, `group_policy.py:22-24`).
- **What this enables.** Per-department personas without per-user configuration:
  - A "Security" group gets a security-focused agent set, audit-logging MCP, and restrictive permissions.
  - A "Frontend" group gets React/Vue/Tailwind specialists + a design-system knowledge pack + the Figma MCP, no shell-write rights.
  - A "New Hire" group gets an `onboarding.md` agent and a paused `Shell` permission until they complete training.
  - A "Data Platform" group pins a known-good set of plugins and removes the experimental ones.

This is the path most AI-coding products don't ship: **admin pushes → user receives on next login, no per-machine deployment.**

### Pricing & hosted models

The portal exposes a Stripe-backed per-seat subscription across three tiers — `lite`, `pro`, `max` — with a volume-discount ladder. Authoritative source: `ember-server/app/api/base_v1/schema/portal/billing.py:135-200` (`PLAN_CATALOG` + `VOLUME_TIERS`).

| Tier | Per-seat / mo | Parallel agents | Inference speed | CodeIndex repos / seat | Support |
|---|---|---|---|---|---|
| **Lite** | **$25** | 2 | standard (25 tps cap) | 1 | community |
| **Pro** | $50 | 5 | fast (50 tps cap) | 2 | priority |
| **Max** | $100 | 10 | high-speed (uncapped) | 3 | priority + SSO & SAML |

| All three tiers include | Detail |
|---|---|
| **Unlimited tokens on the hosted MiniMax-M2.7 model** | The `features: ['Unlimited tokens', …]` line in every tier applies to the MiniMax-M2.7 default — **no per-token usage meter on the default model**. GLM and Kimi are planned additions to the subscription roster (see hosted model note below). |

| Seats | Volume discount |
|---|---|
| 1–4 | 0 % |
| 5–49 | 10 % |
| 50–99 | 15 % |
| 100–249 | 20 % |
| 250+ | 25 % |

Beyond the org-wide CodeIndex pool (`code_index_repos_per_seat × seats`), extra repos are billed at `CODE_INDEX_OVERAGE_RATE = $15/repo/mo` (`billing.py:99`), so there's no arbitrage between bundled and standalone CodeIndex.

**Hosted model roster.** The default hosted model on `/login` is **MiniMax-M2.7** (per `README.md:46` and the model classifier in `pyproject.toml`). **GLM and Kimi are planned to be added to the subscription soon.** BYO-model (`api_key_env`, `api_key_cmd`, or direct key in `.igni/config.yaml` per `README.md:62-74`) remains supported today for teams that want Anthropic, OpenAI, or self-hosted inference.

---

## 4. How ember-server Fits In

`ember-server` is **the producer in a producer/consumer split**. It does not hold the searchable index — that lives in `ember-code` on the user's machine.

### Architecture

```
GitHub/GitLab repo
      │  webhook (push, PR, comment)
      ▼
┌─ ember-server (producer) ──────────────────────────────┐
│  POST /v1/webhooks/{github,gitlab}                      │
│      │                                                  │
│  Celery + gevent (100 concurrency) worker               │
│      │                                                  │
│  CodeAnalysisWorkflow — 5 phases:                       │
│    0  tree-sitter AST extraction (58 languages)         │
│    1  Initial file summaries (AI)                       │
│    2  Entity summaries (per class/fn/method/var)        │
│    3  Final file summaries (entity-aware)               │
│    4  Folder/module rollups                             │
│      │                                                  │
│  5  JSONL changeset emission                            │
│      │                                                  │
│  GCS  gs://{bucket}/{prefix}/{repo_id}/{sha}.jsonl       │
└──────────────────┬──────────────────────────────────────┘
                   │
                   ▼
┌─ ember-code consumer ──────────────────────────────────┐
│  ChangesetFetcher.pull_and_apply()                       │
│      │ copy-on-write into per-commit local Neo4j DB     │
│         (BE-spawned; one DB per commit; no Docker)       │
│  Local Neo4j (no server roundtrip for searches;          │
│  search runs on the user's machine)                     │
└─────────────────────────────────────────────────────────┘
```

### Key properties

- **Producer-only** — per `docs/CODEINDEX.md`, "the server is now a producer-only service: it generates summaries and ships changesets. It does **not** answer search/items/tree queries — those run entirely in the consumer."
- **Wire-format stability** — JSONL is the integration point. Five op shapes — `commit` / `upsert_item` / `delete_item` / `upsert_reference` / `delete_reference` — content-addressed by UUID5(`path + content`) so unchanged items keep the same ID across commits and the consumer's copy-on-write index handles replays idempotently. Producer-side writer: `ember-server/app/services/jsonl_changeset/writer.py`. Consumer-side schema: `ember-code/core/code_index/delta.py` (`CODEINDEX.md:64-76`).
- **Consumer storage = per-commit Neo4j** — the consumer spins up a fresh Neo4j instance per commit (BE-spawned, no Docker). This replaces the earlier chroma+sqlite local-store approach. `docs/NEO4J_MIGRATION_DONE.md` is the working migration document; the migration is described as "done and remaining" — treat the post-migration contract as the source of truth, with chroma as the legacy fallback still potentially dual-running on older sessions.

### Self-host vs. hosted

The CLI points at `api.ignite-ember.sh` by default. A self-hosted `ember-server` switches the CLI's `api_url` in `.igni/config.yaml`.

---

## 5. Differentiators vs. Competitors

### 5.1 The headline comparison (Claude Code)

`README.md:17-46` publishes a 16-row feature table; `CLAUDE_CODE_PARITY.md` does a deeper 61-row parity audit. Treating both as evidence:

| Capability | Claude Code | igni | Status |
|---|---|---|---|
| **Multi-agent teams** | Single agent loop | Dynamic team assembly (5 modes: Single/Route/Coordinate/Broadcast/Tasks) | igni-only |
| **Agent nesting depth** | One level (sub-agents can't spawn sub-agents) | Unlimited | igni broader |
| **Code intelligence** | Grep + file reads | CodeIndex semantic+metadata index + grep | igni broader |
| **Pre-built code index** | None shipped | Per-commit JSONL → local Neo4j (BE-spawned, per-commit DB, no Docker) via ember-server | igni-only |
| **Knowledge base** | None | Local Neo4j + EmberEmbedder (384d), git-shareable | igni-only |
| **Permission modes** | 6 (incl. TS-only `auto`) | 5 (CC's five; TS `auto` skipped) | ✅ parity |
| **Hook events** | 30 | 18 of 30 (12 still missing) | ⚠ partial |
| **Hook handler types** | 5 | 4 of 5 (missing `agent`) | ⚠ partial |
| **Hook `permissionDecision` envelope** | yes | yes (post-2026-06-25) | ✅ parity |
| **Bypass-resistant scoped deny** | yes | yes (`Bash(rm *)` survives bypass) | ✅ parity |
| **Managed policy tier** | `/Library/.../CLAUDE.md` | `/Library/Application Support/Ember/...` | ✅ parity |
| **Auto-memory MEMORY.md** | `~/.claude/.../MEMORY.md` | `~/.igni/.../MEMORY.md` + CC fallback | ✅ parity |
| **Cross-tool rules** | n/a (single namespace) | `.claude/*` reads when `cross_tool_support` on | igni broader |
| **Plugin LSP primitive** | yes | yes (33 tests) | ✅ parity |
| **Plugin monitor primitive** | yes (experimental) | yes (26 tests, supervisor + backoff) | ✅ parity |
| **Plugin theme primitive** | yes | **not shipped** | ❌ igni gap |
| **Surface bundle** | terminal + Anthropic desktop + claude.ai/code | Tauri desktop + VSCode + JetBrains + browser, **one React bundle** for all 4 (earlier Textual TUI removed) | ✅ parity + igni richer UI |
| **Output styles** | yes | yes (`default`/`explanatory`/`learning`) | ✅ parity |
| **Plan mode** | yes | yes (`/plan`, `enter_plan_mode(reason)`) | ✅ parity |
| **acceptEdits mode** | yes | yes (`/accept`) | ✅ parity |
| **TodoWrite tool** | yes | yes (`todo_write(todos)` atomic replace) | ✅ parity |
| **`/schedule` (cron + one-shot)** | none as a slash command; `CronCreate`/`CronList`/`CronDelete` tools exist | explicit `/schedule` slash command + `scheduler_tasks` table | igni richer |
| **`/loop` (recurring)** | yes (since v2.1.196) | yes | ✅ parity |
| **`/fork` (clone history)** | yes (since v2.1.212) | yes | ✅ parity |
| **In-session chat search** | none | SQLite-backed (BE 23 cases, FE 24 cases) | igni-only |
| **Schedule for sub-agents** | yes (fire-and-forget Task tool) | named-specialist team in pool | ⚠ different design |
| **Cost (single trial)** | $4.01 (Opus-4.7) | ~$0.05 (MiniMax-M2.7) | 1/80th, but partly a model-size artifact |
| **Mergeable rate (12-case, 5 runs)** | 31/60 (52%) | 49/60 (82%) | +18 ✅, self-published |
| **Wall time (mean)** | 1,548 s | 2,331 s | CC ~33% faster |

The honest read: **Claude Code parity is broad (33/61 ✅, 5 ⚠️, 1 ❌)**, but the *meaningful* differentiators are the producer/consumer CodeIndex split (no equivalent in CC), the multi-agent orchestration depth, the cost ratio (mostly model-driven), and the broader slash-command surface. The wall-time disadvantage is real and acknowledged.

### 5.2 The broader competitor landscape (Cursor, Aider, Continue, Cody, Cline, Windsurf)

**How the verification worked.** Each comparative claim — "Cursor ships a semantic codebase index", "Aider's repo map is precomputed", "Cline is multi-agent", etc. — was sent to **three independent sub-agent verifiers** working in parallel, each prompted to **try to refute** the claim. The verdict is the per-claim vote tally (`✓-✗` count): a claim survives only if **at least 2 of 3 verifiers agree it stands**. This is the same pattern used by adversarial-review harnesses — the bias is set against the claim being true so only well-evidenced claims get through.

In total, **25 claims** about competitive overlap were put to this test against primary sources (vendor pricing pages, the vendors' own docs, their GitHub READMEs). The aggregate result: **7 claims survived (2/3+), 18 were killed (≤1/3)**. The breakdown:

| Status | Count | What survived / didn't |
|---|---|---|
| ✅ Confirmed (2/3 or 3/3 vote) | 7 | Concrete, falsifiable, vendor-documented facts |
| ✗ Refuted (0/3 or 1/3 vote) | 18 | Claims that read plausible but failed to find a primary-source anchor |

**What survived verification (the verified facts)**

These are the comparative claims that did hold up:

- **Cline is Apache 2.0, model-agnostic across 200+ providers, ships SDK + CLI + VS Code + JetBrains + web Kanban.** License file + provider list + multi-surface claim, all verifiable on its repo.
- **Cody retrieves code context through Sourcegraph's search API.** Sourcegraph-hosted backend, well-documented.
- **Cursor's Enterprise tier ships SSO, SCIM, and audit logs.** Confirmed on `cursor.com/pricing`.
- **Claude Code Pro pricing starts at $17/month annual.** Confirmed on `claude.com/product/claude-code`.
- **Cursor's multi-surface bundle spans IDE (Cursor Desktop), CLI, terminal, iOS, Slack, GitHub (PR reviews), and Cloud Agents.** Confirmed on `cursor.com/en/features` (see also the §Corrections note below — the original adversarial-verifier negative finding on Slack/GitHub surfaces was wrong and has been reverted).

These are small, **specific, falsifiable** facts — which is exactly the kind of claim that survives an adversarial review.

**What got killed (the overclaims)**

These are the competitive claims that *sounded* right but didn't survive:

- **"Cursor offers semantic search and secure codebase indexing, directly competing with igni's CodeIndex."** Cursor doesn't publish a public per-commit graph+vector index equivalent to `ember-server`/GCS/local-Neo4j. Vote: **0-3** — unanimously refuted.
- **"Aider's repo map is a precomputed token-budgeted dependency graph persisted across sessions."** Aider's repo map is constructed per-conversation, not pre-built or persisted per-commit. Vote: **0-3** to **1-2** across claim variations.
- **"Cline is multi-agent with coordinator/specialist coordination across SDK/CLI/IDE."** Only experimental multi-agent — no orchestrator-driven team-mode equivalent to igni's Agno-based five-mode team system. Vote: **1-2**.
- **"Claude Code now offers Routines/scheduled/event-driven automation as a first-class product feature."** No "Routines" product feature at verification time. **Note:** Claude Code does ship `/loop` (since v2.1.196), `/fork` (since v2.1.212), and the `CronCreate`/`CronList`/`CronDelete` cron tools (per `https://code.claude.com/docs/en/scheduled-tasks`). So a softer claim of "broader scheduling/looping/forking parity between igni and Claude Code" stands now, even though the original Routines framing didn't. Vote on original Routines claim: **0-3**.
- **"Cursor offers defined per-seat team pricing at $40/user/mo with Bugbot, team privacy mode, and a shared internal marketplace."** Vote: **0-3** — the broader claim about a full team plan + marketplace couldn't be anchor-verified, even though $40/mo Pro pricing is real.

**What this looks like as a pattern.** The adversarial review killed roughly **3 out of 4 competitor claims** before they hit this report. The pattern is consistent: **well-marketed capabilities that look like they compete with igni on the surface turn out to be different shapes when you read the vendor docs**. Cursor does have *some* semantic-search-y capability; Cline does have *some* multi-agent capability; Aider does have *some* repo-context capability — but in each case the capability does not match what the original claim said it matched. That gap between marketing and capability is exactly what the adversarial verification surfaced.

**Implication for the comparison:**

**Implication for the comparison:**

- **Cursor**: no public per-commit semantic index analog; pricing ($40/user/mo Pro, Hobby free tier exists) is consumer-facing, not Apache 2.0. **Multi-surface bundle is broader than igni's on three axes** — Slack is a workspace surface igni doesn't ship, GitHub PR review is a workflow surface igni doesn't ship, and Cloud Agents is a background-task surface igni doesn't ship directly. igni still wins on JetBrains coverage and Apache 2.0 licensing. Without a head-to-head benchmark, comparison is structural, not empirical.
- **Aider**: persistent repo map is per-session conversation; no producer/server side; no semantic vector search; model coverage (Claude, GPT, Gemini, Grok, DeepSeek, o-series) is broader than igni's hosted default. Aider's narrow-but-deep commit-graph differs from igni's multi-dimensional CodeIndex.
- **Continue.dev**: open-source tab-complete + chat; not benchmarked.
- **Cody**: Enterprise-shaped (Sourcegraph backend); not benchmarked against igni.
- **Cline**: open-source, Apache 2.0, multi-provider; no public CodeIndex equivalent.
- **Windsurf**: not in this research scope.
- **Codex CLI**: not benchmarked; OpenAI-corporate shipping.

**The honest gap in igni's comparison story:** *igni benchmarks only Claude Code.* No third-party reproduction of the 12-case suite exists outside the repo. A stakeholder weighing igni vs. Cursor or Cline has no published data to compare on — only the structural argument that "CodeIndex gives the agent a pre-built index nobody else has."

---

## 6. Strengths and Risks

### Strengths

1. **CodeIndex producer/consumer split is unique.** Per-commit JSONL → GCS → local Neo4j (BE-spawned, per-commit DB) with copy-on-write UUID5 IDs is a real architectural choice none of the listed competitors ship. Cursor, Aider, Cline, Continue, Cody all compute their context per-session, not per-commit-persisted. Whether it materially helps depends on whether you're working on a long-lived codebase under version control where pre-computed indexes beat per-conversation retrieval — fits large production projects, less so one-off scripts.

2. **Claude Code parity is real and broad.** 33/61 ✅, with deep hook parity (the parts that matter for compliance and CI integrations), 5/5 of the meaningful permission modes, plugin primitives (LSP, monitor, agent-sandbox) with test coverage. A Claude Code refugee can transition without losing their memory bank, settings, agents, or skills.

3. **Four-surface bundle from one React tree.** Tauri desktop, VSCode, JetBrains, and browser all load `clients/web/dist` — visual + behavioral parity across all four hosts. Surface coverage beats any single competitor in the JetBrains direction (Aider has CLI only, Cline has VSCode/JetBrains but no browser). The earlier Textual TUI is no longer shipped; `clients/web` is the only host stack.

4. **Agno multi-agent model with optional depth.** The orchestrator's reasoning over the agent pool is a more sophisticated routing primitive than Claude Code's `Task` tool or Cline's experimental multi-agent. Whether the sophistication materializes as quality depends on the agent pool definitions users bring.

5. **Engineering rigor signals.** Apache 2.0 license. Test counts growing over time (1363 → 2503 → 2619 → 473 vitest cases). Real bugs surfaced and fixed by test-writing during the same period the parity footnotes are added.

6. **Enterprise hardening layers are landing.** Managed settings / managed CLAUDE.md / managed plugin scope / scoped deny / audit logger / hook permissionDecision envelope — these are the right controls for an enterprise buyer. Not at the SSO/SCIM parity level with Cursor Enterprise yet, but the structural shape is there.

### Risks / honest weaknesses

1. **The benchmark is self-published.** `docs/BENCHMARKS.md` is authored by the igni team. 12 cases × 5 runs against a single ~600-module Python target. Substring-pattern autograding. No third-party reproduction cited. Adjust the +18 ✅ gap accordingly: *the gap is real but it's not third-party verified.* The README acknowledges wall-time honestly ("Claude Code wins on wall time by ~33 %") which improves trust but doesn't close the independence gap.

2. **The 2026-06-25 / 2026-06-26 shipping spree at v1.0.0 is "feature breadth before stabilizability" by any reasonable read.** The mid-2026 work landed all the v1.0-critical scaffolding: hooks, permissions, plugin primitives, CodeIndex producer/consumer split, Group-policy, the full UI rebadge. `CLAUDE_CODE_PARITY.md` itself notes multiple in-flight bugs being caught and fixed during the same period (`spawn_team` TypeError crash for every team-card; FilePill wrote the basename not full path to clipboard; `_format_edit_diff` start_line broken for live edits until the 2026-06-28 patch). v1.0.0 reflects surface completeness, not yet battle-tested stability across all edge cases.

3. **CodeIndex without an index is a downgrade.** On a fresh project with no `ember-server` history, the agent's main advantage is missing. The "CodeIndex-first specialist agents" auto-switch only when a populated index exists. New users don't see the differentiator immediately.

4. **One explicit Claude Code gap.** Plugin theme primitive is the single ❌ in the parity table (`CLAUDE_CODE_PARITY.md:47`). Plus 5 partials in agent-allowlist semantics and the deliberate non-registration of `Read`/`Grep`/`Glob` on the main team (forces model through Bash, sometimes wrong). For teams coming from CC workflows that lean on those tools, that's a friction point.

5. **No competitor head-to-head benchmarks exist.** Public comparison data vs Cursor / Aider / Cline / Continue / Cody is zero. A stakeholder evaluating igni has only the structural argument and the single CC benchmark.

---

## 7. Evidence Appendix

Selected key citations. Line numbers refer to the files as of the snapshot read.

| Claim | Citation |
|---|---|
| Rebrand to `igni` from v0.7.0 | `README.md:88-98` |
| Multi-agent positioning vs Claude Code | `README.md:11` |
| 12-case benchmark headline | `docs/BENCHMARKS.md:1-66` |
| Cost / wall time / mergeable verdict | `docs/BENCHMARKS.md:20-32` |
| Mandatory "What already exists" preamble | `README.md:115-117` |
| Feature comparison vs Claude Code | `README.md:32-46` |
| Four-surface deployment story | `README.md:102-107` (README still mentions a Textual TUI; production reality is 4 surfaces) |
| Hook / permission / managed-policy parity scores | `CLAUDE_CODE_PARITY.md:13-66`, `## Tallies` line ~79 |
| Cross-tool compat (`cross_tool_support`) | `CLAUDE_CODE_PARITY.md` rows 17, 18, 30 |
| 12-case detailed breakdown | `docs/BENCHMARKS.md:69-87` |
| CodeIndex architecture overview | `docs/CODEINDEX.md` |
| ember-server 5-phase pipeline | `docs/CODEINDEX.md`, `ember-server/README.md:104-125` |
| JSONL wire contract | `CODEINDEX.md:64-76` |
| Tree-sitter 58 languages list | `docs/CODEINDEX.md:55-60` |
| Orchestrator + 5 team modes + unlimited nesting | `docs/ARCHITECTURE.md:1-120` |
| Tool surface (`Bash`/`Write`/`Edit`; `Read`/`Grep`/`Glob` not on main team by design) | `QUICKSTART.md:212-218`, `CLAUDE_CODE_PARITY.md` row 22 |
| Apache 2.0 license | `pyproject.toml:9`, `README.md:241` |
| Agno / Neo4j / textual dependency pins | `pyproject.toml:38-67` |
| Knowledge-base / Neo4j + EmberEmbedder (384d) | `README.md:200-209`, `docs/ARCHITECTURE.md`, `docs/NEO4J_MIGRATION_DONE.md` |
| Org-level Groups + zero-install rollout | `core/config/group_policy.py:27-119` (pack + entry schema), `core/config/group_policy.py:122+` (materialization), `core/config/settings_loader.py:246`, `core/config/merge_plan.py:250` (settings tier), `ember-server/app/api/base_v1/endpoints/portal/groups.py:33+` (group API) |
| Pricing tiers + volume discounts | `ember-server/app/api/base_v1/schema/portal/billing.py:135-200` (`PLAN_CATALOG`, `VOLUME_TIER`), `ember-server/app/services/portal/billing_service.py:60+` |
| Hosted model roster (MiniMax-M2.7 default; GLM/Kimi planned) | `README.md:46` |
| `MiniMax-M2.7` default model positioning | `README.md:46`, `pyproject.toml` (model classifier topic) |
| 13 base agents + 6 .codeindex.md variants | direct `ls agents/`; `QUICKSTART.md:178-196` |
| Permission modes (5) + eval pipeline (6) + scoped deny | `CLAUDE_CODE_PARITY.md` rows 7, 8, 9; `CODE_STANDARDS.md` |
| Output styles (`default`/`explanatory`/`learning`) | `CLAUDE_CODE_PARITY.md` row 52 |
| Plan mode (`/plan`, `enter_plan_mode(reason)`) | `CLAUDE_CODE_PARITY.md` row 50 |
| acceptEdits mode + `/accept` | `CLAUDE_CODE_PARITY.md` row 51 |
| TodoWrite (`todo_write(todos)` atomic replace) | `CLAUDE_CODE_PARITY.md` row 25 |
| `/schedule`, `/loop`, `/fork` (parity landed, parity doc lags) | `CLAUDE_CODE_PARITY.md` rows 45, 59, 41 (parity doc itself is from 2026-06-25 and predates CC's `/loop` and `/fork` — those rows now read "igni-only" but CC ships these as of v2.1.196/212), `https://code.claude.com/docs/en/commands`, `https://code.claude.com/docs/en/scheduled-tasks` |
| In-session chat search (BE 23 / FE 24 cases) | `CLAUDE_CODE_PARITY.md` row 42 |
| Hook parity (18 events, 4 handler types, 3 modes, `permissionDecision`) | `CLAUDE_CODE_PARITY.md` rows 1–6 |
| Plugin LSP primitive (33 tests) | `CLAUDE_CODE_PARITY.md` row 32 |
| Plugin monitor primitive (26 tests) | `CLAUDE_CODE_PARITY.md` row 33 |
| Plugin agent security envelope (force_isolation=worktree) | `CLAUDE_CODE_PARITY.md` row 37 |
| Managed settings / managed CLAUDE.md / managed plugin scope | `CLAUDE_CODE_PARITY.md` rows 10, 11, 35 |
| MEMORY.md 200-line / 25KB cap + cross-tool fallback | `CLAUDE_CODE_PARITY.md` row 18 |
| Provider/CLI/dev/WIF/Helm deployment story | `ember-server/README.md:178-216` |
| ember-server `producer-only` | `docs/CODEINDEX.md` final paragraph; `CODEINDEX.md:1-3` |
| Neo4j migration (in flight) | `docs/NEO4J_MIGRATION_DONE.md` |
| Self-host CLI config override | `ember-server/README.md:191-216` |
| Three-environment branch-to-env mapping | `ember-server/README.md:184-188` |
| ember-iac repo dependency for Helm charts | `ember-server/README.md:297-303` |

---

## Bottom Line

igni is a credible, well-engineered, multi-agent AI coding assistant whose **CodeIndex producer/consumer split is a genuinely novel architectural choice** in a category where most competitors compute context per-conversation. Its Claude Code parity (33/61 ✅) and cross-tool compatibility make it a reasonable migration target for CC users, and its feature breadth (4 surfaces, plugin primitives, output styles, plan/acceptEdits modes, knowledge base, scheduling/looping/forking) gives it competitive parity in dimensions that matter.

The **honest caveats** for a stakeholder weighing it:

1. Its quality claims rest on a self-published benchmark with no third-party reproduction.
2. The cost ratio is partly a function of running a smaller default model, not just the architecture.
3. v1.0.0 reflects surface completeness, not yet battle-tested stability — the 2026-06-25 / 2026-06-26 shipping spree is "feature breadth before stabilizability" by any reasonable read.
4. One explicit gap and five partials exist vs Claude Code.
5. Self-hosting requires running two repos worth of infrastructure plus a GCS bucket.
6. No head-to-head benchmark against Cursor/Aider/Cline/Continue/Cody exists, so comparison against those is structural, not empirical.

If you want a **Claude Code alternative with a pre-built code index**, that's a real differentiator nobody else ships today, and `ember-server` + igni deliver it. If you want **empirical proof that igni beats Cursor or Aider**, that proof does not exist yet.
