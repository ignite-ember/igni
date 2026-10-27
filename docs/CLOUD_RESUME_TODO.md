# igni cloud — faster resume, TODO

Status: targets hot under 25 ms (50 ms at most), cold under 150 ms. igni's part of 1 and the idle-memory part of 2 done
and measured in a Linux container; on the image, still to measure. 3
done. Design: `docs/CLOUD.md`. Build plan:
`docs/CLOUD_BUILD_PLAN.md`. Measurements so far: `igni__cloudvm`
`RESULTS.md`.

## Where we are

Wake plus first request, p50, n=20, on a nested VM with the small test
guest (not igni), fscrypt state (`igni__cloudvm` `5fccca5`):

| State on | Resume hot | Resume cold | Suspend | First start |
|---|---|---|---|---|
| Local SSD | 44 ms | 195 ms | 70 ms | 559 ms |
| pd-ssd | 44 ms | 280 ms | 66 ms | 605 ms |
| Hyperdisk Balanced | 46 ms | 280 ms | 72 ms | 697 ms |
| pd-balanced | 46 ms | 384 ms | 82 ms | 677 ms |

- Hot: the producer's pages were still in the host's memory. Cold: the
  working set was read from disk.
- On the real igni image, igni's first WebSocket handshake after a resume
  is 363 ms (`igni__cloudvm` `9583c64`) — more than any storage difference
  above.
- igni leaves 90–163 MB of changed pages per producer; the test guest
  leaves ~40 MB. Real cold resumes read more than the table shows.

Storage choice is settled: local SSD by default; pd-ssd, Hyperdisk and
EBS as supported options (a network disk survives its node, so recovery
is simpler at +85 ms cold); pd-balanced not for node state.

## Target

Wake plus first answer at p50, measured at the gateway on the real igni
image with node state on local SSD:

| | Goal | Must not exceed | Today (test guest) |
|---|---|---|---|
| Hot (pages still in host memory) | under 25 ms | 50 ms | 44 ms |
| Cold (working set read from disk) | — | 150 ms | 195 ms |

Every number that counts against these targets comes from the real igni
image doing real work: a real model, real agent runs on a real repository
(reading and editing files, running shell commands and tests, git),
suspended between turns and resumed for the next. Test guests, fake
models and simulated runs are only for finding where time goes, never
for the result.

