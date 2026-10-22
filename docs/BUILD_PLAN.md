# Build Plan

The executable layer under [PLATFORM_PLAN.md](PLATFORM_PLAN.md). That document says *what* and *in what order*; this one says *where in the tree*, *what we decided so we stop re-deciding*, and *how we know an item is done*. Dated 2026-10-22.

Work splits into three lanes. **Track 0** is harm that ships today and blocks nothing — it just has to stop. **Track A** (extensions) and **Track B** (shared backend) are independent after the spine, and both depend on the spine.

```
Track 0  ──┐
Spine      ├── Track A  extensions      (single-user backend)
           └── Track B  shared backend  (cloud)
```

---

## Decisions taken now

Recorded so the first hour of each item isn't spent re-opening them.

| | Decision | Why |
|---|---|---|
| **Extension origin** | One loopback HTTP listener **per extension**, port 0 | Port is part of the origin, so two extensions cannot read each other's storage or reach each other's frame. A shared `/ext/<id>/` path under the WS port is one origin for all of them. Cost is one listener per installed extension; we expect single digits. |
| **The bridge shape** | The in-frame bridge implements the **existing `IgniClient` interface** over `postMessage` | `CodeIndexPanel` takes `{client, onClose}`. If the bridge presents the same object, the panel ports with zero diff — which is the go/no-go in A3. |
| **Allow-list enforcement** | Manifest `permissions` checked **host-side and again in the backend** | The frame is the untrusted party. A host-side check alone is a suggestion. |
| **Extension states** | Reuse `SubsystemRegistry` — READY / PREPARING / DISABLED / FAILED, reason+fix mandatory on FAILED | No second vocabulary for "this thing isn't working". |
| **`PageKind`** | Becomes `string`, validated against a runtime registry | A closed union cannot admit a contribution. Validation moves from the compiler to registration, which is where it has to be anyway. |
| **Deployment profile** | A `Settings` field, not an environment sniff | `local` → SQLite, loopback, sidecars allowed. `shared` → Postgres, auth required, remote extensions only. One switch an operator sets and we can test both. |
| **Postgres** | Store **interfaces first**, drivers second | The four raw-`sqlite3` stores in `core/session/` are the blocker. Interface extraction is reviewable on its own and ships with no behaviour change. |
| **Auth** | At attach, before the first protocol frame is read | Anything later means an unauthenticated frame has already been dispatched. |
| **Delivery** | Subscription per connection; `send()` stops being a fan-out | Authorize by principal, deliver by subscription. |

---

## Track 0 — stop the bleeding

No dependencies. Roughly a week, done in any order.

| # | Item | Where | Done when |
|---|---|---|---|
| 0.1 | Per-runtime `load_settings` | `backend/session_orchestrator.py:453` — it deep-copies the **boot** project's Settings | A session uses its own project's permissions: open a session on repo B from a backend booted on repo A, and B's `permissions.deny` applies while A's does not. |
| 0.2 | Don't kill a sidecar you don't own | `backend/neo4j_runtime.py:1288`, `:833` | A second attached backend shutting down leaves the first one's JVM alive; `stop_all()` honours the refcount. |
| 0.3 | Stale pidfile is loud | `backend/neo4j_runtime.py:1156` | Killing the JVM externally and rebooting surfaces a FAILED subsystem with a reason and a fix, not a silent no-artifact-store. |
| 0.4 | Reap orphans | `backend/neo4j_runtime.py` + a boot-time sweep | Boot removes state dirs with no live PID and `tmp*.tar.gz` older than a day, and logs the count. Measured against the current 36 JVMs / 481 dirs / 248 MB. |
| 0.5 | `WorkflowRunner` on pool runtimes | `backend/app.py:311` vs `rpc_handlers/workflows.py:33` | `/workflows` works on a second session, not only the default one. |
| 0.6 | Neo4j bolt auth on | `backend/neo4j_runtime.py:1052` — `dbms.security.auth_enabled=false` | The generated password is required. Connecting without it fails. |

0.6 sits in Track 0 rather than Track B because it is an unauthenticated port on the loopback interface of every developer machine today.

---

## Spine — identity and the group tier

Both tracks read this. ~1.5 weeks.

| # | Item | Where | Done when |
|---|---|---|---|
| S.1 | Four identity keys as types | new `core/identity/` — principal, app identity, session, workspace lease | Every RPC entry point carries a principal. In `local` the loopback principal is synthesised; nothing downstream knows the difference. |
| S.2 | Policy cache keyed by `group_id` | `core/config/group_policy.py:318` | Two packs in one data dir materialise to separate trees. Today one prunes the other, and the tree holds executable `scripts/*.sh` — so today it is cross-tenant code execution. |
| S.3 | A local deployment holds exactly one pack | `core/config/group_policy.py` fetch path | A laptop requesting a group its principal isn't in is refused by the server **and** declines to materialise. (Your standing constraint: never download a group the user has no access to.) |
| S.4 | Group `default_model` reaches resolution | `core/agents/builder.py:146` — chain is `definition.model` → `settings.models.default` | `pack.default_model` is a settings tier, not a display string. |
| S.5 | Adapter passthrough | `core/config/model_registry*`, `provider_builders.py` | A group or agent naming a LoRA adapter resolves to the same `base_url` with a different model id, and fails legibly when the endpoint doesn't serve it. |
| S.6 | `group_id` + pack ETag in every trace | session trace writer | "Why did it behave differently for them?" is answerable. |

