# igni cloud — faster resume, TODO

Status: target 50 ms. igni's part of 1 and the idle-memory part of 2 done
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

Wake plus first answer under **50 ms** at p50, measured at the gateway on
the real igni image with node state on local SSD.

- Hot (pages still in host memory): 44 ms with the test guest today, so
  igni's own part must stay a few milliseconds.
- Cold (working set read from disk): 195 ms with the test guest today.
  Under 50 ms needs a small working set read compressed and in parallel
  (items 2, 5), and most resumes made hot by keeping pages in memory
  (item 4). Each item reports how far it moved this.
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
      plus 0.5 s (1 s if nobody asks). Container, PostgreSQL, p50 of 12,
      client at once after `restored`: handshake 24.9 → 4.3 ms, first RPC
      26.8 → 5.2 ms, first database RPC 41.1 → 24.6 ms; memory touched
      restore → first database answer 28.7 → 24.2 MB. To measure on the
      image.
- [ ] Keep `1710f774` (reseed off the first-request path) and check it
      holds on the image. Kept (the reseed of known generators is 3 ms in
      the container; the rescan is 2 s later); the image check is open.

### 2. Read less on a cold resume — igni and node agent

- [x] igni: find what still dirties memory while idle and stop it, so the
      changed pages and the working set shrink. Measured by page checksums
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

- [ ] igni: attribute the 90 MB changed at binding (163 MB after a run) to
      modules and objects — Python's reference counting writes to every
      object it touches, so shared base pages become the producer's own —
      and cut it: touch less while binding, freeze or immortalise
      long-lived objects where Python allows.
- [ ] igni: the allocator — limit glibc's per-thread arenas or use
      jemalloc/mimalloc; return freed memory (`malloc_trim`) before
      suspend so it is not in the snapshot. Measure changed pages.
- [ ] igni: memory during a real agent run, and with several chats at once
      in one VM; confirm or change the 1 GiB default.
- [ ] igni: what is imported at `ready` (~169 MB) that a producer may never
      use; weigh it against loading on the first request.

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
