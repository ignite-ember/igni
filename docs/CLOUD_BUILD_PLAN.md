# igni cloud — build plan

Status: plan, nothing below is built unless it names a commit. Design:
`docs/CLOUD.md`. Proof of concept: the `igni__cloudvm` repository
(`RESULTS.md`).

Sizes are rough: **S** about a day, **M** a few days, **L** one to two
weeks, for one person with agents.

## Order

```
1 PostgreSQL in igni ──┐
2 Guest image ─────────┼──► 4 Gateway ──► 6 End-to-end on bare metal
3 Node agent ──────────┘        ▲
5 igni server pieces ───────────┘
7 Deploy manifests (with the delivery overhaul)
```

1, 2, 3 and 5 can run in parallel; 4 needs 2 and 3; 6 needs everything
before it.

## 1. PostgreSQL in igni (L) — needed whatever else changes

The desktop keeps SQLite and its files; the backend chooses by
configuration. One code path. What is built and what is not:
`CLOUD.md` section 9.

| Step | Size | Status |
|---|---|---|
| Database URL in settings instead of a file path; engine registry, `Database`, migrations and `igni migrate` keyed by URL; the revision read through SQLAlchemy, not `sqlite3` | M | built, `ccbca59` |
| The four raw `sqlite3` stores (pending messages, session preferences, session directories, client state) and local notifications moved onto SQLAlchemy and alembic, no SQLite-only syntax | M | built, `ccbca59` |
| Agno storage chosen by URL: `AsyncSqliteDb` on the desktop, `AsyncPostgresDb` in the cloud | S | built, `ccbca59` |
| Org, producer and project columns on every row in the cloud; every query filtered by them (row-level security if decided) | M | built, `ccbca59`; Agno's session key scoped too, `1e231c4`; row-level security with per-producer roles and `igni db grant-producer`, `f6a932b` |
| A store interface for `MEMORY.md` memories and attachments: files on the desktop, PostgreSQL (large objects for attachments) in the cloud | M | built, `ccbca59`; the agent's memory writes stored through a write-through mirror, `7fc48b0`; every attachment reader through the store, `66b6a36` |
| Approvals waiting on the user and messages typed during a run written as they happen | M | built, `9ef5eda` — on the desktop too; a queued message taken, then deleted once the conversation holds it, `dac89aa`; approvals resume later — waiting counts as idle (`app.activity`), sub-agents continue after a cold boot, `chat.approvals.changed` for apps, `7c94a2a`; nested chains stream and ask again, `fa78797`; a cut turn continues complete, `4ece789`; paused teams continue and a cut-off continuation is reported, `23fdd06` |
| Logs to standard output in the cloud; credentials and settings not read from `~/.igni` there | S | built, `7a99d1a`: the cloud profile is `IGNI_PROFILE=cloud`; priority, MCP approvals and extension settings from igni server and the database, `3cf7bfe`; refreshed token through a file the gateway writes, `21aac46` |
| CI runs the suite against SQLite and PostgreSQL | S | built, `ccbca59`: the suite on SQLite, the storage, restart and row-level security tests also on PostgreSQL (`test-postgres` job) |

## 2. Guest image (M)

- Minimal Linux with the guest kernel options the proof of concept used
  (free page reporting, VMGenID), igni installed, igni running as root.
- Read-only base root plus an overlay whose writable layer is on the
  producer's volume; the workspace on the same volume, kept apart.
- After restore: resync the clock, reseed randomness, reconnect to igni
  server and services; flush the volume when a run ends and before
  suspend; close heavy caches (the browser) when the profile says so.
  igni's side built, `dbffea2`: it notices the clock jump, empties its
  database pools and reopens the notification stream (`CLOUD.md` section
  9, guest contract).
- Free page reporting through Firecracker's balloon at order 3 (sysfs,
  after boot); an idle-time cgroup v2 `memory.reclaim` trimmer; no
  `drop_caches`, no compaction; MGLRU in future kernels.