---

## Track A — extensions

Contract in [EXTENSIBILITY.md](EXTENSIBILITY.md). A3 is the go/no-go: if the panel does not host unmodified, the contract is wrong and we find out in days rather than after six items.

### A1 — the static origin (≈2 days)

**Done** — `src/ember_code/extensions/origin.py`, 21 tests.

`ExtensionOrigin` / `ExtensionOrigins`: one `asyncio` HTTP listener per extension on `127.0.0.1:0`, serving that extension's `ui/` read-only. GET and HEAD, one request per connection, no keep-alive, no body, a capped head with a deadline. Paths resolved and then checked against the resolved root, so a symlink out of the tree is refused like a `..`. Missing and forbidden answer alike. A directory is its index or a 404.

Two things beyond the brief: `frame-ancestors 'none'` until A2 names a host, and the `Host` header must name the bound port — without that, a page on a domain pointed at `127.0.0.1` reaches these ports with the browser's own credentials.

**This reversed a decision recorded above.** The plan said to serve over the existing WS port via the `websockets` library's `process_request` hook, since that was the only HTTP parser in the tree. It does not fit: `websockets` parses only what a handshake can be, so a `POST` never reaches the hook — it is refused by closing the connection with no response and a traceback per attempt, `HEAD` the same way, and anything that can reach the port can fill the log by asking. The replacement is a small HTTP/1.1 server of its own.

Still to wire: the port has to reach the frontend through the extension-status RPC, which does not exist until A6.

### A2 — the bridge (≈3 days)

**Done** — `clients/web/src/extensions/`, 31 tests.

`protocol.ts` is the envelope both sides read. `ExtensionFrame.tsx` is the host: it checks the window, the origin, a per-mount nonce and the granted method, in that order, and nothing else is let through. `bridge.ts` is the guest — the file that becomes a published package — presenting `rpc(method, args, timeoutMs)` plus `navigate`, `close`, `params` and `theme`.

`CodeIndexPanel` turned out to use exactly one thing, `client.rpc`, four times. So the bridge's job is narrower than the plan assumed, and A3's gate will turn on a type rather than on behaviour: the panel's prop is the concrete `IgniClient` class, and no bridge can satisfy that without being one. Narrowing the prop to what the panel actually uses is a one-line change — which means the "empty diff" gate cannot be met literally, and the honest form of it is "one line, and that line documents what the panel needed all along".

One host CSP needs `frame-src` for the loopback origins: `clients/tauri/src-tauri/tauri.conf.json:16` (`default-src 'self'`). **Not the IDE clients** — D16 freezes them at context-handoff scope and extensions render in the igni app only, so `clients/vscode/src/extension.ts:304` keeps its `default-src 'none'` and the JetBrains panel is untouched. An extension that wanted an IDE surface would be asking for the decision to be reopened, not for a CSP line.

*Done when:* the bridge refuses a method the manifest did not ask for — **met, host-side**.

The second half, the backend refusing the same call independently, is **split out as A2b** rather than quietly dropped. It cannot be built yet: the backend has no way to tell that an RPC came from an extension, because everything arrives on one connection as the user. Doing it properly means the host stamping `origin_extension` on calls it forwards — the host is trusted, the frame is not — and the backend checking that against the grant. That needs the manifest (A6) for the grant and a protocol field for the stamp. Until A2b lands, the host-side check is the only one, and a frame that got past it would be calling as the user.

### A3 — port `CodeIndexPanel` — **gate**

**Passed** — one line, and the line was wrong before.

`CodeIndexPanel` now renders real data through the real `connect()` bridge: `codeindex_status` and `codeindex_head_breakdown` go out as `postMessage` envelopes and come back as results. No stub stands in for the bridge; the only thing standing in for production is the window on the other side.

The cost was the prop type. It read `client: IgniClient`, the concrete class — which has private fields and is therefore nominal to TypeScript, so no bridge could ever satisfy it whatever its shape. It is now `Pick<IgniClient, "rpc">`, which is what the panel was using all along: four calls, one method. The body is untouched.

**Still named off `IgniClient`** rather than a hand-written interface, so a change to `rpc`'s signature breaks the panel and the bridge together. A parallel declaration would drift instead.

**What this gate did not prove.** CodeIndex is the easy case — one method. A panel wanting `onEvent` or `runMessage` would widen the `Pick`, and at some point the bridge has to implement those, which is work rather than a type. The harder evidence arrives when Artifacts ports.

### A4 — contribution points (≈3 days)

