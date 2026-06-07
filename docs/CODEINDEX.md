# CodeIndex Integration

CodeIndex is the semantic code intelligence engine behind Ember Code. While other tools operate on raw text (grep for patterns, read for file contents), CodeIndex gives agents **pre-processed understanding** of the entire codebase — searchable by meaning, not just keywords.

**CodeIndex is included with every Ember Code plan** (Lite, Pro, Max). If you only need code intelligence without the AI coding assistant, CodeIndex is also available as a standalone plan at $10/seat. Sign up at [ignite-ember.sh](https://ignite-ember.sh).

## What CodeIndex Does

CodeIndex is a semantic search platform built on Weaviate (vector DB), PostgreSQL (metadata), and Agno readers (intelligent chunking). Ember Code uses it as the backend for code intelligence:

```
Your Codebase
    │
    ▼
┌──────────────────────────────────────────┐
│  Ember Code Analysis Pipeline            │
│                                          │
│  1. Read every file                      │
│  2. Chunk semantically (Agno readers)    │
│  3. Generate summaries per category      │
│     (code, security, testability, ...)   │
│  4. Build hierarchy bottom-up            │
│     function → class → file → module     │
│  5. Index everything in CodeIndex        │
└──────────────┬───────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────┐
│  CodeIndex                               │
│                                          │
│  Weaviate (vectors + semantic search)    │
│  PostgreSQL (references + metadata)      │
│  Redis (cache + rate limiting)           │
│                                          │
│  API: /v1/vector-query/search/run        │
└──────────────┬───────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────┐
│  Agents query CodeIndex                  │
│                                          │
│  "How does authentication work?"         │
│  → Returns auth module summary with      │
│    flow description, security analysis,  │
│    references to all related files       │
└──────────────────────────────────────────┘
```

---

## Two Layers

It's important to understand the separation:

**CodeIndex** (the platform) provides:
- Semantic chunking via Agno readers (semantic, fixed-size, document-aware, agentic)
- Vector storage and similarity search (Weaviate)
- Hierarchical document structure (parent/child, source/derived)
- Custom references between documents with tags
- Access control, multi-tenancy, per-repository scoping
- REST API for ingestion, search, and management

**Ember Code** (the intelligence layer) provides:
- The analysis pipeline that generates multi-category summaries
- Bottom-up hierarchical summary generation (function → class → file → module → project)
- Category-specific analysis (security, testability, architecture, performance, maintainability)
- The `CodeIndex` tool that agents use to query all of this
- Automatic re-indexing on code changes

Together, they give agents something no other coding assistant has: **pre-computed, semantic understanding of the entire codebase, searchable by meaning.**

---

## Multi-Category Analysis

When Ember Code indexes a codebase, it doesn't just store raw code. For each entity, it generates summaries across **six categories**:

| Category | What It Captures | Example Insight |
|---|---|---|
| **Code** | Purpose, behavior, algorithm, data flow | "JWT-based auth middleware that validates tokens from the Authorization header" |
| **Security** | Vulnerabilities, auth patterns, input validation, secrets | "Refresh token rotation not implemented — stolen tokens have unlimited lifetime" |
| **Testability** | Coverage, mockability, edge cases, testing patterns | "Complex branching in process_payment() — 4 untested edge cases around currency conversion" |
| **Architecture** | Dependencies, coupling, design patterns, boundaries | "Tight coupling between OrderService and PaymentGateway — no interface abstraction" |
| **Performance** | Bottlenecks, complexity, resource usage | "N+1 query in get_user_orders() — loads related items in a loop instead of a join" |
| **Maintainability** | Code quality, readability, tech debt | "500-line function with 12 parameters — candidate for decomposition" |

Each category is a separate summary stored as a tagged document in CodeIndex. This means agents can search within a specific category:

```
search("authentication", categories=["security"])
→ Returns only security-related findings about auth code

search("slow endpoints", categories=["performance"])
→ Returns performance bottleneck analysis for API endpoints
```

---

## Hierarchical Summaries

Summaries are built **bottom-up**. Children are analyzed first, then their summaries feed into the parent's analysis. This is the key differentiator.

```
┌─────────────────────────────────────────────────────────────┐
│  Project: my-api                                            │
│  Summary: "FastAPI REST API with JWT auth, PostgreSQL,      │
│           and Redis caching. 47 endpoints across 8 modules" │
│                                                             │
│  Security: "3 critical findings: SQL injection in search,   │
│            missing rate limiting on auth, exposed debug     │
│            endpoints in production config"                  │
│                                                             │
│  ┌───────────────────────────┐  ┌─────────────────────────┐ │
│  │  Module: src/auth/        │  │  Module: src/api/       │ │
│  │  Summary: "Authentication │  │  Summary: "REST API     │ │
│  │  module with JWT tokens,  │  │  routes for users,      │ │
│  │  session management, and  │  │  orders, and products.  │ │
│  │  OAuth2 providers"        │  │  Uses dependency        │ │
│  │                           │  │  injection for auth"    │ │
│  │  ┌─────────────────────┐  │  │                         │ │
│  │  │ File: middleware.py │  │  │  ...                    │ │
│  │  │ Summary: "Request   │  │  └─────────────────────────┘ │
│  │  │ auth middleware.    │  │                              │
│  │  │ Validates JWT from  │  │                              │
│  │  │ Authorization header│  │                              │
│  │  │                     │  │                              │
│  │  │ ┌─────────────────┐ │  │                              │
│  │  │ │ Class:          │ │  │                              │
│  │  │ │ AuthMiddleware  │ │  │                              │
│  │  │ │ ┌─────────────┐ │ │  │                              │
│  │  │ │ │ validate()  │ │ │  │                              │
│  │  │ │ │ refresh()   │ │ │  │                              │
│  │  │ │ └─────────────┘ │ │  │                              │
│  │  │ └─────────────────┘ │  │                              │
│  │  └─────────────────────┘  │                              │
│  └───────────────────────────┘                              │
└─────────────────────────────────────────────────────────────┘
```

**Why this matters:** When an agent asks about the auth module, CodeIndex doesn't return raw file contents — it returns the module-level summary that already distills knowledge from every function and class inside it. The agent gets multi-resolution understanding: zoom out for architecture, zoom in for implementation.

### How It's Stored

Each level of the hierarchy is a document in CodeIndex with parent-child relationships:

```
Document: src/auth/middleware.py
  ├── parent: src/auth/ (folder)
  ├── children: [] (files don't have children in FS)
  ├── chunks:
  │   ├── chunk[0]: AuthMiddleware class (semantic chunk)
  │   ├── chunk[1]: validate() method (semantic chunk)
  │   └── chunk[2]: refresh() method (semantic chunk)
  ├── derived_documents:
  │   ├── summary_code (code category summary)
  │   ├── summary_security (security category summary)
  │   ├── summary_testability (testability category summary)
  │   └── ...
  └── references:
      ├── → src/auth/jwt.py (imports from)
      ├── → src/api/deps.py (used by)
      └── → src/config/security.py (reads config from)
```

CodeIndex stores the hierarchy natively:
- **Parent/child** relationships via Weaviate references (folder → files)
- **Source/derived** relationships for summary documents (code file → its category summaries)
- **Custom references** with tags in PostgreSQL (cross-file dependencies, imports)
- **Ancestry tracking** via `parent_ids_hierarchy` (full path from root to entity)

---

## Search Capabilities

### Semantic Search

Query by meaning, not keywords. CodeIndex vectorizes all content and summaries using embedding models, then finds results by vector similarity.

```
Agent: search("how does payment processing handle refunds")

CodeIndex returns:
  1. src/payments/refund.py (score: 0.93)
     Code: "Refund processor that reverses charges via the
            payment gateway. Supports partial refunds, handles
            currency conversion, and creates audit records."

  2. src/payments/gateway.py (score: 0.88)
     Code: "Payment gateway adapter. The refund() method calls
            the provider's reversal API and handles idempotency
            via transaction IDs."
```

### Category-Filtered Search

Search within specific analysis categories:

```
Agent: search("authentication", categories=["security"])

Returns only security-focused findings:
  1. src/auth/middleware.py
     Security: "Token validation is solid but refresh token
               rotation is not implemented..."

  2. src/auth/jwt.py
     Security: "RS256 is appropriate. However, the 'none'
               algorithm is not explicitly rejected..."
```

### Entity Lookup

Get the full summary for a specific file, class, or function:

```
Agent: get_entity("src/auth/middleware.py")

Returns:
  Code: "JWT-based authentication middleware..."
  Security: "Refresh token rotation not implemented..."
  Testability: "Well-structured for testing — middleware is
               injected via dependency injection..."
  Architecture: "Central auth dependency — 32 files import
                from this module..."
  Performance: "JWT validation adds ~2ms per request..."
  References: [src/auth/jwt.py, src/api/deps.py, ...]
```

### Reference Traversal

Follow the dependency graph:

```
Agent: get_references("src/auth/middleware.py")

Returns:
  References to (this file depends on):
    → src/auth/jwt.py (token validation)
    → src/config/security.py (key configuration)

  Referenced by (other files depend on this):
    ← src/api/deps.py (dependency injection)
    ← src/api/routes/users.py (auth required)
    ← src/api/routes/orders.py (auth required)
    ... (28 more files)
```

### Similarity Search

Given a file, find similar ones (useful for finding duplicated logic):

```
Agent: find_similar("src/api/routes/users.py")

Returns:
  1. src/api/routes/orders.py (similarity: 0.91)
     — Same CRUD pattern, same auth middleware, same pagination
  2. src/api/routes/products.py (similarity: 0.87)
     — Similar structure but uses different validation
```

---

## Project Map

Beyond the per-entity summaries, the indexing pipeline also produces a **commit-level overview** — a single markdown digest that gives agents a factual high-level picture of the codebase *before they issue a single tool call*. Without it, an agent's first query often misfires because it hasn't yet learned the project's vocabulary; with it, the first query lands on the right area on the first try.

The map is rendered **once per commit, server-side**, and shipped to the client as the final line of the JSONL changeset (`commit_summary` op). Ember Code persists the markdown alongside the per-commit chroma directory and, when a session starts, injects it into the agent's system prompt as a `## Project Map` section.

### What's in the map

| Section | Content |
|---|---|
| **Project snapshot** | Folder / file / entity counts. |
| **Folder taxonomy** | Top-level packages with one-line role descriptions. Heavy subtrees auto-expand: a folder with no direct summary but many descendants (above the expansion threshold) still surfaces, so deep modules don't hide. |
| **Tables** | Classes detected as SQL tables — via path heuristic (`db/sql/…`) or name suffix (`*Model`, `*DB`, `*Table`) — each with its summary so the agent picks the right table without grep-walking. |
| **Migrations** | Most recent first, so schema-touching tasks anchor on current shape. |
| **Cached-resource wrappers** | Classes that own a cached client / connection / session. Flagged explicitly: *call their accessors; never construct a new client of the same type.* This is the single biggest reuse foot-gun, and surfacing it up-front prevents duplicated SDK instantiations. |
| **Category classes** | Classes grouped by concept-word suffix (`*Pool`, `*Gate`, `*Manager`, …). When a user's verb maps to a category, the agent reads every class in the group before picking a reuse target — instead of cloning the first hit it finds. |

### Why server-side

Pre-server, every developer's session rendered the map locally on first use. Moving the render to the indexing pipeline pays off three ways:

- **Single source of truth.** Every developer indexing the same commit sees the same map, identical down to wording. No drift from per-developer LLM jitter or model selection.
- **Cost amortization.** Rendered once per commit and reused for every session on every developer machine.
- **No client credentials required.** Even users without their own model API key get the full overview — the render runs on the indexing pipeline's keypool.

### Graceful degradation

If the map isn't present (older changeset, server-side render failed, no AI key on the indexing pool, etc.), the session loader silently skips the `## Project Map` section. The agent runs without it — slower to route the first query, but otherwise unaffected.

---

## The CodeIndex Tool

Agents access CodeIndex through the `CodeIndex` tool (see [Tools](TOOLS.md)):

```python
from ember_code.tools.codeindex import CodeIndexTools

codeindex = CodeIndexTools(
    project_id=config.project_id,  # authenticated via Ember Code login
)
```

**Functions:**

| Function | Description |
|---|---|
| `search(query, categories?, limit?)` | Semantic search across summaries |
| `get_entity(path)` | Full summary for a specific entity |
| `get_children(path)` | Summaries of all children of an entity |
| `get_references(path)` | Entities that reference or are referenced by this entity |
| `get_category(path, category)` | Specific category summary (e.g., security analysis) |
| `find_similar(path, limit?)` | Find entities similar to this one |

### Which Agents Get CodeIndex

| Agent | Has CodeIndex | Why |
|---|---|---|
| Explorer | yes | Primary research tool for understanding code |
| Planner | yes | Needs architectural context to design plans |
| Editor | yes | Needs to understand surrounding code before editing |
| Reviewer | yes | Needs to assess impact across the codebase |
| Git | no | Only runs git commands, doesn't reason about code |
| Conversational | no | General Q&A, no code analysis needed |

---

## Indexing Pipeline

### When Indexing Happens

| Trigger | What Gets Indexed |
|---|---|
| First onboarding | Full codebase |
| `git push` (if configured) | Changed files |
| Background (auto-refresh) | Changed files, periodic |

### Pipeline Steps

```
1. Discover files
   └── Walk project tree, respect .gitignore + config ignore patterns

2. Read & chunk (Agno readers)
   ├── Semantic chunking (default) — respects code boundaries
   ├── Section chunking — for files with [SECTION:*] markers
   └── Agentic chunking — AI-powered intelligent splitting

3. Generate category summaries (LLM)
   ├── For each entity (function, class, file, module):
   │   ├── Code summary
   │   ├── Security analysis
   │   ├── Testability assessment
   │   ├── Architecture analysis
   │   ├── Performance analysis
   │   └── Maintainability assessment
   │
   └── Bottom-up: children summarized first,
       then fed into parent summary generation

4. Extract references
   ├── Import/dependency analysis
   ├── Function call graph
   └── Type/class relationships

5. Index in CodeIndex
   ├── Documents → Weaviate (vectorized for semantic search)
   ├── Chunks → Weaviate (fine-grained search)
   ├── References → PostgreSQL (tagged, bidirectional)
   └── Metadata → PostgreSQL (line ranges, commit info)
```

### Chunking Strategies

CodeIndex supports multiple chunking strategies via Agno readers:

| Strategy | When Used | How It Works |
|---|---|---|
| **Semantic** (default) | Most code files | Respects semantic boundaries (functions, classes, blocks) with ~10% overlap between chunks |
| **Fixed-size** | Very large files | Fixed character/token limits per chunk |
| **Document-aware** | Structured files (markdown, notebooks) | Respects document structure (sections, cells) |
| **Agentic** | Complex files | AI-powered chunking that understands code context |
| **Section** | Files with `[SECTION:*]` tags | Custom markers define chunk boundaries |

---

## Graceful Degradation

If CodeIndex is unavailable (no account, offline, API error), Ember Code still works. The Orchestrator is aware of CodeIndex availability and adjusts:

| CodeIndex Status | Agent Behavior |
|---|---|
| **Available** | Agents use CodeIndex for semantic search, summaries, references |
| **Unavailable** | Agents fall back to Grep, Glob, Read for code exploration |
| **Partially indexed** | Agents use CodeIndex where available, fall back for unindexed files |

The experience degrades gracefully — agents are slower and less informed without CodeIndex, but they still function. The Orchestrator may add extra Explorer agents to compensate with broader file reads.

---

## Configuration

CodeIndex works out of the box with zero configuration. When you sign up at [ignite-ember.sh](https://ignite-ember.sh), CodeIndex is included automatically — no API keys, no config files needed. Your Ember Code login is all that's required.

> **Coming soon:** Per-project configuration for CodeIndex (custom categories, indexing options, ignore patterns, chunking strategies, self-hosting) is planned for a future release. Today, CodeIndex uses sensible defaults for all projects.


---

## Self-Hosting (Planned)

For teams that need to keep code on-premises, self-hosted CodeIndex is planned for a future release. The architecture will include Weaviate (vector DB), PostgreSQL (metadata), Redis (cache), and a FastAPI server.

---

## Why Not Just Use Grep?

| Question | Grep | CodeIndex |
|---|---|---|
| "How does auth work?" | Finds files with "auth" in the name/content | Returns auth module summary with flow description, security analysis, and references |
| "What's vulnerable?" | Can't answer | Returns security-category summaries flagging issues across the codebase |
| "Where should I add rate limiting?" | Can't answer | Returns architecture summaries of request-handling modules with dependency analysis |
| "What needs more tests?" | Can't answer | Returns testability-category summaries ranking under-tested areas |
| "Find code similar to this function" | Can't answer | Vector similarity search finds structurally similar code |
| "What depends on this module?" | Fragile regex on imports | Returns complete reference graph with tags |

Grep finds strings. CodeIndex finds meaning.
