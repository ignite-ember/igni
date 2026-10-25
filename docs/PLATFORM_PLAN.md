# Platform Plan

What has to be built to run igni as an **agentic platform** in two shapes, and in what order.

> Companion to [EXTENSIBILITY.md](EXTENSIBILITY.md), which specifies the extension contract. This document is sequencing and scope. Dated 2026-10-22.

## The two shapes

| | **Single-user backend** | **Shared backend** |
|---|---|---|
| Serves | one person, on their machine | many principals and applications |
| Coding work | yes — this is where it lives | no; documents, data, connected systems |
| Extensions | sidecar (CodeIndex) + remote | remote (Artifacts) |
| State | SQLite | Postgres |
| Isolation | the user's own machine | process per group + hardened container per session |
| Auth | loopback | required, through OIDC/SAML |

A "user" is a **principal** — a person *or* an application. Every request carries a principal (authorization, group) and an app identity (attribution, narrower scope).

---

## Phase 0 — Bugs that ship today

Independent of direction. Days, not weeks.

| | Where | Why now |
|---|---|---|
| **Per-runtime `load_settings`** | `backend/session_orchestrator.py:453` deep-copies the *boot* project's Settings | A session opened on repo B runs with repo A's `permissions.allow`/`deny`, guardrails and model default. Tauri "New Window" uses this path. Cross-project privilege escalation in the shipped product. |
| **Neo4j: don't kill what you don't own** | `igni_codeindex/local/runtime/neo4j.py` (CodeIndex extension) — line numbers from igni's `backend/neo4j_runtime.py:1288`, `:833` | An attached backend SIGTERM→SIGKILLs another's sidecar; `stop_all()` ignores refcounts by design. SIGKILL mid-checkpoint forces a full re-embed. |
| **Neo4j: stale pidfile** | `igni_codeindex/local/runtime/neo4j.py` (CodeIndex extension) — line numbers from igni's `backend/neo4j_runtime.py:1156` | Kill the JVM externally and the next boot silently runs with **no artifact store**, visible only in the log. |
| **Neo4j: orphans and partials** | — | 36 stray JVMs (some 10 days old, `-Xmx2g`), 481 state dirs, 248 MB of abandoned `tmp*.tar.gz` on a developer laptop. Nothing reaps any of it. |
| **`WorkflowRunner` missing on pool runtimes** | `backend/app.py:311` vs `rpc_handlers/workflows.py:33` | `/workflows` fails silently on every non-default session. |

---

## Phase 1 — Extensions

Contract in [EXTENSIBILITY.md](EXTENSIBILITY.md). Order matters; step 3 is the go/no-go.

1. **Backend static origin**, one loopback listener per extension. Smallest change, proves the path with no extension involved. Not `/ext/<id>/` under one port: a path prefix does not separate origins, so every extension would share storage and could reach into the next one's frame.
2. **CSP `frame-src` + bridge** in the web host and Tauri. Only the calls `CodeIndexPanel` needs: rpc, navigate, identity, params, theme.
3. **Port `CodeIndexPanel` behind the bridge, unchanged.** If a two-prop panel hosts without modification, the contract is real. If not, we learn it in days.
4. **Contribution points** replacing the closed unions — `PageKind` (`chat/pageStack.ts:28`), `SIDEBAR_LINKS` / `TOOLS_MENU` (`App.tsx:143`).
5. **Prompt contributions**: generalise `{{CODEINDEX_GRAPH_SCHEMA}}` (`core/prompts/__init__.py:20`) and the `.codeindex.md` variant suffix (`core/agents/loader.py:151`).
6. **Manifest, validation, signature verification.** Now igni server's distribution: the service signs its bundle with its registered key, and the host verifies it against that key before installing (`EXTENSIBILITY.md`, "Shipping one"). No trust store.
7. **Remote extension registration** so any backend can attach a remote extension (Artifacts) regardless of where it runs.

---

## Phase 2 — Group tier correctness

Shared by both shapes.

- **Key the policy cache by `group_id`.** Today one tree per data dir (`core/config/group_policy.py:318`), pruned against whichever pack polled last. It materialises `tools/*.py` and executable `scripts/*.sh`, so two groups in one data dir is cross-tenant code execution.
- **A local deployment holds exactly one pack: the principal's.** The server decides what a deployment may hold; a laptop asking for another group's pack is refused.
- **Group `default_model` reaches model resolution.** `pack.default_model` is display-only today; the chain is `definition.model` → `settings.models.default` (`core/agents/builder.py:146`). Fold the group default into the settings tier.
- **Adapter passthrough.** `ModelRegistry.get_model()` requires registered entries; LoRA adapter names must resolve to the same `base_url` with a different model string, and fail legibly when the endpoint doesn't serve them.
- **Record `group_id` + pack ETag in every session trace.** Groups legitimately produce different work; without this, "why did it behave differently?" is unanswerable.

---

## Phase 3 — Shared backend foundations

Nothing here is optional, and auth is first.