- The warm base snapshot built from it, one per image version. igni's
  side built, `dbffea2`: `/run/igni/ready` once it listens, and late
  binding to the producer `/run/igni/producer.env` names, refusing
  requests until then; what a bound backend imports is imported before
  `ready`, so it is in the snapshot (`WarmImports`).
- Profile layers: a profile's package list built into a signed read-only
  layer from the customer's mirror, with a warm snapshot per profile.
- The guest agent (vsock): flush, reseed, cache trim, hinting, and
  running commands as root with streamed output and exit code.

## 3. Node agent (L) — grows out of `cvm`

A Rust service, one per host:

- Starts, suspends and restores Firecracker VMs: journal append in
  process, background merge, UFFD with working-set prefetch, pre-spawned
  Firecracker processes.
- One network namespace per VM, fixed host tap MAC.
- Volumes: create on first use, map with the producer's key, lease so
  only one node opens a volume, hand the VM a single disk.
- Profiles: ceiling and CPUs at boot, `idle_before_suspend`, cold boot
  when the ceiling changes. igni writes `/run/igni/activity.json`
  (`suspendable`, running runs, waiting approvals, connected clients),
  `dbffea2`; with what holds it busy (`busy`: calls out, scheduled tasks,
  processes, monitors, extensions' `ext.busy`) and when to wake it
  (`wake_at`). The node agent should wake a suspended VM at `wake_at`.
- Memory: watch real usage, reclaim, refuse placements when short;
  under pressure hint and trim the largest VMs first. Patched
  Firecracker (discard-aware diffs, `.discard` sidecar journaled and
  punched on apply, seccomp allowing the pagemap read); before suspend:
  trim, one hinting run, journaled suspend.
- An API the gateway calls: ensure running, suspend, status, run a
  command in a VM (through the guest agent), audited.
- Per-producer PostgreSQL logins: create on first use, hand to the VM
  with the token (`/run/igni/producer.env`, which igni binds to,
  `dbffea2`; `IGNI_PRODUCER` is `person:<id>`, as the token names it).

## 4. Gateway (M)

- Authenticates people (igni token) and apps (`private_key_jwt`).
- Finds the producer's node and VM, wakes it, holds the client's
  connection while it restores, routes the host object model over WSS.
- Places new producers on hosts with free memory; moves a producer when
  its node is gone (cold boot from its volume).
- Hands the VM the producer's token.
- Hands a producer's events to its igni: pulls them, calls
  `deliver_service_events` with them, and acknowledges what igni answers
  in `acknowledge` (`CLOUD.md` section 6).

## 5. igni server pieces (M)

- Apps as producers: registration on the portal, client credentials,
  their enabled extensions.
- VM profiles on the portal: define, assign to a group or an app,
  platform caps, quota.
- Event delivery to a producer that wakes its VM (`CLOUD.md` section 6).
  igni's side built: `deliver_service_events` delivers each event once
  (by id, across restarts), asks the extension (`@ext.on_service_event`)
  and starts the producer's run, or queues it for the run going. Desktop
  delivery waits on a person-facing pull on igni server
  (`/v1/me/events/pull` and `/ack`).
- One group per person (built: ember-server `c70d3fd`, igni `918e22d`).
- Profile packages on the portal; commands in VMs: the producer in its
  own VM, org admins in their org, the super admin anywhere; jobs for
  many VMs applied at each VM's next wake; the audit log.

## 6. End to end on bare metal (M)

The real igni backend as the guest, a realistic agent run between
suspends, a few hundred producers on one host: suspend, resume, first
request, memory per producer, node loss. This replaces the toy-server
numbers in `CLOUD.md` section 8.

## 7. Deploy manifests

Gateway, node agent, per-node services, image and base snapshot as Helm
and compose definitions. Belongs with the delivery overhaul already on
the later TODO.

## Measurements still running or queued

- Per-producer volumes on network storage (running).
- Freed memory returned to the host and its effect on snapshot and
  resume (queued after the volumes).
