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
      leave the snapshot. **Changed (item 3b, this round): only when 64 MiB
      or more is free** — trimming before every suspend cost every
      wake's first requests a fresh page fault per page given back
      (~3,200–3,500 guest faults over ~255 ms on the image).
- [x] igni: memory during a real agent run, and with several chats at once
      in one VM; confirm or change the 1 GiB default. Diagnostic (Linux
      container on Apple Silicon, MiniMax-M2.7, PostgreSQL; the counted
      run is the node agent's on the image): `scripts/measure_resume.py
      --model-url … --model-key-env … --repo-url
      https://github.com/ignite-ember/igni --task … --chats 1 --chats 3`.
      The task: edit a file, make a virtualenv, `pip install -e .`, run a
      test module. All runs ended (`stream_end`).

      | | 1 chat | 3 chats at once |
      |---|---|---|
      | took | 138 s | 85 s |
      | igni's RSS, peak | 205 MB | 219 MB |
      | igni and every process it ran, peak (RSS summed) | 1,912 MB | 753 MB |

      igni itself stays near 220 MB (181 MB at `ready`); its changed pages
      since the warm base after both runs were 63 MB (28 MB base pages
      written, 35 MB new). **1 GiB holds for igni and ordinary commands,
      not for every command an agent runs**: the 1.9 GB peak was the first
      `pip install -e .` building a dependency from source (ripgrep has no
      wheel for aarch64 Linux, so cargo and rustc ran); the later runs
      reused the built wheel and peaked at 0.75 GB with three chats. RSS
      summed over processes counts shared pages more than once, so the
      true peak is lower — but a build like that in a 1 GiB VM would meet
      the OOM killer. Decided: every VM boots with a 4 GiB ceiling and runs
      at a 1 GiB normal size held by the balloon, which the node agent
      deflates while the guest needs more and the node has room
      (`igni__cloudvm/docs/node-agent.md`, results/balloon/).
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
- [ ] igni's share (`igni__cloudvm` `1be6114`, diagnostic: ~91 ms of 120 —
      29 ms to notice the restore, 62 ms more to answer; its timeline:
      reseed 4–8 ms and the pool swap 7–13 ms on the loop before the
      window attached 27–30 ms later, the answer 16–19 ms after that).
      Cut on igni's side (ember-code after `fa3e6371`), diagnostic in a
      Linux container until the image measures it:
      - nothing runs before the first answer but marking the pools'
        connections stale (0.3 ms; no swap, no closing, no connection
        opened ahead — a query's checkout recycles a stale one);
      - the reseed runs the moment the first answer is out;
      - logs are written by a thread, not the event loop: igni's standard
        output in the VM is `console=ttyS0`, the serial console, where
        every byte traps to the host — a JSON line written from the loop
        (one per window attaching) held every request behind it. The node
        agent should also give igni's output a faster sink than ttyS0
        (a pipe to the guest agent, or virtio-console).
      Container, client 30 ms after `restored`: window attached at
      34.2 ms, first answer 0.7 ms later; with the client at once, first
      answer 3.3 ms after the restore (was 4.3 with the swap, 24.9 before
      the last round), first database answer 18 ms.
- [x] Profile igni from the restore to the first answer on the real path
      (connect as the gateway does, igni's before-suspend hook before each
      pause, a session with some work behind it) and take off the answer's
      path what it does not need. Diagnostic, Linux container on Apple
      Silicon (Docker Desktop, 2 CPUs), fake model (6 turns of 300 kB
      replies), PostgreSQL, p50 of 12, client at once after `restored`:
      `scripts/measure_resume.py --idle 0 --resumes 12 --connect-after-ms 0
      --turns 6 --reply-kb 300` (`--no-clear-referenced` for fresh faults
      only). "Pages touched" are igni's page faults with the referenced
      bits cleared at the stop (on arm64 every first touch is then a
      fault); on a restored VM each is a UFFD fault until the prefetch has
      the page. Found:
      - **igni's first answer is mostly the WebSocket handshake's pages.**
        Restore → welcome ~2,370 pages, → first answer ~2,800 (~11 MB):
        the websockets server, asyncio and igni's handler and dispatch
        (~460 distinct Python functions, libpython's own code); the
        restore's own handling ~200–300 of them. The whole path is 2–3 ms
        of igni's CPU, in two dozen callbacks of which the handshake's is
        the longest (half the calls); igni's CPU ≈ its wall time (it never
        waited). On the image
        that is the gateway's `igni_handshake` (p50 57–65 ms from a
        snapshot, 4–8 ms from paused with everything resident): each page
        a fault while the prefetch runs. igni can shave it, not remove it:
        what serves it is the working set reaching those pages first.
      - **"Noticed 139 ms after the restore" is not igni's loop.**
        `restored`'s `at_ms` is the host's clock when it sent
        `post_restore` (guest agent `ops.rs`), not when the file was
        written. The timeline now splits it: `written_after_ms` (host →
        agent → file, by its modification time) and `woke_after_write_ms`
        (file → igni's loop reading the event), and gives every step igni's
        CPU time (`cpu_ms`: far below `at_ms`, igni was waiting).
      - **The ~3,500 page faults over 255 ms after the answer were the heap
        trim before the pause.** Every pause gave the allocator's free heap
        back (8–27 MB here), and the first requests after the wake
        allocated it again, one fresh page — a guest fault and a host
        zero-page fault — at a time. Reproduced: first answer → first
        database answer 4,311 fresh faults with the trim, 5 without.
      - **A collection during the burst** reads the header of everything
        young objects point to: on the image gen-0 collections of
        10–53 ms and a gen-2 of 130 ms among the first requests.
      - **Three inotify watches on `/run/igni`**, each woken by every file
        written there: a restore also ran the binding's and the suspend
        watch's look, and igni's own `activity.json` (each window
        attaching, each activity change) woke all three.
      - **The log writer thread** was woken by the window attaching's line,
        taking the interpreter's lock from the loop during the handshake.

      Cut (ember-code, this commit): one inotify watch per directory, each
      file's waiter woken by its own name only; the collector off from
      noticing the restore until the burst is through (the later steps
      begin: answer + 0.5 s, or 1 s with no answer); the log writer held
      (records kept, in order, with their times) until then; the window's
      "attached" line after its welcome; the timeline's records built when
      it is logged, not while marking; the heap trimmed only from 64 MiB
      free (`free_heap_kb` in `suspend-ready`). What is kept stays in a
      paused VM (19–45 MB free heap here, before: trimmed to ~0): the
      trim's threshold is first-request latency against the paused
      tier's density, for the counted runs to settle. glibc's own
      trimming still gives some back between wakes (one run: 5 fresh
      faults on each first database answer, another 1,320); fixing its
      thresholds with `mallopt` (32 MiB, with the trim's threshold there
      too) stopped that but let the free heap grow past the trim's
      threshold, so every suspend trimmed (5,400 faults): not kept.

      | p50, n = 12 | before | after |
      |---|---|---|
      | restore → welcome, pages touched | 2,374 | 2,173 |
      | restore → first answer, pages touched | 2,802 | 2,594 |
      | igni's timeline, noticed → first answer, pages | 1,795 | 1,609 |
      | first answer → first database answer, fresh page faults | 4,311 | 5 |
      | free heap at the before-suspend answer / trimmed | trimmed 8–27 MB | 39–42 MB, kept |
      | collections before the burst is through | gen 0 at 33–46 ms | none |
      | restore → welcome / first answer / first database answer (ms, container) | 2.0 / 2.8 / 42.7 | 1.6 / 2.5 / 40.7 |

      The container's times barely move — its memory is resident; the
      gain is in pages and faults, which the image pays for. Next: the
      node's counted run on this igni (`igni_handshake`, first database
      answer, the timeline's new splits). Left on igni's side: the
      handshake's own pages — a dry-run bind and handshake while building
      the warm base would make them base pages, shared and likelier
      resident (proposed, not done).
- [x] The warm base practises its first answer (proposed above; counted
      on GCP with `f1fc03fb` first: igni handshake ~31 ms at the gateway,
      noticed 8.1 + 25.1 ms after the resume, answered 22.1 ms later,
      faults after the first answer 2,700 → 843, paused host RSS +32 MiB,
      wake times within noise: hot 62 vs 53 ms, cold 218 vs 193 ms).
      Before `ready`, a warm base's igni (no producer bound) connects to
      its own listener on `127.0.0.1` as the guest agent's `forward` does
      for the gateway, without compression, takes the handshake and
      `welcome`, sends `get_session_id`, gets it answered as every request
      is before binding, and closes; the transport forgets the connection
      (the first window is still awaited, the event sequence starts over),
      then the collector runs, the base is made immortal and frozen, and
      `ready` is written (`igni_cloud/guest/practice.py`,
      `CloudGuest.listening`). Needs no producer, token or database;
      bounded at 5 s and logged either way (`first answer practised` /
      `not practised`, with `welcome_ms`, `answer_ms`). The node needs no
      new call: `ready` now comes after the practice. Past the transport
      — the dispatch to a session — needs a producer and is not practised.
      Diagnostic (container, as above, two runs each, p50 of 12, with
      `--fault-trace`: igni's page faults by address through a kprobe, so
      each touched page is the base's unchanged, igni's own, or a file's):

      | pages touched | without practice | with |
      |---|---|---|
      | restore → welcome, all | 2,155 / 2,173 | 2,135 / 2,173 |
      | of them igni's own (not the base's) | 743 / 743 | 706 / 714 |
      | restore → first answer, all | 2,652 / 2,663 | 2,595 / 2,669 |
      | of them igni's own | 940 / 944 | 885 / 903 |
      | changed since the base at binding, MB | 29.5 / 29.5 | 28.6 / 29.1 |

      A small gain (~5% fewer own pages on the answer's path): most of the
      ~900 own pages it touches are not the handshake's code but memory
      igni allocated after the base — the bind, the session, the objects
      of earlier connections — which no practice in the base can share.
- [x] `list_sessions` "never answered within 60 s" through the gateway
      (realwork harness): igni answers it — as a typed
      `session_list_result` carrying the request's id, like every RPC whose
      handler returns a protocol message (`get_status` too); clients
      correlate answers by id (`clients/web/src/protocol/client.ts`). The
      harness waited for `type == "rpc_response"` only. Fix the harness:
      match the id, whatever the type.
- [ ] The first answer on resumes that were not evicted took 1.24–1.39 s at
      the gateway, 0.46–0.51 s on evicted ones. igni's own timelines from
      those runs show the same 40–70 ms from noticing the restore to its
      answer in both; the extra ~0.8 s is before igni sees the connection
      (`welcome_ms` 1.22–1.37 s vs 0.45–0.49 s), on the node's or the
      gateway's side of a kept (not evicted) VM's resume.

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
