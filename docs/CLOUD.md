# igni in the cloud — design proposal

Status: **proposal for review. Nothing in this document is built unless
it names a commit.** Everything marked *proposed* is design only.

Related: `docs/EXTENSION_ROUTING_AND_CONCURRENCY.md` (who may call what;
its shared-backend sections are superseded by this document),
`docs/EXTENSIBILITY.md`, `docs/HOST_OBJECT_MODEL.md`.

## 1. Scope

- **The agent works in the cloud as on the desktop:** the agent loop,
  file tools, shell, background processes and monitors all run, against
  the producer's own workspace. "Not for coding" means only that igni
  itself has no git or code-host features: no provider sign-in, no pull
  requests, no code-intelligence sandboxes in the floor.
- **Two kinds of producer**, not connected to each other:
  - **people**, each of whom belongs to exactly one group;
  - **apps**, which act only as themselves (no group, never on behalf
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
| Engine | Firecracker on KVM, one engine everywhere (measured, section 8) |
| Hosts | bare metal with local NVMe. Nested virtualization works but makes the first request about 5× slower (section 8) |
| Rights in the guest | igni is **root** inside the producer's microVM and installs packages or changes the system for them. The VM is the security boundary, not anything inside the guest |
| Guest root filesystem | the shared read-only base image plus the producer's own writable layer on their volume, so what igni installs persists |
| Durable structured state | PostgreSQL (section 9) |
| Producer's files | one network volume per producer, created and attached automatically (section 10) |
| Node local NVMe | memory snapshots and the suspend journal only — a cache. Losing it costs a cold start from the producer's volume, not data |
| Git | runs inside the producer's microVM as an ordinary program. A person authorizes git inside their own VM; the credentials stay on their volume and are never shared with another VM |

### Lifecycle (Lambda-style)

1. A request arrives for a producer whose microVM is not running.
2. The node agent restores it from its snapshot (or boots it the first
   time from the warm base snapshot).
3. It serves the request.
4. As soon as the run is done **and** nothing is connected, after a
   short grace period (seconds, not minutes), the guest flushes its
   volume and the microVM is suspended to the node's journal and
   stopped. Idle producers use no RAM.

Every producer is restored from **one shared warm base snapshot** taken
after igni's imports. Its clean pages are shared copy-on-write through
the host page cache, so a producer pays only for the memory it dirties.

Restore caveats the backend and node agent must handle:

| Caveat | Handling |
|---|---|
| Open connections drop (network, vsock) | the gateway reconnects on the client's behalf; the backend reconnects to igni server and services |
| Guest clock resumes at snapshot time | resync on restore; timers must tolerate a jump |
| Restored copies share random state | reseed on restore (kernel VMGenID on Linux 5.18+, plus app-level reseed) |
| Clones share IP, MAC and ARP state | one network namespace per VM, a fixed host tap MAC |
| Snapshots need identical CPUs | pin each pool to one instance family |

### VM profiles (proposed)

The org admin defines named profiles and assigns one to each app and each
group; a person, who belongs to exactly one group, gets that group's
profile — there is never a choice between several. A profile holds:

| Setting | Example "standard" | Example "browser" |
|---|---|---|
| Memory: normal, most (ceiling), and CPUs | 1 GiB, up to 4 GiB, 1 vCPU | 2 GiB, up to 8 GiB, 2 vCPU |
| `idle_before_pause_s` | 10 s | 10 min, or never |
| Expected memory, for placement | the node's estimate | 3 GiB |

Every value a VM boots and runs with is a setting: the profile's above (igni
server; the deployment sets the Standard profile and the caps), the rest in
the node agent's configuration (`igni__cloudvm/docs/node-agent.md`, "Every
setting a VM boots or runs with"). Nothing closes caches before a suspend:
what it would drop, the next resume reads again.

- The platform sets the maximum ceiling and the longest timeout; time
  awake counts against the org's quota.
- A long `idle_before_pause_s` is for producers whose suspend would be
  large: a headless browser dirties hundreds of MB to GBs, so suspending
  and resuming it between closely spaced requests costs more than
  staying awake.
- A new timeout applies at once; a new ceiling at the producer's next
  cold boot (a memory snapshot keeps the size it was taken with). The
  node agent forces that cold boot at the next suspend; nothing is lost,
  since durable state is in PostgreSQL and on the volume.

### Elastic memory: a ceiling, a normal size, real usage only

- **The ceiling is fixed at boot** and never exceeded: Firecracker fixes
  a VM's memory when it boots.
- **A normal size, held by the balloon.** The VM boots with the ceiling
  and the balloon inflated so the guest normally has the profile's
  normal size (1 GiB by default; igni idle uses ~220 MB). While the guest
  needs more (a native build: igni and a ripgrep build peaked at 1.9 GB)
  the node agent deflates the balloon, up to the ceiling, if the node has
  room; if not, the paused tier makes room by stopping the longest-paused
  VMs. It inflates it back when the need is over and before every pause,
  so a paused VM and its snapshot are back to about the normal size
  (`igni__cloudvm/docs/node-agent.md`). `deflate_on_oom` is the
  guest's last resort, counted against the node like a grant.
- **Only touched pages cost.** The host backs only the pages the guest
  touches, so a VM at 300 MB of a 4 GB ceiling holds 300 MB of host RAM.
- **Shrinking gives memory back.** The guest reports freed pages to the
  host through Firecracker's balloon device (free page reporting) and
  the host releases them. igni closes heavy things (the browser) when a
  run ends, not only at suspend, and the guest trims its file cache on a
  timer. Released pages are also left out of the next snapshot.
- **Overcommit is deliberate.** Ceilings may add up to more than the
  host's RAM. The node agent watches real usage and reclaims memory or
  stops placing new VMs on a host that is running short.

Measured (`igni__cloudvm` `747dc16`, nested, Firecracker 1.17, 4 GiB
ceiling):

- Free page reporting (always on) returned 99% of a freed 1 GiB within
  about 6 s; host-triggered free page hinting within about 0.1 s — used
  before a suspend and under host pressure. Both are developer preview
  in 1.17.
- Overcommit: 10 VMs with 4 GiB ceilings, each growing to 2 GiB in turn,
  peaked at 6.3 GB of host memory (24 GB with the balloon off).
- No cost when idle, no change in request latency; about 14 points more
  guest CPU under heavy churn. Restore with a balloon device worked in
  all 80 cycles; the per-VM warm image is 3.7× smaller.
- A 4 GiB ceiling suspends in 18–26 ms (10–16 ms at 256 MiB): ceilings
  should fit the profile.