- **Authenticate at attach**, through OIDC/SAML rather than a parallel login. Bind principal + app identity to the connection.
- **Session ownership.** A session is owned by the principal that created it; an inbound `session_id` not owned by the connection's principal is rejected. This closes an approval-hijack path: inbound routes on `session_id` alone today.
- **Subscription-filtered delivery.** `WebSocketServerTransport.send()` fans every payload to every connection, stamped, filtered client-side. Two principals on one backend see each other's work. Authorize by principal, deliver by subscription.
- **TLS**, since it stops being loopback.
- **Principal-aware router.** Not a cookie-sticky LB: authenticate, resolve principal → group → that group's pool, place **least-loaded**, then pin. Sticky placement is permanent, so a bad placement is permanent.
- **Process per group, separate HOME/OS user.** The only isolation lever that works today — `data_dir` is bypassed by ~21 hardcoded `Path.home()` call sites.
- **Neo4j bolt auth on.** `dbms.security.auth_enabled=false` (`igni_codeindex/local/runtime/neo4j.py` (CodeIndex extension) — line numbers from igni's `backend/neo4j_runtime.py:1052`) — a password is generated and passed to the driver, and the server ignores it. Every sidecar is an unauthenticated bolt port on loopback.
- **Deployment profile → Postgres** (see EXTENSIBILITY §3). The blocker is the four raw-`sqlite3` stores in `core/session/`; `session_directories` and `client_state` are exactly what a router needs.

### Scale-out blockers

Correctness bugs the moment a second instance exists:

- **Session registry needs an owner.** `session_directories` maps session → dir; it must map session → dir → **owning instance**, in shared storage.
- **Scheduler has no lease.** `get_due_tasks()` selects, then marks running in a separate statement — N instances all fire the same task.
- **`background_processes` keys on PID with no owner column.** The orphan rehydrator adopts any live PID as "a prior BE lifetime" and offers to kill it.
- **Concurrent `alembic upgrade head`** is guarded by a `threading.Lock` and an in-process set. Postgres removes this.

---

## Phase 4 — Execution and scheduling

- **Hardened rootless container per session**: user namespaces, caps dropped, seccomp, read-only root, no docker socket, network policy. Proportionate for same-org groups; microVM only for classified or cross-organisation.
- **Images ship in the bundle** — no runtime pulls, air-gapped. **Don't assume Kubernetes**; podman/systemd must work.
- **The sandbox image is the customer's**, declared per group, like a CI runner — a generic image won't have their toolchain.
- **Execution backend is an interface**: direct locally, container in shared. Otherwise two execution paths and bugs in only one.
- **Locally, containers are optional.** The real protection against destructive commands is the permission pipeline, the deny list, the safety check and git — not a container you've mounted the toolchain into anyway.
- **Two priority classes**: interactive preempts batch. Biggest perceived win for the least work.
- **Per-principal concurrency quota** (caps are per session today) and an **admission queue** with a visible position, rather than silent slowdown.

---

## Phase 5 — Operability and lifecycle

The SI runs this, not us. `partner-enablement-gaps.md` names several of these as blockers to signing.

- **Sanitised diagnostic bundle** — versions, extension states with reasons, group id and pack ETag, config with secrets stripped, recent errors. No customer content. L2 cannot escalate anything without it.
- **Health endpoint.** There is a ready line on stdout for a spawning parent and nothing an operator or LB can poll.
- **Structured logs to stdout.** One rotating file (`~/.igni/logs/backend.log`) is multi-process-unsafe — processes keep appending to a renamed inode.
- **Metrics**: queue depth and model-endpoint wait time. That is the signal that tells a customer to add a GPU; we do not need per-principal token accounting.
- **Drain on upgrade**, window **configurable by the enterprise** via group policy. Blue/green behind the router: new sessions to the new process, old one drains to the cap. An interrupted run stays visible and resendable — never a bare "connection lost".
- **Retention setting + one reaper over every store** — session rows, traces, scratch, logs, artifacts — reporting what it deleted. A policy that prunes conversations and leaves traces reads as compliance and isn't. Infrastructure (encryption, backups, keys) is the SI's; **data lifecycle is ours**.
- **Data minimisation** as the design default, so retention has less to clean.
- **Distinct principals per paid extension, per period** — a set, not a meter. D11 caps named users per band and the true-up number has to come from somewhere.

---

## Phase 6 — Batch

Small once Phase 3 exists.

- **Dispatch to ephemeral workers**, no affinity — the LiveKit shape. Interactive is sticky; batch is dispatched.
- **Act as the requesting principal** into the customer's systems via MCP — token passthrough, never a shared service account that can reach more than the requester.
- **Fail closed on anything that would prompt.** Approvals assume a human is attached; a batch job must run with a pre-agreed permission set, not park forever on an `ASK` nobody will see. Approve-all is only defensible with a complete audit trail.
- **Idempotency keys**, so a client that times out and retries doesn't run the work twice.
- **Scratch storage with a TTL**; anything durable becomes an Artifact.

---

## Decisions already taken

| | |
|---|---|
| IDE plugins | Frozen at context-handoff scope (business-ops **D16**). Extensions render in the igni app only. |
| Coding | A single-user-backend concern. The shared backend works on documents, data and connected systems. |
| Extensions | One concept, two packagings — sidecar or remote. UI is an optional contribution. |
| Tools | Always MCP. No second tool transport. |
| Serving | Not ours. igni is a client of an OpenAI-compatible endpoint; adapters are model names. |
| Warm pools | No. Boot is 1.44 s and 92% of it is Python import; everything else is project- and user-specific. |
| Mobile | No. The prerequisite is moving sessions off the developer's machine, which is its own decision. |

## Open questions

- Does a service-account principal count as a platform user for banding? Pricing decision, not technical.
- Retention defaults — what does an unconfigured deployment do?
- Does the shared backend's agent need a shell at all, or only tools? Changes how much sandbox is needed.
- `project_id` hashes the git remote, not the path (`core/data_paths.py`, and the CodeIndex extension's copy) — two checkouts of one repo are not isolated. Latent until HOMEs are ever consolidated.

## Not being built

Per-token billing, a public marketplace, countersigning partner extensions, multi-tenancy inside one backend process, an IDE-hosted extension surface, and a second tool transport.
