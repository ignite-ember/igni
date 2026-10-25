# igni in the cloud — design proposal

Status: **proposal for review. Nothing in this document is built unless
it names a commit.** Everything marked *proposed* is design only.

Related: `docs/EXTENSION_ROUTING_AND_CONCURRENCY.md` (who may call what;
its shared-backend sections are superseded by this document),
`docs/EXTENSIBILITY.md`, `docs/HOST_OBJECT_MODEL.md`.

## 1. Scope

- **The cloud is not for coding.** No repository checkouts, no
  code-intelligence sandboxes. Cloud producers do document, data and
  workflow work through igni's agent and its extensions.
- **Two kinds of producer**, not connected to each other:
  - **people**, who belong to groups;
  - **apps**, which act only as themselves (no groups, never on behalf
    of a person).
- **The desktop stays as it is.** One person, one local backend, local
  extension processes. No microVMs locally.

## 2. One microVM per producer

Every producer gets its own microVM running an ordinary **single-person
igni backend** — the same backend the desktop runs. Isolation between
producers is the VM boundary, so nothing inside igni has to keep people
apart.

| Piece | Choice |
|---|---|
| Runtime | Firecracker (needs KVM) |
| Hosts on AWS | `.metal` instances, or regular Intel instances with nested virtualization: GA Feb 2026 for C8i/M8i/R8i, extended Jun 2026 to the 7i families, I7i, X8i (Intel only). **Benchmark nested-virt overhead before committing.** |
| Persistent data | one EBS volume per producer: sessions, history, the producer's files |
| Instance NVMe | snapshot cache only — it is lost when an instance stops, so nothing lasting lives there |
| On-prem without KVM | gVisor through the Kubernetes agent-sandbox project, with stop/start or gVisor checkpoint/restore |

### Lifecycle (Lambda-style)

1. A request arrives for a producer whose microVM is not running.
2. The gateway restores it from its snapshot (or boots it the first
   time from the warm base snapshot).
3. It serves the request.
4. As soon as the run is done **and** nothing is connected, after a
   short grace period (seconds, not minutes), the microVM is
   snapshotted to disk and stopped. Idle producers use no RAM.

Every producer is restored from **one shared warm base snapshot** taken
after igni's imports. Its clean pages are shared copy-on-write through
the host page cache, so a producer pays only for the memory it dirties.

Restore caveats the backend must handle:

| Caveat | Handling |
|---|---|
| Open connections drop (network, vsock) | the gateway reconnects on the client's behalf; the backend reconnects to igni server and services |
| Guest clock resumes at snapshot time | resync on restore; timers must tolerate a jump |
| Restored copies share random state | reseed on restore (kernel VMGenID on Linux 5.18+, plus app-level reseed) |
| Snapshots need identical CPUs | pin each pool to one instance family |

## 3. The gateway (proposed)

The only thing producers connect to.

1. **Authenticates the producer's token.**
   - Person: their igni token, which carries their groups (built in
     `9badded`).
   - App: a `private_key_jwt` client an org admin registers on igni
     server, the same client-credentials mechanism services already
     use. Proposed.
2. **Finds, creates or wakes** that producer's microVM.
3. **Holds the client's connection** while the microVM restores, so
   the client sees a pause, not a disconnect.
4. **Routes the host object model**, which becomes the public API,
   served over authenticated WSS.

Clients of that API:

| Client | Notes |
|---|---|
| igni's web UI | a person's windows; extension pages load here |
| `igni-client` (proposed Python SDK) | built from the same generated host-model code as the extension SDKs, plus sign-in as an app or a person |
| Apps | have no windows; window-only members stay app-only, sessions and messages work without a window |

Once outside apps depend on the host object model, its schema is a
public contract: breaking changes need a major version.

## 4. Per-node shared services

Heavy, read-only things run **once per node**, not once per producer.

| Service | Status |
|---|---|
| Decisions (Laya) | built in `4cdb3b1`: `src/ember_code/decisions_server` serves it; backends point at it with `IGNI_DECISIONS_URL` and `IGNI_DECISIONS_TOKEN`. A node-level deploy manifest is not written yet. |
| Embedders, if any producer-side feature needs one | proposed, same pattern |
| Inference | unchanged: igni server's proxy |

### Memory per producer

Measured on Linux arm64 (backend plus three extension processes, after
one run) unless marked.