- The three gaps, closed (`igni__cloudvm` `57ed5e9`, decided defaults):
  - **Fragmentation:** free page reporting at order 3 (set through sysfs
    after boot; kernel 6.1 ignores the boot parameter) plus an idle-time
    cgroup v2 `memory.reclaim` trim. After headless Chromium the host
    holds 352 MB instead of 1,006 MB (guest's own use plus the ~150 MB
    every fresh VM carries). No guest compaction: it stalled requests
    220–270 ms.
  - **Freed pages in suspend diffs:** a 93-line Firecracker patch records
    every range the balloon discards and leaves pages still untouched at
    snapshot time out of the diff, listing them in a `.discard` sidecar
    the node agent journals and punches out of the memory file. The
    grow-and-shrink-1-GiB case: 1,134 MB / 5.5 s per suspend stock,
    88 MB / 0.48 s patched; resume and first request unchanged; 160
    restores with a checksummed 256 MB guest buffer, no data lost.
    Production seccomp must allow reading `/proc/self/pagemap`.
  - **Page cache:** `memory.reclaim` returns 93% of what `drop_caches`
    does but keeps hot files (a hot re-read 16 ms vs 243 ms). No
    `drop_caches` timer. Future guest kernels get MGLRU.
- Node agent, before every suspend: `memory.reclaim` trim, one hinting
  run (~0.1 s), then the journaled suspend. Under host pressure: hinting
  and trim on the largest VMs first.

## 3. The gateway (proposed)

The only thing producers connect to.

1. **Authenticates the producer's token.**
   - Person: their igni token, which carries their one group
     (`group`).
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
| Commands on first use, migrations at provisioning (built, macOS) | backend 153 MB ready, 159 MB after one message (was 163 / 169) |
| With the remaining cuts (proposed) | backend ≈ 110–130 MB after a run |
| Restored from the shared base snapshot (estimated, not measured) | ≈ 110–140 MB per microVM including guest kernel |
| Fork from a pre-imported process with `gc.freeze()` (measured, for comparison) | ≈ 49 MB private per backend |

Built:

- slash commands load on first use: the registry
  (`ember_code/backend/builtin_command_registry.py`) holds each built-in
  as `"module:attr"` and imports it when it first runs, so an idle
  backend carries none of `/evals`, `/loop`, `/schedule`, `/plugin`;
- migrations run at provisioning: a backend reads its database's
  revision with `sqlite3` and imports alembic only when the file is new
  or behind (`ember_code/core/db/schema.py`). A producer's volume is
  prepared with `igni migrate`, so its backend starts current and never
  imports alembic.
  With PostgreSQL (section 9) this moves from "per producer's volume" to
  "once per org database, at deploy"; the backend still never imports
  alembic.

sqlalchemy, dateparser and dnspython still load at start: the agent's
schedule tool, the session's loop store and sign-in discovery use them,
not only commands.

Remaining cuts, all proposed:

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

## 6. Event delivery to a producer

A floor capability on igni server (its service-identity document, §15:
built) and igni (`extensions/service_events.py`: built).

1. A service posts an event for its own extension and a producer
   (`POST /v1/service-events`), signed as itself. igni server refuses
   unless the producer has the extension on; it audits, rate-limits per
   service and keeps the event until delivered or expired (14 days).
2. **Cloud:** the gateway wakes the producer's VM, pulls its events
   (`/v1/platform/.../events/pull`, a 2-minute lease) and hands them to
   igni with the `deliver_service_events` RPC (`{events: [...]}`) on its
   own control stream — igni takes it from nowhere else (below); igni
   answers which to acknowledge (`acknowledge`: delivered now or before)
   and the gateway acks those (`/events/ack`). Delivery is at least once;
   igni keeps every event id it delivered (`delivered_events`, scoped,
   in the producer's database), so a repeat — after a lost ack, across
   restarts — is acknowledged and dropped. One igni could not take
   (`failed`) is left unacknowledged and comes back.
   **Desktop:** igni pulls the person's own events itself while online
   (`extensions/event_pull.py`): `POST /v1/me/events/pull` with their
   session, each through the same inbox, then `POST /v1/me/events/ack`
   with what to acknowledge; a full page (100) is followed at once by the
   next, otherwise it asks again a minute later. An app's token gets a
   403 and the pull stops; the cloud layer leaves pulling to its gateway
   (`Layer.fetches_own_events`).
3. For each new event igni asks the extension it is for, over its host
   socket (`@ext.on_service_event(type)` in the SDK; 30 s), what the
   agent should see; without an answer it writes it itself from the
   type and payload.
4. **The event always starts or resumes the producer's agent run**,
   unattended, for people and apps alike: it goes to the producer's
   main session as the extension's message (labelled as the
   extension's, never the person's). Nothing going on: it **starts a
   run**. A run going — working, or **waiting on an approval**: it is
   **queued for that run** (durably, as a message typed during a run):
   the run takes it after its next step, and a run paused on an approval
   takes it once the person has decided and the run goes on. Not a
   second run beside it: two runs in one conversation would race over
   its history, and an event must never answer, or go round, a decision
   that is a person's. Approvals the run then meets wait as usual (and a
   run waiting on one is not busy: the VM may sleep until the decision).
   An event is recorded as delivered only once it has started its run or
   been queued, so a crash before then gets it handed out again.
5. When the work is done, the microVM snapshots back to disk.

Long work belongs in the service (its own workers). Work that must run
inside the producer's VM holds it awake with `ext.busy(reason, ttl)`
(built, section 9): a lease that expires after at most 600 s unless
renewed, shown in `app.activity` and `activity.json`. An org-set maximum
from the VM profile, and showing it in the extensions panel, are still
to come.

## 7. Packages and commands in a VM (proposed)

Two ways to put software in a producer's VM, besides asking igni's agent
(which is root in the VM).

**Prepared packages, per profile.** A VM profile (section 2) carries a
package list (apt, pip, npm) or a setup script. The platform builds it
into a read-only **profile layer** between the shared base and each
producer's writable layer, signed and built from the customer's own
package mirror so air-gapped installs work. Every VM with the profile
shares it (Chromium stored once, not per VM), and each profile has its
own warm snapshot. A changed list rebuilds the layer; a VM picks it up
at its next cold boot.

**Commands, dynamically.** API → gateway → (wakes the VM) → node agent →
a small agent inside the guest over vsock (no network path into the VM)
→ runs the command as root, streams output and the exit code back.

