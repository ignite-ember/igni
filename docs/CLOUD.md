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
| Settings, group policy, priority, the person's group, MCP approvals, extension settings | JSON and YAML under `~/.igni` | settings only from the image (command line, managed policy) and igni server (group policy); nothing under `~/.igni` or the workspace's `.igni`. Priority and the person's group from igni server's listing (`GET /v1/extensions`), kept in the process; MCP approvals are the servers igni server's group policy delivered, none approvable in the VM; extension settings a row per extension (`extension_settings`), scoped |
| Logs | files under `~/.igni` | JSON lines on standard output, collected by the node: no backend log, `debug.log`, `llm_calls.log`, `hitl_trace.log` or audit log file |
| Workspace files, `.igni` in the workspace, worktrees, process logs, installed packages | files | the producer's volume (section 10) |

**The cloud profile is one setting:** `IGNI_PROFILE=cloud`, set by the
image (`core/profile.py`). An environment variable rather than a setting,
because it decides where settings come from. It is what turns logs,
credentials and settings to the cloud's (above) and makes
`IGNI_DATABASE_URL` required; anything else is the desktop.

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

**Row-level security** (`core/db/rls.py`), so the isolation holds when
igni inside a VM is buggy or compromised:

- *Each producer connects as its own PostgreSQL role.* The role is bound
  to its org and producer in `producer_roles(role_name, org, producer)`,
  a table no producer role can read or write; the policies reach it only
  through `SECURITY DEFINER` functions keyed on `session_user` — the role
  that logged in, which `SET ROLE` cannot change. Nothing the VM can
  `SET` decides whose rows it sees. A mapping table rather than role
  names spelling the ids: org and producer ids are free-form and may not
  fit a role name, and an opaque name rotates without the data changing.
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

- `igni db grant-producer --org <org> --producer person:<id>|app:<id>`
  creates the producer's role, or rotates its password when it exists,
  and prints JSON: `role`, `password`, `database_url`. The platform hands
  `database_url` to the producer's VM as `IGNI_DATABASE_URL`, the way it
  hands the token. The org and producer must be the ones the producer's
  token names: the application's filter and the policy then agree, and a
  write in any other scope is refused by the database.
- `igni db revoke-producer --org <org> --producer <id>` drops the role.
  What it owned (its uploads' large objects) passes to the admin role;
  its rows stay, unreachable until a role is granted again.

**Token refresh contract** (the gateway's, build plan step 4). The VM's
environment names `IGNI_ACCESS_TOKEN_FILE`, a file holding the
producer's token. The gateway rewrites it — write a temporary file in
the same directory, rename it over — whenever it refreshes the token, and
after a resume before the VM is used. igni reads the file on every
credentials load, so a fresh token reaches a running VM without a restart
or any call into it. `IGNI_ACCESS_TOKEN` (fixed for the process) remains
for a short-lived VM.

**Guest contract** (igni's side of the guest protocol in
`igni__cloudvm`; `backend/guest/`, cloud profile only). Three
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
  `IGNI_PRODUCER`; only `IGNI_*` lines are taken into the environment.
  The backend then changes into the project directory again (the volume
  is mounted over it by then) and boots as it would have with the
  variables set from the start. A file rewritten as it was is nothing; one
  that comes to name another producer or database shuts igni down, for
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
binds and after every restore it notices (`ProcessEntropy`). The string
hash salt (`PYTHONHASHSEED`) is fixed at interpreter start and stays
shared; it only matters for hash flooding.

**After a restore** igni notices the wall clock jump ahead of the
monotonic one (the guest agent's `post_restore` steps it): it reseeds
(above), the database
pools are emptied, engines kept, so no query waits on a connection
dropped while the VM slept, and the notification stream reconnects
keeping its cursor. Every PostgreSQL connection also has TCP keepalives,
a 30 s user timeout and a 10 s connect timeout, so a dead one the pool's
pre-ping meets fails in seconds and is replaced. igni's timers (the
asyncio loop, the session pool's idle sweep) are monotonic and do not
fire in a burst; scheduled tasks are on the wall clock and run once each
when due, a recurring one resuming at its next occurrence. The model's
HTTP client is rebuilt after every run, and a run in progress keeps the
VM awake (`suspendable: false`).

**Agno's tables** keep Agno's schema; `ScopedAsyncPostgresDb` is a
wrapper. A session is stored under `<org>/<producer>/<project>#<id>` and
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
