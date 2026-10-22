# Extensibility Floor

How CodeIndex, Artifacts and third-party capabilities attach to igni — and what igni promises not to break underneath them.

> **Status: proposed.** Nothing below is implemented yet. [Milestones](#milestones) says what exists today.
>
> **"Remote" always means inside the customer's own network.** igni hosts nothing. A laptop talking to an on-prem Artifacts service is still fully air-gapped.

## One concept, two packagings

An **extension** is a capability with a service behind it. It arrives one of two ways, and nothing else about it changes.

| | **Sidecar** | **Remote** |
|---|---|---|
| Arrives as | a signed image, installed and supervised by us | a registered endpoint someone else runs |
| Tools reach the agent via | MCP over stdio | MCP over SSE/HTTP |
| Infrastructure | ours to resolve and supervise (a graph DB on the laptop) | behind their URL — scaling, HA, failover are their problem |
| Installed by | an administrator, from the image | an administrator, by registering a URL |
| Licence enforced by | withholding the image | the endpoint itself |
| Blast radius | one user; single instance | whatever their service does about it |
| Example | **CodeIndex** — repo-coupled, cannot be remote | **Artifacts** — a service with a URL |

**Any backend can attach either kind.** A single-user local backend can register a remote extension; a shared backend serving everyone can too. CodeIndex is sidecar-only because it is bound to a checkout, not because of where the backend runs.

**Tools are always MCP.** We do not invent a second tool transport. What an extension needs *beyond* tools is what this document specifies.

**One extension, one manifest.** Packaging decides which fields apply — `requires` and an image for a sidecar, `endpoint` and auth for a remote one. Everything else is identical, so a capability is never described twice and cannot drift between deployments.

---

## 1. What an extension contributes beyond tools

This is the whole justification for a contract. MCP carries tools. It does not carry any of the following, and CodeIndex needs all of them.

| Contribution | What it does | Why MCP can't |
|---|---|---|
| `promptFragments` | text injected at a named anchor in a system prompt | replaces the hardcoded `{{CODEINDEX_GRAPH_SCHEMA}}` placeholder |
| `promptVariants` | which agents switch to a variant while this extension is `READY` | generalises the `.codeindex.md` suffix |
| `health` | `READY` / `PREPARING` / `DISABLED` / `FAILED`, **always with a reason and a fix** | MCP reports connectivity, not "licence expired, install the renewed key" |
| `settings` | a config-schema fragment the host renders | — |
| `commands` | slash commands | — |
| `pages`, `indicators`, `chatRenderers` | **optional** UI — a surface, a status chip, inline result rendering | MCP has no UI concept |

UI is a contribution some extensions have, not what makes something an extension. A headless extension is normal.

Agents, skills and hooks are **not** here: plugins and group packs already carry those. Don't build a second path.

Everything is namespaced by extension id. A collision with a base name is a validation error at install, not a surprise at runtime.

### Not in v1

Keybindings, context menus, custom editors, theme overrides, i18n. Additive changes are never breaking, so omitting is cheap and premature inclusion is forever.

---

## 2. The manifest

```jsonc
{
  "id": "igni.codeindex",             // reverse-DNS, immutable, namespaces everything
  "version": "1.4.0",
  "contract": ">=1.0 <2",             // the only compatibility axis

  "signature": "…",                   // detached; verified against a key this deployment trusts
  "trust": "first-party",             // first-party | partner | unsigned (dev only)

  // ── one of these two, by packaging ──
  "sidecar": {
    "mcp": { "transport": "stdio" },
    "requires": [                     // infra WE resolve, because there is no vendor service
      { "capability": "graph-db", "constraint": ">=5", "mode": "shared" }
    ]
  },
  "endpoint": {                       // the remote form
    "url": "https://artifacts.internal/mcp",
    "auth": "…"
  },

  "permissions": { "workspace": "read", "egress": [], "secrets": [] },

  "contributes": { /* §1 */ }
}
```

**Installed once.** One `id`, one install. No second copy, no per-project instance. Everything the extension needs to do, it does — two indexes is configuration inside the extension, not two installs.

**One version axis.** An extension declares a `contract` range and never names a product version. The deployment advertises one contract version, the lower of what the backend and the installed client implement.

**Sidecar requirements resolve offline.** Declare capability *kinds* (`graph-db`, `vector-store`, `relational-db`) — never a product name; a regulated customer often has a sanctioned Postgres you must use. The resolver binds to a running instance or provisions one from the bundle. Nothing is fetched at resolve time. Unsatisfiable requirements fail **at install**, naming the fix.

**A sidecar is a single instance, and that is the trade.** If it dies, one user is blocked. That is the correct blast radius for a laptop, and the reason none of a remote service's HA machinery belongs here.

**A remote extension is registered, not installed.** Its availability, scaling and failover are its own. We check that the endpoint answers, that the contract range matches, and that this principal's group is entitled to it.

---

## 3. The deployment profile

Packaging is one axis; how many people a backend serves is another. A **single-user backend** and a **shared backend** differ in what igni's own state sits on, and a **profile** names that once rather than letting each store decide for itself.

| | Single-user backend | Shared backend |
|---|---|---|
| Session / scheduler / loop / memory state | SQLite (`state.db`) | **Postgres** |
| Migrations | alembic, per process | alembic, one migrator |
| Concurrency | one process, WAL | connection pool |
| Extensions | usually sidecar, may register remote ones | usually remote; a sidecar would be shared by everyone |

Most of it is already portable: every SQLAlchemy-backed store — scheduler, loop, agno sessions and memory, background processes, code-index models — goes through one URL builder and alembic.

Two things have to change before a profile means anything.

**The URL builder is SQLite-only.** `core/db/urls.py` takes a *path* and formats `sqlite:///` / `sqlite+aiosqlite:///`. That is the single choke point: it becomes "profile → URL" instead of "path → SQLite URL". Small, and everything downstream follows.

**Four stores bypass SQLAlchemy entirely** — raw `sqlite3`, with WAL and `busy_timeout` set by hand in `core/session/_sqlite_utils.py`:

- `session_preferences.py`
- `session_directories.py`
- `client_state.py`
- `pending_messages.py`

These are the real work. They need porting to the same seam, or an explicit per-profile backend. Note that `session_directories` and `client_state` are precisely the stores a shared multi-user backend leans on hardest.

Two problems Postgres removes for free, both found in the multi-process audit:

- **Concurrent `alembic upgrade head`.** Guarded today by a `threading.Lock` and an in-process set — useless across processes, so two backends starting together can both migrate one file.
- **`database is locked` under load.** The SQLAlchemy engines set no WAL and no `busy_timeout`; they inherit WAL only by luck, when one of the four raw stores happens to touch the file first.

A profile is also where the air-gap answer lives: Postgres is a customer-run instance inside their network, declared like any other infrastructure, never anything we host.

---

## 4. Groups decide who gets what

Entitlement and configuration are group-level in both deployments; capability existence is deployment-level.

| Layer | Decides | User override? |
|---|---|---|
| **Deployment** | which images are installed / which endpoints exist, trust store, contract version | no |
| **Group** | entitlement, configuration, permission narrowing, which contributions appear, branding | **no** |
| **User** | preferences inside what the group allows | yes, within bounds |

- **A group may narrow, never widen.** Deployment grants are a ceiling; a pack asking for more is a validation error.
- **Hiding is not disabling.** `contributes.hide` removes a surface while the capability still runs; `enabled: false` turns it off.
- **One pack per session** — group membership resolves server-side, so there is no client-side merge to arbitrate.
- **Local deployments hold exactly one pack: the principal's.** The cache materialises plain files, including `tools/*.py` and executable `scripts/*.sh`, so a pack a user is not entitled to is a disclosure. The server decides what a deployment may hold; a laptop asking for another group's pack gets a refusal.
- **On a shared BE the policy cache must be keyed by `group_id`.** It is not today — one tree per data dir, pruned against whichever pack polled last.
- **Record `group_id` and the pack ETag in every session trace.** Different groups legitimately produce different work; without this, "why did it behave differently?" is unanswerable.

Entitlement can change mid-session (packs poll on a short TTL). A revocation moves the capability to `DISABLED` with a policy reason — not `FAILED` — closes open surfaces with that reason shown, and cancels in-flight calls.

---

## 5. The UI surface

Optional, and packaging-independent: the client is the same either way. The bundle is served by whichever backend the client is talking to — from the sidecar, or proxied from the registered endpoint. A headless extension contributes none of this.

- **Served** at `/ext/<id>/` by the backend. One origin means one place to enforce entitlement and the contract check before a byte is served.
- **Framed** with `sandbox="allow-scripts allow-forms"` and **without** `allow-same-origin` — opaque origin, no host storage, no top-navigation, postMessage only.
- **Per-extension CSP**, no `connect-src` beyond the bridge. The bundle cannot open its own socket, which is what makes egress auditable.
- **Bridge API**: `rpc`, `subscribe`, `identity`, `params`/`onParams`, `navigate`, `setTitle`/`setStatus`/`notify`, `theme`, `storage` — all namespaced, budgeted, cancellable. Origin and per-session nonce checked on every message.

**A capability UI never holds a credential and never opens a socket.** Every privileged call goes through the backend, where credentials already live.

**The dividing rule:** the iframe owns pages; json-render owns anything inline in the chat stream. Per-message iframes wreck scrolling, selection and performance.

Per business-ops **D16**, capability UI renders in the igni app only — never inside the VS Code or JetBrains host.

---

## 6. Security

The threat model is not a hostile stranger on a marketplace. It is **a trusted author's capability, built under deadline, inside a regulated estate, driving an agent that executes what it is told.**

### Trust

| Tier | Signed by | Trusted because | Installed by |
|---|---|---|---|
| first-party | us | our key is compiled into the base | ships with the platform |
| partner | the SI, or the customer for their own code | the admin added that key to the trust store | the admin |
| unsigned | nobody | — | `--dev` only, persistent banner, never production |

**We do not sign, review or countersign anyone else's capability.** The base verifies it was signed by a key *this deployment* trusts — nothing more. Verification is local against a detached signature, no network, so it survives the air-gap.

**Only an administrator installs or registers**, and only an administrator adds a trusted key. No user install, no marketplace fetch, no install-on-demand: software inventory is an accreditation control.

### Permissions

Declared, admin-granted, base-enforced, deny by default: `workspace` (none/read/write, path-scoped), `egress` (explicit hosts, empty by default), `secrets` (named grants resolved by a broker — a handle, never a value), `tools` (names it may register).

A capability that asks for more on upgrade **does not get it silently**; it stays at its old grants until an admin accepts. Permission drift is how extension ecosystems rot.

### The agent is an attack surface

Tool descriptions, prompt fragments and tool results all land in a model's context, so a careless capability can steer the agent rather than merely misbehave.

- **Supplied text is data, never instructions** — delimited and origin-labelled on the way in, like any untrusted file content.
- **No modifying the system prompt**, no altering another capability's tool definitions, no contributing to agents it does not own.
- **Fragments are length-capped and token-budgeted**; over-cap truncates and reports rather than crowding out the user.
- **Tools obey the permission pipeline and the hooks chain.** `BYPASS_PERMISSIONS` is unreachable from a capability.
- **Guardrails apply** on the same path as everything else.
- A result that *looks* like an instruction is still a result. Stated so a reviewer can test it.

### Egress

The air-gap claim is defended by a test (`tests/test_nothing_phones_home.py`). The contract extends it: every host declared, empty by default, UI egress structurally zero, and each capability service carries its own equivalent of that test. Undeclared egress is a contract violation, not a bug — it is the one failure that costs a customer their accreditation.

### Audit

Install, registration, upgrade, permission grant, tool invocation and bridge call — all auditable with capability id, version, principal, app identity and `group_id`.

---

## 7. Failure is normal

| If this fails | Then | Never |
|---|---|---|
| sidecar / endpoint down | `FAILED` + reason + fix; agent told it is unavailable | session crash, agent hang |
| call times out | deadline fires, cancelled, degraded once per session | silent retry storm |
| UI throws | its own error boundary | blank page, broken shell |
| manifest / entry invalid | refused at install, every error named at once | partial load |
| contract out of range | refused, naming the version and which half is behind | "works mostly" |
| licence expired | `DISABLED` with the renewal fix | hard stop on a *paying* customer mid-contract |
| id collides with a base name | install error naming both | last-writer-wins |

Plus: validate at install not at use; supervise with backoff and open a circuit after repeated failure; atomic reversible upgrades; unknown fields ignored in both directions with a report of what was dropped.

Every call into a capability carries a **deadline and a cancellation token** — the agent loop is never blocked — and a concurrency, result-size and token budget.

---

## 8. The floor itself

- **The contract is versioned independently of the product**, semver, declared as a range.
- **Breaking** = removing or renaming a contribution, field or bridge call; changing its meaning; tightening validation on something previously accepted; changing isolation so existing UI stops working.
- **Deprecation window: two minor releases or six months, whichever is longer** — set for a customer who upgrades twice a year after a change-advisory board.
- **Additive changes are never breaking.**
- **A mismatch fails loudly and legibly**, naming the version needed.
- **Installed capabilities and versions appear in the diagnostic bundle**, so L2 escalation starts from versions, not customer content.

## Not in the floor

- Importing anything from `ember_code.*` — the contributions are the interface, not the package.
- The React component tree, CSS class names, any token not published as a theme variable.
- `RpcMethod` members not namespaced to a capability.
- Database schemas, Neo4j labels, on-disk layouts.
- Base prompt text and base agent definitions.
- **Anything not named in this document.**

## 9. Developing one

- **`igni ext init`** scaffolds something that runs on first launch; the reference extension lives in-tree.
- **Dev mode** runs from localhost, unsigned, with a persistent banner and hot reload. The only place `trust: unsigned` is accepted.
- **`igni ext doctor`** checks manifest schema, contract range, permissions, collisions, deprecations, the MCP round-trip and the phones-home scan. **If doctor passes, it loads** — that equivalence is the promise.
- **Errors carry fixes**, because the reader is often a partner's L2 engineer with no internet.
- **Observability**: logs in the Watcher panel, state in the subsystem view, versions in the diagnostic bundle.
- **A changelog for the contract itself.**

---

## Milestones

| Step | State |
|---|---|
| Subsystem status (`READY`/`PREPARING`/`DISABLED`/`FAILED` + reason + fix) | **shipped** |
| Permission pipeline + hooks chain | **shipped** |
| Guardrails (PII / injection / moderation) | **shipped** |
| MCP client, stdio + SSE | **shipped** |
| Group policy packs | **shipped** |
| json-render catalog + action round-trip | **shipped** |
| Phones-home test | **shipped** |
| Capability services as separate images | **shipped** (`igni__code-index`, `igni__artifacts`) |
| Prompt fragments / variants as contributions | not started |
| Manifest + validation + signature | not started |
| Backend static origin + sandboxed iframe + bridge | not started |
| Policy cache keyed by `group_id` | not started |
| Remote extension registration in group packs | not started |
| Requirement resolver (local only) | not started |
| Deployment profile: URL builder + the four raw-sqlite3 stores | not started |
| `igni ext init` / `doctor` | not started |

**First consumer is CodeIndex**, ported while still in-tree. `CodeIndexPanel` takes two props; if the bridge hosts it unchanged, the contract is real.

## Reference

- Closed unions to open: [`clients/web/src/chat/pageStack.ts`](../clients/web/src/chat/pageStack.ts), [`clients/web/src/App.tsx`](../clients/web/src/App.tsx)
- Subsystem contract: [`backend/subsystem_status.py`](../src/ember_code/backend/subsystem_status.py)
- Permissions: [`core/config/permission_eval/`](../src/ember_code/core/config/permission_eval), [`core/config/tool_permissions/`](../src/ember_code/core/config/tool_permissions)
- Group tier: [`core/config/group_policy.py`](../src/ember_code/core/config/group_policy.py)
- Store seams: [`core/db/urls.py`](../src/ember_code/core/db/urls.py), [`core/db/engine_registry.py`](../src/ember_code/core/db/engine_registry.py), [`core/session/_sqlite_utils.py`](../src/ember_code/core/session/_sqlite_utils.py)
- MCP: [`core/mcp/config.py`](../src/ember_code/core/mcp/config.py), [`docs/MCP.md`](MCP.md)
- Prompt placeholder to generalise: [`core/prompts/__init__.py`](../src/ember_code/core/prompts/__init__.py), [`core/agents/loader.py`](../src/ember_code/core/agents/loader.py)
- Air-gap enforcement: [`tests/test_nothing_phones_home.py`](../tests/test_nothing_phones_home.py)
- Commercial framing: `igni__business-ops/strategy/decisions.md` (D1, D3, D4, D7, D11, D16), `strategy/license-enforcement.md`