- Hot under 25 ms means igni's own part stays a few milliseconds and the
  restore path itself (Firecracker, UFFD, the node's stream, the gateway)
  is cut too; a per-phase timeline shows where the 44 ms goes.
- Cold under 150 ms needs a small working set read compressed and in
  parallel (items 2, 5); keeping pages in memory (item 4) makes more
  resumes hot. Each item reports how far it moved these.
- Out of scope for 50 ms: resumes from network disks or object storage
  (offload), and a producer's first start; they get their own numbers.

## TODO

Biggest gain first. Every item is measured on the real igni image, hot
and cold, before and after.

### 1. igni's first answer after a resume — igni

- [x] Profile everything igni does from `/run/igni/restored` to answering
      the first request (handshake, reseed, pools, notification stream,
      first RPC). Profiled in a Linux container that plays restores (stop,
      clear the referenced bits, continue, write `restored`, connect):
      `scripts/measure_resume.py`. On the image, igni logs one `resume
      timeline` line per restore — each step, the window attaching, the
      first answer, with faults, and with `IGNI_RESUME_PAGES=1` the memory
      touched since the restore — and the probe runs against a running igni
      with `--pid --ws --restored`. Found: every after-restore step ran
      before the handshake — closing the old PostgreSQL connections, the
      notification stream, the scheduler's two queries (re-armed timers),
      the activity file, the credential.
- [ ] Cut it; target is the 189 ms of the earlier image or better. igni
      side done: before the first answer only the reseed and an empty
      pool swapped in (a connection opened ahead in the background); the
      old connections close 2 s later; the rest waits for the first answer
      plus 0.5 s (1 s if nobody asks). Diagnostic only (Linux container,
      fake model, PostgreSQL, p50 of 12, client at once after `restored`): handshake 24.9 → 4.3 ms, first RPC
      26.8 → 5.2 ms, first database RPC 41.1 → 24.6 ms; memory touched
      restore → first database answer 28.7 → 24.2 MB. To measure on the
      image.
- [ ] Keep `1710f774` (reseed off the first-request path) and check it
      holds on the image. Kept (the reseed of known generators is 3 ms in
      the container; the rescan is 2 s later); the image check is open.

### 2. Read less on a cold resume — igni and node agent

- [x] igni: find what still dirties memory while idle and stop it, so the
      changed pages and the working set shrink. Diagnostic, measured in a
      Linux container by page checksums
      (Docker Desktop's kernel has no soft-dirty bits) and referenced bits,
      60 s idle 30 s after a turn: 0.44 MB changed, 2.8 MB touched before;
      0.47 / 2.8 MB after — idle was already quiet (no periodic work of
      igni's own; what wakes is the extension health probe, the
      workspace poll and, without a credential directory, the credential
      file's poll). What did dirty memory around activity: the after-restore
      steps (above), and the plugin marketplace catalogs fetched — a clone
      of the repository, written and deleted — twice on every boot; now at
      most every six hours.
- [ ] Node agent: store the working set compressed (lz4 or zstd at a fast
      level) and decompress during the UFFD prefetch; measure bytes read
      and resume time.

- [x] igni: attribute the 90 MB changed at binding (163 MB after a run) to
      modules and objects — Python's reference counting writes to every
      object it touches, so shared base pages become the producer's own —
      and cut it: touch less while binding, freeze or immortalise
      long-lived objects where Python allows. Attributed in a Linux
      container (`scripts/measure_resume.py --save-pages`, page checksums
      against the warm base, each changed 8-byte word mapped to the object
      and field it falls in): of igni's own process, base pages written
      were mostly one to eight words each — reference counts, string
      hashes computed on first use, class version tags, the collector's
      headers — then code objects' adaptive bytecode (specialisation
      counters, which CPython 3.12 cannot turn off) and dict keys'
      versions. Cut: at the warm base the collector freezes what is there
      and every object alive is made immortal (CPython 3.12's immortal
      refcount: taking a reference writes nothing), with its strings'
      hashes and its classes' version tags filled in
      (`igni_cloud/guest/immortal.py`, 0.3 s at base build). Diagnostic
      (container, fake model, PostgreSQL; igni's process only, not the
      VM's page cache): changed pages at binding 35.7–38.3 → 28.4 MB,
      after a turn 55.4–58.0 → 40.9 MB; RSS at `ready` unchanged (181 MB).
      Left: the adaptive bytecode and dict-keys versions — only running
      the code in the base would settle them (a dry-run bind while
      building the base: proposed, not done).
- [x] igni: the allocator — limit glibc's per-thread arenas or use
      jemalloc/mimalloc; return freed memory (`malloc_trim`) before
      suspend so it is not in the snapshot. Measure changed pages.
      Diagnostic (container, same run), changed MB at binding / after a
      turn / RSS at ready: glibc 28.4 / 40.9 / 181; `MALLOC_ARENA_MAX=2`
      28.5 / 40.9 / 181 (igni's few threads use few arenas); jemalloc
      37.6 / 49.5 / 200; mimalloc 30.3 / 41.2 / 205. glibc stays.
      `malloc_trim(0)` before suspend: 2.0 MB fewer changed pages after a
      turn — igni answers `/run/igni/suspending` from its before-suspend
      hook (`packages/igni-cloud/hooks/before-suspend.d/50-igni`, waits
      0.5 s at most), which the image must install in
      `hooks/before-suspend.d/`; pages the kernel then reports free
      leave the snapshot.
- [ ] igni: memory during a real agent run, and with several chats at once
      in one VM; confirm or change the 1 GiB default. The probe is ready:
      `scripts/measure_resume.py --model-url … --model-id … --model-key-env
      <variable> --repo-url <repository> --task "<a real task>" --chats 1
      --chats 3` clones the repository as igni's project, runs the task in
      that many sessions at once with edits and commands allowed, and
      samples igni's RSS and its whole process tree's (the commands the
      agent runs) every 200 ms, then the pages changed since the warm base.
      Not run yet: an agent running commands from a cloned external
      repository needs the person's approval here, and the counted run is
      the node agent's on the image anyway.
- [x] igni: what is imported at `ready` (~169 MB) that a producer may never
      use; weigh it against loading on the first request. Diagnostic
      (container, `PYTHONTRACEMALLOC`, `-X importtime`): 2,706 modules at
      `ready`; of 129 MB traced, 53 MB is module code (every module's code
      objects), then pydantic 14.5 MB, sqlalchemy 7.8, typing/dataclasses/
      enum 8.7, regex 3.4, pygments 2.4. Imported and rarely or never used
      by a producer: the whole `openai` SDK (537 modules, through
      `agno.models.openai` importing `openai.types`), `mcp.server` with
      uvicorn, sse-starlette, opentelemetry and multiprocessing (agno's MCP
      client imports the server side), rich, markdown-it and pygments
      (agno's logging and printing), `dns` (igni's sign-in discovery — never
      used in a VM) and `dateparser` with `regex`, pytz, dateutil and tzlocal
      (the scheduler's parser, through its models). Weighed: kept in the
      base. What the base imports is mapped by every VM from the same pages
      — host memory once per node, and nothing in a producer's snapshot or
      working set unless touched — while a module loaded on a first request
      becomes that producer's own pages and the request's latency. Loading
      later pays only for what almost no producer touches and would cost a
      lot to import: none of these, measured, beats keeping it shared. What
      would shrink the base is upstream: agno importing only the OpenAI
      chat client and not the MCP server, and igni's scheduler models not
      importing its parser.

### 3. Start reading earlier — gateway

- [x] Check how much of the resume already overlaps the client's
      connection and token check.
- [x] Start ensure-running as soon as the client's connection opens, in
      parallel with authentication; abandon it (suspend again) if
      authentication fails. Measured (`igni__cloudvm` `7607ad8`,
      `8b4570b`): with signing keys cached, auth and gateway work before
      the wake are under 1 ms, so nothing to overlap; pre-wake built only
      for when the signing keys must be fetched (first RPC 101.6 → 69.9 ms
      with a 30 ms key fetch), bounded (wake only, no bytes before the
      check, 30/min per address).

### 3b. The hot path itself — node agent and gateway

- [ ] Break the hot 44 ms into phases (gateway, node stream, Firecracker
      restore, UFFD faults, guest wake, igni) on the real image and cut the
      largest toward 25 ms.

### 4. Keep recently suspended producers in memory longer — node agent

- [ ] Replace the fixed `keep_mapped_s` timer with eviction by memory
      pressure: keep a suspended producer's pages in host memory while
      there is room, drop the least recently used first.
- [ ] Report the hot/cold resume ratio as a metric.

### 5. Read in parallel — node agent

- [ ] Fetch the working set with many reads in flight (io_uring or a
      thread pool) instead of mostly one after another; matters most on
      network disks.

## Then

Step 6 of the build plan (bare metal, real igni, a few hundred producers
per host) measures the improved version.