- `PageKind` union → `string` + registry (`clients/web/src/chat/pageStack.ts:28`).
- `SIDEBAR_LINKS` and `TOOLS_MENU` built from static + contributed (`clients/web/src/App.tsx:143`).
- The static `ArtifactPanel` import at `App.tsx:3300` goes the same way as A3's panel.

*Done when:* an extension contributing a page and a sidebar entry appears in both, and removing the extension removes both without a reload crash.

### A5 — prompt and agent contributions (≈3 days)

Generalise the two hardcoded CodeIndex hooks into named contributions:
- `CODEINDEX_SCHEMA_PLACEHOLDER` (`core/prompts/__init__.py:20`) → a placeholder registry an extension fills.
- The `.codeindex.md` variant suffix (`core/agents/loader.py:151`) → `promptVariants` keyed by extension id.

*Done when:* `CodeIndexSpec()` is no longer hardcoded in the `default()` catalog (`core/tools/tool_spec.py:620`) and the base install has no CodeIndex symbols in it.

### A6 — manifest, validation, signature (≈4 days)

Pydantic model of the manifest in EXTENSIBILITY §2. Install verifies the signature against a compiled-in key for `first-party` and a deployment trust store for everyone else. Unsigned installs are a per-deployment setting, default off. `ExtensionRegistry` reports state through `SubsystemRegistry`.

*Done when:* a tampered bundle refuses to install with a reason, and an expired or unknown signer says which it was.

### A7 — remote extension registration (≈3 days)

A manifest with `endpoint` instead of `sidecar` registers against any backend, local or shared. No UI contribution in `shared` — the frame is hosted by the igni app next to the user, and a shared backend has no such app.

*Done when:* Artifacts attaches to a local backend as a remote extension and the tool surface is identical to the sidecar path.

---

## Track B — shared backend

Auth is first and nothing lands in front of it.

### B1 — deployment profile (≈2 days)

`Settings.deployment.profile: local | shared`. Gates the store driver, whether auth is required, and whether sidecar extensions may install. One switch, both paths testable in CI.

### B2 — auth at attach (≈1 week)

OIDC/SAML at the transport handshake, not a parallel login. The connection carries principal + app identity before the first frame is dispatched. TLS, since it stops being loopback. `transport/websocket.py:111` default host stays `127.0.0.1`; `shared` binds explicitly and refuses to start without TLS and an issuer.

*Done when:* an unauthenticated attach is closed before the first frame is dispatched.

### B3 — session ownership and subscription delivery (≈1 week)

A session is owned by the principal that created it. An inbound `session_id` the connection's principal doesn't own is rejected — this closes the approval-hijack path, since inbound routes on `session_id` alone today. `WebSocketServerTransport.send()` (`transport/websocket.py:248`) stops fanning to `_conns` and delivers to subscribers.

*Done when:* two principals on one backend do not see each other, including that neither can answer the other's HITL approval.

### B4 — store interfaces, then Postgres (≈2 weeks)

Interfaces over `session_preferences`, `session_directories`, `client_state`, `pending_messages` (all raw `sqlite3`, via `core/session/_sqlite_utils.py`). SQLite driver first, no behaviour change, ships alone. Postgres driver second.

Then the scale-out corrections, which are only wrong once a second instance exists:
- `session_directories` maps session → dir → **owning instance**.
- The scheduler takes a lease; `get_due_tasks()` selects and marks running in two statements today, so N instances fire the same task.
- `background_processes` gets an owner column; the orphan rehydrator currently adopts any live PID.
- Concurrent `alembic upgrade head` — a `threading.Lock` today, which Postgres makes real.

### B5 — process per group (≈1 week)

Separate HOME and OS user. `data_dir` is bypassed by ~21 hardcoded `Path.home()` call sites; those are the work.

### B6 — principal-aware router (≈1 week)

Authenticate, resolve principal → group → that group's pool, place **least-loaded**, then pin. Sticky placement is permanent, so the placement decision is the whole design. Batch dispatch (Phase 6) reuses the resolution and skips the pin.

---

> Each *done when* is written as a sentence because that is also the test's
> name when it is written — this suite names tests as statements of what
> holds. They are not quoted as filenames here: a plan that cites a path
> before it exists sends its reader somewhere empty.

## Gates

| Gate | Question | If it fails |
|---|---|---|
| After **A3** | Does a real panel host unmodified? | The bridge is under-specified. Revise before A4; do not build five more items on a contract that didn't hold once. |
| After **A6** | Can an SI build and sign one without us? | Documentation gap, or the manifest is doing too much. |
| After **B3** | Can two principals be put on one backend safely? | Nothing else in Track B is safe to demo. |
| After **B4** | Does the SQLite driver ship with zero behaviour change? | The interfaces are wrong, not the drivers. |

## Still open

- **D7** (open-core split) gates how much of Track A is required for the first paid deal versus merely wanted. A5 is the item that actually separates the licensed code; A1–A4 are useful either way.
- Retention defaults for an unconfigured deployment.
- Whether the shared backend's agent needs a shell at all, or only tools — decides how much of Phase 4's sandbox is needed.
- Does a service-account principal count against a band? Pricing, not engineering.