| | Decision (2026-10-26) |
|---|---|
| Who may run commands | the producer in their own VM (a person, or an app with its token); an org admin in any VM of their org; the super admin in any VM of the platform. Every command audited: who, which VM, the command, exit code |
| A suspended VM | one VM: the command wakes it. Many VMs (a group, a profile, an org): a job applied to each VM at its next wake, never a mass wake |
| Lifetime | what a command installs lives in the producer's writable layer, which an image upgrade may reset; anything meant to last belongs in the profile |

The super admin can also define profiles and their packages and assign
them in any org.

The guest agent is the same one that flushes the volume, reseeds after
restore, trims the page cache and triggers free page hinting.

## 8. Suspend and resume: measured

Proof of concept in the `igni__cloudvm` repository (`RESULTS.md`, raw
data under `results/`). Guest: 256 MiB, a Python server holding 127 MB,
dirtying 2 MB/s. Each cycle is real: suspend, kill the Firecracker
process, restore a new one from durable state, first request. n=20 per
row, 180 cycles per environment, state intact after every one.

**Suspend** = pause, diff snapshot to tmpfs, one append to a pre-zeroed
per-node journal, `fdatasync`. **Restore after merge** = the diff was
folded into the producer's memory file in the background (20–50 ms,
off both paths). "In memory" = the producer's files still in the host
page cache; "on disk" = caches dropped.

Bare metal (z3-highmem-192-highlssd-metal, local NVMe, XFS with reflink),
p50 / p95 ms:

| Variant | Suspend | Resume | First request | Second request |
|---|---|---|---|---|
| Restore after merge, in memory | 13.3 / 15.1 | **6.3** / 7.5 | **9.7** / 10.2 | 0.9 / 1.5 |
| Restore after merge, on disk | 19.4 / 21.5 | 23.4 / 25.8 | 45.1 / 50.1 | 1.9 / 3.2 |
| Journal applied during restore, in memory | 15.2 / 16.6 | 13.8 / 15.0 | 9.9 / 10.3 | 1.3 / 2.0 |
| Journal applied during restore, on disk | 19.7 / 21.0 | 32.4 / 34.7 | 30.5 / 35.1 | 2.3 / 6.0 |
| UFFD with working-set prefetch, in memory | 25.4 / 27.8 | 16.0 / 16.7 | 9.9 / 12.4 | 1.8 / 3.5 |
| UFFD with working-set prefetch, on disk | 27.3 / 30.0 | 36.0 / 38.3 | 12.3 / 18.8 | 2.8 / 12.0 |

Nested virtualization (n2-standard-8, local NVMe), same first row:
suspend 10.8, resume 4.8, **first request 50.7**, second request 2.0.
Bare metal makes the first request about 5× faster, because a guest page
fault costs about 1–2 µs instead of 15–50 µs. Suspend and resume were a
little slower on the z3 host than on n2; that compares two machine
families, not bare metal with nested.

Findings:

- Resume ≤ 10 ms and first request ≤ 10 ms are met on bare metal when
  the producer's files are in the page cache. From disk, prefetch keeps
  the first request at 12 ms.
- Suspend is 13–15 ms, not ≤ 10. Nobody waits on it (the reply has
  already gone out); doing the journal write in the node agent instead
  of a helper process already saved 5.5–8 ms.
- A suspended producer holds no host memory. A running idle one costs
  3–4 MB of private host memory on top of the shared base; about 45 MB
  in the worst case. 400 VMs ran on one host.
- Firecracker diff snapshots are a developer preview; the design merges
  only after the VM is killed.
- Prefetch-backed VMs write diffs twice as large (10 vs 5 MB), which
  makes their suspend about 25 ms. Not yet explained.
- Cloud Hypervisor was measured too: the fastest durable suspend
  (8.4 ms, writing memory to disk continuously) but 15–30 ms resume and
  no usable prefetch path. Firecracker is the choice.

Not yet measured: the real igni backend as the guest, with a realistic
agent run between suspends (larger diffs); per-producer volumes
(section 10, in progress).

## 9. Durable state: PostgreSQL

All of igni's durable state in the cloud lives in PostgreSQL, written
when it happens, never held in memory until suspend. The microVM's
memory and the node's journal are then a cache: losing them costs a cold
start, not data, and a producer can move to another node with nothing
to copy.

**Tenancy: the org is the tenant; the producer is a filter.** One
database per org, shared by its people and apps. Every row carries its
org, its producer and its project (the folder it belongs to), and the
backend filters by them, the way changing directory shows only that
directory's chats today. No database, schema or VM per producer for
data. Under the backend's filter, PostgreSQL row-level security keyed
on the producer's own database role, so a missing filter — or igni
compromised inside a VM — returns nothing instead of someone else's
rows (below).

What moves where (audit of `src/ember_code`, 2026-10-26; built in
`ccbca59`, `9ef5eda`, `1e231c4`, `dac89aa`, `7fc48b0`, `66b6a36`,
`7a99d1a`, `7c94a2a`, `fa78797`, `4ece789`, `23fdd06`, `3cf7bfe`,
`21aac46` and `f6a932b` unless the row says otherwise):

| State | Desktop (unchanged unless noted) | Cloud |
|---|---|---|
| Sessions, transcripts, runs, `session_data` (event log, todos, plan decisions), memories, learning, sub-agent sessions | Agno `AsyncSqliteDb` in the project's `state.db` (`core/memory/manager.py`) | Agno's `AsyncPostgresDb` as `ScopedAsyncPostgresDb`, on igni's engine: the scope is in the session's key and in `user_id` |
| Schedules, `/loop` state, background processes | alembic tables in `state.db` | the same tables, scoped |
| Pending and interrupted messages, model per session | alembic tables in `state.db` (were raw `sqlite3`) | the same tables, scoped |
| Session → directory, window state and extensions' own state, local notifications | alembic tables in one global database in the data directory (`global_db_path`; were `sessions.db`, `client_state.db`, `notifications.db`) | the same tables, scoped |
| Approvals waiting on the user (main agent, auto-resolved, sub-agents and teams, the calls waiting above them and the finished calls of their turns), messages typed during a run | `waiting_approvals`, `waiting_spawns`, `turn_results` and `queued_messages` in `state.db` (were memory only) | the same tables, scoped |
| `MEMORY.md` memory files | files under `~/.igni/projects/` | `memory_files` rows; the memory directory is a write-through mirror of them |
| Uploaded attachments | `<project>/.igni/attachments/` | PostgreSQL large objects, written and read in chunks, an `attachments` row each |
| Credentials | `~/.igni/credentials.json` | from the gateway: a token file it rewrites on refresh (`IGNI_ACCESS_TOKEN_FILE`) or a fixed `IGNI_ACCESS_TOKEN`; `credentials.json` is never read or written |
| Settings, group policy, priority, the person's group, MCP approvals, extension settings | JSON and YAML under `~/.igni` | settings only from the image (command line, managed policy) and igni server (group policy); nothing under `~/.igni` or the workspace's `.igni`. Priority and the person's group from igni server's listing (`GET /v1/extensions`), kept in the process; MCP approvals are the person's own, kept by igni server (`/v1/me/mcp-approvals`: approve once, for every VM of theirs), for servers group policy offers — a server not offered cannot be approved, and an unreachable server approves nothing; extension settings a row per extension (`extension_settings`), scoped |
| Logs | files under `~/.igni` | JSON lines on standard output, collected by the node: no backend log, `debug.log`, `llm_calls.log`, `hitl_trace.log` or audit log file |
| Workspace files, `.igni` in the workspace, worktrees, process logs, installed packages | files | the producer's volume (section 10) |

