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
| Memory ceiling and CPUs | 256 MB, 1 vCPU | 4 GB, 2 vCPU |
| `idle_before_suspend` | 10 s | 10 min, or never |
| Drop caches before suspend (e.g. close the headless browser) | — | yes |

- The platform sets the maximum ceiling and the longest timeout; time
  awake counts against the org's quota.
- A long `idle_before_suspend` is for producers whose suspend would be
  large: a headless browser dirties hundreds of MB to GBs, so suspending
  and resuming it between closely spaced requests costs more than
  staying awake.
- A new timeout applies at once; a new ceiling at the producer's next
  cold boot (a memory snapshot keeps the size it was taken with). The
  node agent forces that cold boot at the next suspend; nothing is lost,
  since durable state is in PostgreSQL and on the volume.

### Elastic memory: a hard ceiling, real usage only

- **The ceiling is fixed at boot** and never exceeded.
- **Growing needs no request.** The host backs only the pages the guest
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
- Gaps being worked on: fragmented free memory (pieces under 2 MiB, as
  Chromium leaves it) is not returned — about 1 GB stayed on the host
  after the browser closed; freed pages still land in the suspend diff (a
  guest that grows and shrinks 1 GiB between suspends writes a 1.1 GB
  diff, about 5 s), to be fixed by a Firecracker patch that leaves
  reported pages out of the dirty log; the guest's page cache needs its
  own trim.

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
data. Proposed safety net: PostgreSQL row-level security keyed on the
producer, set per connection, so a missing filter returns nothing
instead of someone else's rows (open decision).

What moves where (audit of `src/ember_code`, 2026-10-26; built in
`ccbca59` and `9ef5eda` unless the row says otherwise):

| State | Desktop (unchanged unless noted) | Cloud |
|---|---|---|
| Sessions, transcripts, runs, `session_data` (event log, todos, plan decisions), memories, learning, sub-agent sessions | Agno `AsyncSqliteDb` in the project's `state.db` (`core/memory/manager.py`) | Agno's `AsyncPostgresDb` as `ScopedAsyncPostgresDb`, on igni's engine: the scope is Agno's `user_id` |
| Schedules, `/loop` state, background processes | alembic tables in `state.db` | the same tables, scoped |
| Pending and interrupted messages, model per session | alembic tables in `state.db` (were raw `sqlite3`) | the same tables, scoped |
| Session → directory, window state and extensions' own state, local notifications | alembic tables in one global database in the data directory (`global_db_path`; were `sessions.db`, `client_state.db`, `notifications.db`) | the same tables, scoped |
| Approvals waiting on the user (main agent, auto-resolved, sub-agents), messages typed during a run | `waiting_approvals` and `queued_messages` in `state.db` (were memory only) | the same tables, scoped |
| `MEMORY.md` memory files | files under `~/.igni/projects/` | `memory_files` rows — read; the agent still writes files (below) |
| Uploaded attachments | `<project>/.igni/attachments/` | PostgreSQL large objects, written and read in chunks, an `attachments` row each |
| Credentials | `~/.igni/credentials.json` | not stored: the gateway hands the VM the producer's token (not built) |
| Settings, group policy, priority, the person's group, MCP approvals, extension settings | JSON and YAML under `~/.igni` | delivered by igni server or the image; read-only in the VM (extension settings per producer go to PostgreSQL) (not built) |
| Logs | files under `~/.igni` | standard output, collected by the node (not built) |
| Workspace files, `.igni` in the workspace, worktrees, process logs, installed packages | files | the producer's volume (section 10) |

**How it is chosen.** One code path; `storage.database_url`
(`IGNI_DATABASE_URL`) picks the backend. Unset, a project's state is its
`state.db` and what is no one project's is the global database in the
data directory, as before. Set,
both are that one database and every row is scoped. The URL may be any
spelling of PostgreSQL (`postgresql://`, `postgres://`,
`+asyncpg`, `+psycopg`); igni speaks psycopg 3 to it, sync and async,
one driver for alembic, its stores and Agno (the `postgres` extra).
`igni migrate` (`--database-url`) prepares it; a backend whose schema is
current still never imports alembic.

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
session is in — filters by org and producer only. Row-level security is
not built; adding it means setting the scope per connection where
`StorageScope` is applied.

What is not covered yet:

- **Agno's tables** have no scope columns. `user_id`
  (`<org>/<producer>/<project>`) is forced on every session, memory and
  learning read and write. Their key is still `session_id` alone, and
  igni's ids are eight hex characters: Agno will not overwrite another
  scope's session with a colliding id, but the colliding one is not
  saved. Agno's other tables (metrics, evals, knowledge, traces) are not
  scoped; igni does not write them. A shared *SQLite* URL (tests) uses
  `AsyncSqliteDb`, unscoped.
- **Memory files**: the session reads `MEMORY.md` from the database, but
  the agent writes memory files with its file tools, to paths on the
  volume. A memory tool or a write-through is needed.
- **Attachments**: an upload's reference in the cloud is
  `attachment:<id>`, not a path; what reads an attachment by path does
  not read that yet.
- **Approvals** come back after a restart and resume their run from
  Agno's stored copy; a sub-agent's request comes back and can be
  answered, but the `spawn_agent` call that waited on it died with the
  backend, so nothing resumes on the answer. A message a run took and
  then died with is gone with that run (delivered once, never twice).

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

1. **Row-level security** under the producer filter (section 9).
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