| Setup | Memory |
|---|---|
| Before `4cdb3b1` | ≈ 430 MB RSS (backend 247 + 3 × 61) |
| Backend after `4cdb3b1` (macOS) | 170 MB after one message (was 246); never loads torch |
| Extensions remote (section 5) | the extension processes leave the microVM entirely |
| With the remaining cuts (proposed) | backend ≈ 110–130 MB after a run |
| Restored from the shared base snapshot (estimated, not measured) | ≈ 110–140 MB per microVM including guest kernel |
| Fork from a pre-imported process with `gc.freeze()` (measured, for comparison) | ≈ 49 MB private per backend |

Remaining cuts, all proposed:

- register slash commands lazily at one seam (≈ 30–45 MB);
- run migrations at provisioning, not at every start (≈ 11 MB);
- give the wire protocol its own schemas instead of importing agno
  (≈ 10–15 MB, and defers openai and mcp to the first run);
- make the extension SDK's stdio transport skip starlette and uvicorn
  (≈ 8–12 MB per local extension process).

## 5. Extensions in the cloud: remote by default (proposed)

An extension is an MCP server plus a service plus UI. In the cloud the
MCP server runs **with the vendor, next to or inside its service**, not
in the producer's microVM.

| | Desktop | Cloud |
|---|---|---|
| Extension process | started by igni next to the backend | **remote MCP endpoint run by the vendor**, always on, multi-tenant |
| Who the call is for | the signed-in person | every call carries its producer as `Caller` (built in `9badded`; the SDK keeps callers apart per request) |
| Producer's microVM holds | — | only igni's backend |
| Local process allowed | always | only when the extension declares it needs the producer's machine |

Why: the extension is always up, so long-running work, Celery results
and calls from its own service reach it without waking anyone; and the
producer's microVM stays small.

Consequences:

- **Hooks over the network.** Today `ExtensionManifest` refuses
  `permissions.hooks` for an extension with no process igni starts
  (`_hooks_need_a_process` in `src/ember_code/extensions/manifest.py`).
  Proposed: allow them for remote extensions, with the same 2-second
  timeout (`ExtensionHooks.TIMEOUT_S`) and the same fault state
  (`ExtensionFaults`, three failures in a row).
- **Reaching the host during a call:** back through the same MCP
  session that carried the call.
- **Reaching the host outside a call:** through igni server (section 6).
- **Tokens:** each call carries the producer's token to the vendor,
  audience-bound to the service as today, so the vendor sees exactly
  what its service already sees.

## 6. Event delivery to a producer (proposed)

A new floor capability on igni server.

1. A service asks igni server: deliver this event to extension E for
   producer X. It signs as itself, as it already does for
   `host.notify`.
2. igni server refuses unless X has E enabled; it audits and
   rate-limits per service.
3. **Cloud:** the gateway wakes X's microVM and hands the event to the
   extension (`@ext.on_service_event(...)`, proposed).
   **Desktop:** delivered at once if X's igni is online, otherwise held.
4. When the work is done, the microVM snapshots back to disk.

Long work belongs in the service (its own Celery workers). Work that
must run inside the producer's microVM holds it awake with a bounded,
visible `ext.busy(...)` (proposed): an org-set maximum, renewable, shown
in the extensions panel, counted against the producer's quota.

## 7. Open decisions

1. **Undelivered events:** how long they wait. Proposal: a time limit
   the service sets, at most 24 hours, then dropped and the service told.
2. **Unattended runs:** may an event start an agent run with nobody
   present, or only for apps (people get posts and notifications)?
3. **Rollout order:** Firecracker first (fastest resume, best density,
   needs KVM) or gVisor first (any Kubernetes, air-gap friendly, no KVM).
4. **Startup network call:** the backend clones the default plugin
   marketplace (`DEFAULT_MARKETPLACES` in
   `src/ember_code/core/plugins/marketplace_store.py`) at every start,
   which breaks air-gapped installs. Remove the default?
5. **Linux arm64:** `ripgrep~=15.1.0` has no wheel there, so installing
   needs Rust. Fix before offering Graviton nodes?
6. **Deploy manifests:** the gateway, the microVM pool, per-node
   services and the shared base snapshot need Helm and compose
   definitions. This belongs with the delivery overhaul (Helm charts per
   server, an igni platform chart, compose, an offline bundle) that is
   already on the later TODO.