**One product: the core and a cloud layer.** The desktop igni
(`ignite-ember`, `ember_code`) is the core; the cloud is a layer on top,
its own package — `packages/igni-cloud` (`igni-cloud`, `igni_cloud`) —
which the core never imports (a test holds it to that). The core asks
seams instead of where it runs (`core/layer.py`): where logs go, where
credentials come from, whether settings files under `~/.igni` count,
whether state must be in a database, whether igni server's listing is
kept in files, how MCP servers are approved, and what a backend does
around its boot and serving. Each has the desktop's answer by default.
`IGNI_PROFILE=cloud`, set by the image, is read in one place
(`Layers`): it installs the layer the `igni.layers` entry point `cloud`
names — `igni-cloud`'s `CloudLayer` — for the backend, `igni migrate` and
the CLI alike, from the one setting the image already has; a desktop,
which never sets it, never imports a line of cloud code, and a name no
package declares is refused rather than run as a desktop. The image
installs both packages (`pip install ignite-ember igni-cloud`); the
desktop's wheel depends on neither psycopg nor `agno[postgres]`.
PostgreSQL storage — Agno's scoped storage, uploads as large objects,
and what `igni migrate` prepares (Agno's tables, row-level security) —
is chosen by the database URL instead, through the `igni.storage` entry
point `postgresql` (`core/db/providers.py`; `igni-cloud`'s), so it is
there wherever the package is installed.

**How the database is chosen.** One code path; `storage.database_url`
(`IGNI_DATABASE_URL`) picks the backend. Unset, a project's state is its
`state.db` and what is no one project's is the global database in the
data directory, as before. Set, both are that one database and every row
is scoped. The URL may be any spelling of PostgreSQL (`postgresql://`,
`postgres://`, `+asyncpg`, `+psycopg`); igni speaks psycopg 3 to it,
sync and async, one driver for alembic, its stores and Agno (the
`postgres` extra). `igni migrate` (`--database-url`) prepares it; a
backend whose schema is current still never imports alembic.

**Scoping, in one place.** Every igni table carries `org`, `producer`
and `project` (empty on the desktop, whose file is already its own), and
each is part of a key wherever uniqueness needs it — a pid, for one, is
unique per VM, not per org. Every query goes through `StorageScope`
(`core/db/scope.py`): `org` and `producer` from the signed-in principal
(`SignedInPerson.tenant`; `org` is the tenant, `producer` is
`person:<id>`), `project` from `resolve_project_id` of the session's
folder, so two clones of one repository share state as they do at home.
A shared database with nobody signed in refuses to open rather than read
unscoped. The one lookup that cannot know the project — which folder a
session is in — filters by org and producer only. Under that filter,
row-level security (below) holds the same line in the database.

**Row-level security** (`igni_cloud/db/rls.py`), so the isolation holds when
igni inside a VM is buggy or compromised:

- *Each incarnation of a producer connects as its own PostgreSQL role.*
  The role is bound to its org, producer and incarnation in
  `producer_roles(role_name, org, producer, incarnation)`,
  a table no producer role can read or write; the policies reach it only
  through `SECURITY DEFINER` functions keyed on `session_user` — the role
  that logged in, which `SET ROLE` cannot change. Nothing the VM can
  `SET` decides whose rows it sees. A mapping table rather than role
  names spelling the ids: org and producer ids are free-form and may not
  fit a role name, and an opaque name rotates without the data changing.
- *Incarnations* (igni server's service-identity §11.3). A producer's
  **incarnation** is which life of it a VM is: a person's membership of
  the org, new each time they are added; an app's or a space's own id.
  A person removed and added again is a new incarnation and must never
  see the old one's data, which the org keeps for its retention period
  (default 30 days) and then erases. So the cloud's rows are an
  incarnation's: the producer column holds `person:<id>@<incarnation>`
  (`ProducerIncarnation.stored`; an incarnation is an id without `@` or
  `/`), and so does the connection's side of every policy and Agno's
  `user_id` prefix. In the producer value rather than a column of its
  own because igni's tables are keyed — and upserted — on org, producer
  and project, and the core knows nothing of incarnations: a separate
  column would leave the new incarnation's writes colliding with the
  old one's keys. A role per incarnation rather than one per producer
  told its incarnation: the role is what the VM cannot change, so the
  new VM's credentials cannot reach the old rows, and old credentials,
  should they leak, cannot reach the new ones.
- *Every igni table* with `org` and `producer` columns has `ENABLE` and
  `FORCE ROW LEVEL SECURITY` and one policy, for reading and writing, on
  org and producer; the project stays the application's filter (a person
  sees all their projects). *Agno's tables* (schema `ai`) are held by the
  scope prefix of `user_id`; its versions table is readable, the tables
  igni does not use are closed. *Large objects* belong to the producer's
  role that uploaded them, with `lo_compat_privileges` off; the
  `attachments` row naming an oid is under the table policy.
- Producer roles take their table privileges from the group role
  `igni_runtime`, granted `WITH INHERIT TRUE, SET FALSE` (PostgreSQL 16 or
  later), so a producer cannot `SET ROLE` to it either; no producer role
  owns a table or has `BYPASSRLS`. The tables belong to an admin role,
  which runs migrations: `igni migrate --database-url <admin url>` also
  makes Agno's tables (a producer's role may not, and igni tells Agno
  not to at run time) and applies the roles, functions, grants and
  policies on every run, so a table a migration adds is covered. The
  admin role must bypass row-level security (superuser or `BYPASSRLS`)
  for a migration that moves data.
- Cost: every policy column leads a primary key or an index; measured on
  a point query by primary key on local PostgreSQL 17, about 130 µs per
  round trip as the admin role and 165 µs as a producer role — the policy
  is a one-time filter over the same index scan.

**Provisioning contract** (for the node agent and igni server, build
plan steps 3 and 5). With the org database's admin URL:

- `igni-cloud db grant-producer --org <org> --producer
  person:<id>|app:<id>|space:<id> --incarnation <id> [--owner app:<id>]` (the cloud package's
  command; a **space** is one end user of an app, a producer of its own,
  the app its owner — `IGNI_PRODUCER_OWNER` in `producer.env`; igni server
  is still settling its contract. It imports the role
  code and SQLAlchemy only — 0.16 s for a grant, where `igni db` in the
  full CLI took 0.84 s — since the node agent runs it on a new
  producer's first start)
  creates the incarnation's role, or rotates its password when it
  exists, and prints JSON: `role`, `password`, `database_url`, `producer`,
  `incarnation`. The platform hands `database_url` to the producer's VM
  as `IGNI_DATABASE_URL`, the way it hands the token, and the
  incarnation as `IGNI_PRODUCER_INCARNATION` (the feed's `incarnation`). The org and producer must be the ones the producer's
  token names: the application's filter and the policy then agree, and a
  write in any other scope is refused by the database.
- `igni-cloud db revoke-producer --org <org> --producer <id> --incarnation <id>`
  drops the role — when the incarnation is removed. What it owned (its
  uploads' large objects) passes to the admin role; its rows stay, for
  the org's retention period, unreachable by any producer.
- `igni-cloud db export-producer --org <org> --producer <id> --incarnation <id>
  --out <file>` writes the incarnation's data, for an org admin during
  retention, as a zip: `manifest.json` (whose, when, rows per table,
  uploads), `rows.jsonl` (`{"table", "row"}` per line: every igni table's
  and Agno's rows, dates ISO 8601, bytes base64) and
  `attachments/<id>/<filename>`; read in one repeatable-read snapshot,
  written to a `.partial` file and renamed. It prints the manifest.
- `igni-cloud db erase-producer --org <org> --producer <id> --incarnation <id>`
  deletes, in one transaction, every row of the incarnation in igni's
  and Agno's tables, unlinks its uploads' large objects and drops its
  role (with anything else it still owned), and prints the audit
  summary: rows per table, large objects and their bytes, whether a role
  was dropped, when. Run again it finds nothing and says so. Export and
  erase need the admin role to bypass row-level security, as data
  migrations do; one that does not is told so.

**The VM's own credential** (igni server's service-identity §16;
`igni_cloud/guest/credential.py`). The platform never mints a producer a
token: each VM holds a **VM credential** and exchanges it for every token
it needs. A person's comes when they sign in through the gateway, which
hands their session to igni with the `vm_sign_in` RPC
(`{session_token, organisation?}`, answered with the credential's state)
on its control stream. **Both platform calls are taken only from the
platform's control connection:** the node agent adds `X-Igni-Gateway:
control` to the upgrade of the gateway's `stream?control=1` and strips
it from every other stream, and igni reads it off the connection, never
off a message (a client sending either call gets an error);
igni exchanges it (`vm-sign-in` grant). An app's the node agent writes to
the volume once; igni takes it when it appears, without a restart. The
credential lives in `IGNI_VM_CREDENTIAL_FILE` (on the volume, `0600`,
replaced atomically and fsynced); the 15-minute access token igni gets
from it goes to `IGNI_ACCESS_TOKEN_FILE` (the run directory), which every
part of igni already reads on every credentials load, so nothing else
changes when it is renewed. Renewal (`vm-credential` grant) runs a minute
before expiry, after a restore, and one at a time; every exchange rotates
the credential, and the new one is written before the token is. A crash
between the exchange and that write leaves the rotated-away credential
on the volume; presenting it again is reuse, which revokes the family —
the person signs in again, an app is re-issued (the window is one round
trip and an fsync). `invalid_grant` (revoked, reused, idle 90 days, the
person signed out or was removed, the app deleted) removes the token and
puts the VM in **needs sign-in** (a person) or **needs re-issue** (an
app, logged as an error): pushed to the windows (`vm_credential`) and
written to `activity.json` (`credential`). The node agent no longer
writes or refreshes tokens; `IGNI_ACCESS_TOKEN` (fixed for the process)
remains for a VM started without the guest contract.

**Managed settings** are what the node agent writes to
`/run/igni/managed-settings.yaml` at every start, after the token and
before `producer.env` (`IGNI_MANAGED_SETTINGS_FILE`; the layer's
`managed_settings_path` seam) — `api_url` among them.

**Confirmed end to end on Linux** (a container, PostgreSQL on the host):
a cold boot with the node agent's files in place — an app's credential
on the volume, managed settings, `producer.env` with a granted role —
binds, exchanges the credential (rotated, kept `0600`), writes the token
and `activity.json` with `credential: ready`, and answers a client, 3.3 s
after start including a cold interpreter; the cloud package's tests pass
there too, inotify included.

**An app's or a space's services.** In an app's or a space's VM the
token an extension gets for its service (the loopback, igni server's
service-identity §9) comes from the same exchange a person's session goes
through, with the VM's own token as `subject_token` (§9.2): the result is
an `app` or `space` token for the service's URL, cached and renewed as a
person's. The caller igni names to its extensions (`igni/principal`) is
the app or the space the token's `kind` says — a space with its `app` and
`external_id` — never a person.

**Bound before anybody signed in.** igni binds with `producer.env`'s
database, org and producer alone — automatic provisioning boots a
producer's VM before the person ever signs in. Its rows are that
producer's from the start (the tenant is `producer.env`'s; a token, once
there, must name the same producer). Everything local works — the
database, the stores, sessions — and whatever needs igni server or a
service says it needs sign-in until the credential arrives, which igni
picks up without a restart.

**Guest contract** (igni's side of the guest protocol in
`igni__cloudvm`; `igni_cloud/guest/`, the cloud layer's backend hooks). Three
files on the VM's tmpfs, each movable by a variable (`IGNI_RUN_DIR`
moves all three, default `/run/igni`):

- `ready` (`IGNI_READY_FILE`): written once igni listens, its imports
  done, before any producer is known — so a warm base snapshot can be
  taken there. Removed when igni starts and when it exits, so a file
  left by an earlier igni never says a new one serves. Before writing it
  igni also imports what a bound backend's first bind, request and turn
  would (`WarmImports`: the PostgreSQL driver, alembic's schema check,
  the OpenAI client's streaming path, Agno's run internals, the tool
  hooks), so they are in the shared snapshot rather than paid for by
  each producer. Measured with `scripts/measure_warm_base.py`
  (PostgreSQL, fake model, Apple M-series, five runs each): RSS at
  `ready` 153.9 → 169.2 MB, `producer.env` → first answered request
  0.34 → 0.27 s, modules still imported after `ready` 130 → 9 (macOS
  platform probes only).

  **After binding** igni boots its backend, and that path is kept short:
  everything producer-independent is imported before `ready` (above);
  a project's id asks git once per repository state instead of once per
  store opening a scope (`ProjectIds`, eleven subprocesses → one); Agno's
  tables are checked and reflected once per process instead of before
  every query (`ScopedAsyncPostgresDb`, three round trips per read →
  none). Measured bound → first answered request, PostgreSQL on the same
  machine: 224 → 105 ms — settings 1 ms, the session (`BackendServer`)
  101 → 48 ms, its start (`startup`: schema check, the boot session's
  interrupted-run probe, persisted loop and plan state) 85 → 40 ms, the
  pool, host model and extensions under 3 ms. What is left needs the
  producer's database or volume: the first table reflection and schema
  check per database, and the agents and memory files on the volume.
  The plugin marketplace registration and other background services
  start after the first answer, off the path.
- `producer.env` (`IGNI_PRODUCER_ENV_FILE`): **late binding.** A warm
  base's igni starts with no producer. While the file is missing (or
  names no `IGNI_DATABASE_URL`) igni opens nothing — no settings, no
  database, nothing under the home directory — and answers every request
  with a refusal (an RPC's `error`, else an `error` message). It looks for
  the file the moment it is renamed into place: igni watches the run
  directory with inotify (`IN_MOVED_TO`; measured rename → wake 0.04 ms
  median in a Linux container), looking again every second in case an
  event is missed, and polls every 100 ms where inotify is missing (the
  protocol has no "look now" call). Measured end to end on macOS, which
  polls: `producer.env` renamed → bound 87–92 ms, nearly all of it the
  poll interval. It binds when the file names a
  database and the token in `IGNI_ACCESS_TOKEN_FILE` is a producer's
  whose org and `person:<id>` are the file's `IGNI_ORG` and
  `IGNI_PRODUCER`, and names `IGNI_PRODUCER_INCARNATION`, whose rows igni
  then reads and writes; only `IGNI_*` lines are taken into the environment.
  The backend then changes into the project directory again (the volume
  is mounted over it by then) and boots as it would have with the
  variables set from the start. A file rewritten as it was is nothing; one
  that comes to name another producer, incarnation or database shuts igni down, for
  the image's supervisor to start one that binds afresh. An igni started
  with `IGNI_DATABASE_URL` set and no file binds at once, as before.
- `activity.json` (`IGNI_ACTIVITY_FILE`): `app.activity` — `state`,
  `suspendable`, `waiting_approvals`, `sessions`, `busy`, `wake_at` —
  plus `running` (runs working), `clients` (windows connected) and
  `written_at_ms`. Written whole (temporary file, rename) on every
  `app.activity_changed`, on any change seen in a check every second,
  and every 4 s while it is not suspendable; removed when igni exits.
  For the node agent:
  - Times are Unix milliseconds (`written_at_ms`, `wake_at`, and `since`
    / `expires_at` in `busy`).
  - `busy` is a list of `{kind, holder, reason, since, expires_at}` —
    what holds igni awake outside a run's own turn. `kind` is one of
    `post_run`, `model_call`, `http_call`, `mcp_call`, `extension_call`,
    `scheduled_task`, `process`, `monitor`, `extension`, `background`.
    Any entry makes `state` `running` and `suspendable` false. An entry
    with `expires_at` stops counting then on igni's side; the file is
    rewritten when it does.
  - `wake_at` (Unix milliseconds, or `null`) is the earliest time igni has
    work due while nothing runs — today the scheduler's next task. A
    suspended VM should be woken at it (and need not be woken before it
    for igni's sake).

**What decides `suspendable`** (`core/activity`, core — the desktop has
it too and suspends nothing). One registry of **busy leases**: anything
working holds one while it works and lets it go when done — every HTTP
call igni makes (model calls, igni server, services: `BusyAsyncClient`,
until a streamed answer is closed, at most 15 minutes), MCP tool calls,
extension service calls, scheduled tasks while they run, background
shell processes and monitors while they run, post-run work (learning
extraction, session naming), and an extension that says it is working
(`self.busy` in the host model, `ext.busy(reason, ttl)` /
`ext.busy_while(reason)` in the Python SDK: a lease per reason that
expires after at most 600 s unless renewed, and goes when the
extension stops). A run counts through its session as before; waiting
on a person holds nothing. Every task or thread igni starts goes through
one helper (`BackgroundTasks`: `start` holds a lease until the work
ends, `daemon` is for loops that live as long as their owner and hold
none); `tests/test_background_work_is_accounted_for.py` fails on any
other `create_task`, `ensure_future`, `Thread` and the like under
`src/`. Work due later publishes a **wake**; the earliest is `wake_at`.

**Randomness.** Every VM restored from one warm base starts with the
same memory, pseudo-random generators included. The kernel's are
reseeded by `post_restore` (so `os.urandom`, `secrets`, `uuid4`); igni
reseeds Python's own — the `random` module, any module-level
`random.Random`, `tempfile`'s name sequence — from the kernel when it
binds and after every restore it notices (`ProcessEntropy`). After a
restore it reseeds the generators it found at binding at once and looks
for new ones two seconds later: a restored VM's memory comes back page
by page as it is touched, and walking every module's namespace on the
path of the first request after a resume is what the WebSocket handshake
then waits on. The string
hash salt (`PYTHONHASHSEED`) is fixed at interpreter start and stays
shared; it only matters for hash flooding.

**After a restore** the guest agent's `post_restore` steps the wall
clock and then writes `/run/igni/restored` (`{generation, at_ms,
clock_step_ms}`; `IGNI_RESTORED_FILE`). igni watches the run directory
with inotify. No request waits for that: a client connecting is proof
enough that the VM runs. On each new generation igni only marks every
pooled database connection as older than the restore (SQLAlchemy's
pool-wide invalidation: a connection is recycled — closed, opened afresh
— when a query next checks it out, before its pre-ping could wait on a
connection whose packets go nowhere); that touches nothing. The moment
its first answer has gone out it reseeds (above; nothing on the
first-answer path draws from Python's generators, and the kernel's were
reseeded before the VM ran). The rest — reopening the notification
stream keeping its cursor, re-arming the timers, rewriting
`activity.json` (a window attaching meanwhile leaves it to this),
renewing the credential — waits half a second more for the burst of
requests a window sends with its first, or one second if nobody asks.
On a restored VM every page touched before an answer is one it waits
for. Logs are written by a thread of their own (`igni_cloud/logs.py`):
in a VM standard output is the serial console, where every byte traps
to the host, and a line written from the event loop held every request
behind it. Not from a clock jump: a
quick come-back steps the clock by too little to tell. Each restore logs
one `resume timeline` line: every step's time from noticing the restore,
the window attaching, the first answer, collections, with the guest's
page faults — and, with `IGNI_RESUME_PAGES=1`, the memory touched since
the restore (`/proc/self/smaps_rollup` after clearing the referenced
bits), which is what a cold resume reads. `scripts/measure_resume.py`
plays restores against an igni in a Linux container (or measures one
already running, `--pid`), and measures what igni touches and changes
while idle. Every PostgreSQL
connection also has TCP keepalives, a 30 s user timeout and a 10 s
connect timeout, so a dead one the pool's pre-ping meets fails in seconds
and is replaced. **Timers** go by the wall clock where it matters: the
monotonic clock does not move while a VM is suspended, so an
`asyncio.sleep` armed before is late by the whole suspension. The
scheduler sleeps until its next task is due by the wall clock (at most
`poll_interval`; 300 s with nothing pending), publishes that time as a
wake (`wake_at`), and is re-armed — looks again at once — on a restore
and whenever a task is stored (`ActivityRegistry.rearm`). The model's
HTTP client is rebuilt after every run, and a run in progress keeps the
VM awake (`suspendable: false`).

**The warm base stays the base's.** A page a producer's igni writes to
becomes its own: stored at every suspend, read back on a cold resume.
CPython writes to an object whenever it takes or drops a reference to
it, computes a string's hash and a class's version tag on first use, and
writes a header into every object a collection walks — so merely running
igni turned base pages into the producer's own. When the warm base is
taken (`CloudGuest.listening`, just before `ready`), the collector
freezes what is there and every object alive is made immortal (CPython
3.12's immortal reference count, the one `None` has: taking a reference
writes nothing, and it is never freed — these objects lived as long as
the process anyway), with its strings' hashes and its classes' version
tags filled in (`igni_cloud/guest/immortal.py`). **Before a suspend**
the image's hook `packages/igni-cloud/hooks/before-suspend.d/50-igni`
(installed in the guest agent's `hooks/before-suspend.d/`) writes
`/run/igni/suspending` and waits at most 0.5 s for igni's answer in
`/run/igni/suspend-ready`: igni first closes every PostgreSQL connection
it holds — Agno's too; a node pauses about a thousand VMs, and each
producer's own login cannot share a pooler — then, when its allocator
holds 64 MiB free or more (a big run's leftovers), hands that back
(`malloc_trim(0)`), which the kernel then reports free and the snapshot
leaves out (`igni_cloud/guest/suspend.py`). Less is kept, because a page given
back and used again comes back as a fresh page — a guest fault and a
host fault each, never prefetched — and a usual turn's free heap is the
next turn's (trimming before every suspend cost every wake's first
requests ~3,200–3,500 such faults on the image). Nothing
reconnects on resume: the first query opens a fresh connection, and the
first answer needs none. Diagnostic (container, PostgreSQL with SCRAM):
1–3 connections held before, 0 paused; igni's steps 0.6 ms, the hook
9 ms; the first query after 6.9 ms against 2.3 ms on a held connection.
Every connection also asks the server to keepalive it (`options`:
`tcp_keepalives_idle=30`, 10 s apart, 3 probes), so a VM killed holding
one frees the server's within about a minute. glibc's allocator stays:
jemalloc and mimalloc changed more pages and held more memory.

**Idle is idle.** Every wake-up of an idle igni dirties memory the next
suspend must write. Once bound, igni has no periodic work of its own in
the cloud: `activity.json` is written when something moves (a heartbeat
runs only while not suspendable), the run directory (`producer.env`,
`restored`) is watched with inotify, the workspace poll sleeps 60 s
while no extension listens, the scheduler sleeps as above, and once the
backend has started the collector's view of everything built so far is
frozen (`gc.freeze()`), so collections while idle do not walk — and write
to — it. Left on timers: the extension health probe (30 s, when
extensions are installed), the session pool's idle sweep (5 min), and
the notification stream's read timeout while somebody is signed in.
Measured on macOS (no inotify there, so the run-directory watches poll):
60 s idle after a turn, 1,463 event-loop iterations, of which 655 were
those two polls and 58 the session's naming finishing. Measured in a
Linux container (`scripts/measure_resume.py`, PostgreSQL), 60 s idle
from 30 s after a turn: 0.47 MB of igni's pages changed and 2.8 MB
touched, under 10 ms of CPU. The plugin marketplaces' catalogs are
fetched at most every six hours (each fetch clones the marketplace's
repository and deletes it again), where every boot used to fetch the
default one twice.

**Agno's tables** keep Agno's schema; `ScopedAsyncPostgresDb` is a
wrapper. A session is stored under `<org>/<producer>/<project>#<id>` (in
the cloud the producer is the incarnation's, `person:<id>@<incarnation>`) and
handed back with the plain id, so two producers' sessions with one id
are two rows apart by the database's primary key, not by ids being
random; `user_id` carries the same scope and is forced on every session,
memory and learning read and write. Agno's other tables (metrics, evals,
knowledge, traces) are not scoped; igni does not write them. A shared
*SQLite* URL (tests) uses `AsyncSqliteDb`, unscoped.

**Memory files** are written with the agent's ordinary file tools — a
shell command as well as `save_file` — so the store is not a tool the
agent could go around: in the cloud the memory directory is a mirror of
the `memory_files` rows, filled at session start and compared with them
after every tool call the main agent makes and after every run; each
file written, changed or removed becomes the row's state.

**Attachments** are named by what the upload returned — a path on the
desktop, `attachment:<id>` in the cloud — and every reader goes through
the attachments store, which resolves either: the prompt builder turns
a reference into a readable file (a large object written out chunk by
chunk the first time) before the mentions, the vision models' media and
the agent's tools read it, and the file preview reads it the same way.

**Approvals do not expire** and resume their run however late, whoever
answers (a window, or an app through the host object model, which tells
a subscriber of each one with `chat.approvals.changed`):

- a run waiting only on a person — the main agent paused, or
  `spawn_agent` calls whose sub-agents are all paused — counts as
  *waiting*, not running; the host object model's `app.activity`
  (`suspendable`), written to `activity.json` (above), is what the node
  agent reads before suspending. The
  snapshot keeps the waiting run frozen, and the decision wakes it. A
  spawn's own timeout stops while it waits on a person;
- after a cold boot, the main agent's run resumes by its id from Agno's
  copy; a sub-agent's is continued from `waiting_spawns`, its answer is
  handed to the waiting `spawn_agent` call as the tool's result, and the
  main agent's run continues and streams to the session's windows
  (`backend/cold_spawn_resume.py`). The same path serves the desktop
  after igni is quit and started again;
- a *nested chain* (an agent spawned by a spawned agent) is continued
  level by level, each through the same stream handler as a live
  sub-agent: its progress reaches the window and it can ask again —
  deeper or at its own level — and that approval is stored with the calls
  still waiting above it;
- a *multi-agent `spawn_team`* is rebuilt (members, mode, leader model)
  and its stored run continued: Agno pauses a team only once every member
  delegated to in the turn has returned or paused, so the finished
  members keep their stored results and only the paused ones continue;
- a waiting call that *shared its turn* with other tool calls is
  continued with a complete turn: the calls that finished give their
  results (kept in `turn_results` by a tool hook on the main agent and
  on spawned agents), and calls still running at the restart are
  reported as interrupted, not run again — a call cut off may already
  have done what it does, and twice is worse than the model knowing;
- work *being continued* when the backend died — an approval answered,
  the agent or team still running — is reported failed to the call
  waiting on it, not re-run (same reason), and that call's run goes on
  when a window next attaches. The spawn link lives until the paused
  run's stream ends, which is how a restart tells this case apart. The
  main agent's own run is marked (`continuing`) while it is continued;
  one left marked is reported to the session once as interrupted, its
  stored run is closed in error instead of staying paused with nothing
  to answer, and the conversation goes on.

**Messages typed during a run** are a row from queueing; a run that
takes one marks it taken and records it in the conversation under the
row's id; the row is deleted once Agno's saved session holds that id. A
backend that starts offers again every taken message the session does
not hold: each is delivered into the conversation exactly once.

What is not covered yet:

- MCP approvals per person: igni server has no endpoint for them, so
  the cloud approves exactly the servers its group policy delivers. A
  per-person approval list needs one there (part of the igni server
  work).

## 10. The producer's volume (proposed, measured nested)

- **Automatic.** The node agent creates a producer's volume the first
  time the producer appears. Neither the guest, nor the org, nor an
  admin sets anything up.
- **The guest sees one disk and nothing else.** The volume is a
  disk-image file on managed network storage (Filestore on GCP, EFS on
  AWS) that only the **host** mounts; the VM gets it as `/dev/vdb`. The
  storage network has no route from the guest, so even root in VM1
  cannot learn that VM2's volume exists. A guest never mounts NFS
  itself — with root, it could reach other producers' files.
- **Encrypted per producer** on the host (dm-crypt, a raw key per
  producer from the platform's key service).
- **One VM at a time:** the gateway runs one VM per producer, and the
  node agent holds a lease on the volume so two nodes can never open it.
- **Moves freely:** a producer woken on another node opens the same
  volume; nothing is copied. Opening a file on network storage takes
  milliseconds and attaches nothing to the machine, so a node serves
  hundreds of volumes; cloud block disks (seconds to attach, capped per
  machine) are not used.
- **Layout:** the guest's writable root layer (installed packages,
  system changes) and the workspace, kept apart so an image upgrade can
  reset the layer without touching the workspace.
- **Node loss** is a power cut for the guest: it cold-boots from the base
  image and its volume. igni flushes the volume when a run ends and
  before every suspend to keep that loss small.
- **Later**, if managed network storage is outgrown: a block-storage
  cluster (Ceph) behind the same `/dev/vdb`.

Measured (`igni__cloudvm` `c72ae92`, nested, an NFS server VM in the same
subnet; 8 GB sparse image, loop device, dm-crypt `aes-xts-plain64`, p50 ms):

| | Suspend | Resume | First request |
|---|---|---|---|
| No volume, in memory | 9.5 | 4.5 | 51 |
| Volume, mapping kept on the node | 47 | 4.5 | 56 |
| Volume, mapped fresh (new node) | 44 | 27 | 54 |
| Volume, guest flushes before suspend | 62 | 4.5 | 55 |
| Volume, mapping kept, on disk | 57 | 15 | 162 |

- Resume is unchanged when the node keeps a recently suspended
  producer's volume mapped and leased; a move to another node adds
  23–44 ms (lock, loop device, dm-crypt).
- Suspend rises to 45–60 ms: Firecracker syncs the drive inside every
  snapshot, which pushes buffered volume writes to NFS. Nobody waits on
  it.
- Creating a volume on first use: about 80 ms. Cold boot from base and
  volume after a node loss: 2.2 s to the first request; packages
  installed as root and workspace files survived every cycle.
- File operations against local NVMe: small write plus sync 3.7 vs 1.3 ms,
  sequential write 207 vs 304 MB/s; `git clone` and `pip install`
  about the same (download-bound). Keep host readahead on.
- Isolation from guest root: only the base disk and its volume are
  visible; the storage server, the node, the internal network and the
  metadata server stay blocked even after root adds routes; a second
  open of a volume is refused. Every node agent must use the same
  `fcntl` lock.
- **The shared warm base snapshot must not contain a mounted producer
  volume** — restoring it over another producer's volume corrupts it. The
  base is taken with a placeholder disk, unmounted, and the producer's
  volume is swapped in after restore (not measured yet). Each VM needs
  its own drive path (Firecracker's jailer).

## 11. Open decisions

1. ~~**Row-level security** under the producer filter~~ — decided and built: per-producer roles (section 9).
2. **Extensions running inside the VM:** their data folder is scratch
   on the producer's volume and anything durable belongs in their
   service? (Remote extensions, the default, keep data in their
   service already.)
3. **Undelivered events:** how long they wait. Proposal: a time limit
   the service sets, at most 24 hours, then dropped and the service told.
4. **Unattended runs:** may an event start an agent run with nobody
   present, or only for apps (people get posts and notifications)?
5. **Startup network call:** the backend clones the default plugin
   marketplace (`DEFAULT_MARKETPLACES` in
   `src/ember_code/core/plugins/marketplace_store.py`) at every start,
   which breaks air-gapped installs. Remove the default?
6. **Linux arm64:** `ripgrep~=15.1.0` has no wheel there, so installing
   needs Rust. Fix before offering Graviton nodes?
7. **Deploy manifests:** the gateway, the node agent, per-node services
   and the shared base snapshot need Helm and compose definitions. This
   belongs with the delivery overhaul (Helm charts per server, an igni
   platform chart, compose, an offline bundle) that is already on the
   later TODO.
