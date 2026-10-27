# igni cloud — faster resume, TODO

Status: not started. Design: `docs/CLOUD.md`. Build plan:
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

## TODO

Biggest gain first. Every item is measured on the real igni image, hot
and cold, before and after.

### 1. igni's first answer after a resume — igni

- [ ] Profile, on the real image, everything igni does from
      `/run/igni/restored` to answering the first request (handshake,
      reseed, pools, notification stream, first RPC).
- [ ] Cut it; target is the 189 ms of the earlier image or better.
- [ ] Keep `1710f774` (reseed off the first-request path) and check it
      holds on the image.

### 2. Read less on a cold resume — igni and node agent

- [ ] igni: find what still dirties memory while idle and stop it, so the
      changed pages and the working set shrink.
- [ ] Node agent: store the working set compressed (lz4 or zstd at a fast
      level) and decompress during the UFFD prefetch; measure bytes read
      and resume time.

### 3. Start reading earlier — gateway

- [ ] Check how much of the resume already overlaps the client's
      connection and token check.
- [ ] Start ensure-running as soon as the client's connection opens, in
      parallel with authentication; abandon it (suspend again) if
      authentication fails.

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
