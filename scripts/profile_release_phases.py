"""Trace RSS through load → idle → disconnect → GC.

Most leak detectors only catch *growth*. This script also looks for
the opposite — does the BE actually release memory when traffic
stops? Sessions, websocket conns, sqlite caches, agent state — all
should drop over time as references go away.

Phases:
  1. BOOT      — record baseline RSS at ready
  2. ATTACH    — open N sessions, baseline 2
  3. LOAD      — drive RPCs at --rate for --load-seconds
  4. QUIESCE   — stop driver, idle the BE for --quiesce-seconds
  5. DROP      — close every WS client; idle again briefly
  6. GC        — send SIGUSR1 (gc.collect) to the BE

Each phase prints the RSS plateau. A healthy BE should:
  * Plateau during QUIESCE (no slow leak)
  * Drop a bit during DROP (websocket buffers freed)
  * Drop more during GC (Python pools released)

Persistent post-DROP RSS that doesn't budge on GC means memory is
genuinely retained — either by a long-lived structure (e.g. the
session pool never evicts) or by a C extension that doesn't return
pages to the OS.

Run with::

    .venv/bin/python scripts/profile_release_phases.py \\
        --sessions 4 --rate 300 --load-seconds 20 --quiesce-seconds 15
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import websockets


REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = REPO_ROOT / ".venv" / "bin" / "python"


def _rss_mb(pid: int) -> float:
    out = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    return int(out) / 1024 if out.isdigit() else 0.0


async def _connect_and_welcome(port: int):
    ws = await websockets.connect(f"ws://127.0.0.1:{port}")
    raw = await asyncio.wait_for(ws.recv(), 10.0)
    return ws


async def _rpc(ws, method: str, args=None, timeout=5.0):
    req_id = f"rpc-{time.monotonic_ns()}"
    await ws.send(
        json.dumps(
            {"type": "rpc_request", "id": req_id, "method": method,
             "args": args or {}}
        )
    )
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError(f"rpc {method} timed out")
        raw = await asyncio.wait_for(ws.recv(), remaining)
        if json.loads(raw).get("id") == req_id:
            return


def _print_phase(label: str, pid: int, t0: float) -> float:
    rss = _rss_mb(pid)
    elapsed = time.monotonic() - t0
    print(f"# [{elapsed:>5.1f}s]  {label:<20}  RSS = {rss:>7.1f} MiB")
    return rss


async def _watch_phase(label: str, pid: int, t0: float, seconds: float,
                        sample_every: float = 2.0) -> tuple[float, float]:
    """Sample RSS through ``seconds`` of phase; return (start, end)."""
    start = _rss_mb(pid)
    elapsed_0 = time.monotonic() - t0
    print(f"# [{elapsed_0:>5.1f}s]  {label:<20}  start RSS = {start:>7.1f} MiB")
    end_time = time.monotonic() + seconds
    last = start
    while time.monotonic() < end_time:
        await asyncio.sleep(sample_every)
        last = _rss_mb(pid)
        elapsed = time.monotonic() - t0
        delta = last - start
        sign = "+" if delta >= 0 else ""
        print(f"# [{elapsed:>5.1f}s]    sample            "
              f"RSS = {last:>7.1f} MiB  ({sign}{delta:.1f})")
    return start, last


async def _drive(args) -> None:
    project = Path(tempfile.mkdtemp(prefix="ember-release-"))
    env_overrides = {**os.environ, "EMBER_PARENT_PID": str(os.getpid())}
    if args.idle_timeout > 0:
        env_overrides["EMBER_SESSION_IDLE_TIMEOUT"] = str(args.idle_timeout)
    # Force INFO-level logging in the spawned BE so the eviction log
    # lines reach us. Default config is WARNING — hides "session pool:
    # evicted …" lines, which is what we want to verify the drop.
    env_overrides["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [str(PYTHON), "-c",
         "import logging; logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s'); "
         "logging.getLogger('ember_code').setLevel(logging.INFO); "
         "import sys; sys.argv = ['ember_code.backend'] + sys.argv[1:]; "
         "from ember_code.backend.__main__ import main; main()",
         "--ws-port", "0", "--project-dir", str(project)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=REPO_ROOT,
        env=env_overrides,
    )
    ws_url = None
    while True:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("BE exited before ready")
        try:
            env = json.loads(line.decode().strip())
            if env.get("status") == "ready":
                ws_url = env["ws_url"]
                break
        except Exception:
            continue
    port = int(ws_url.rsplit(":", 1)[-1])
    t0 = time.monotonic()
    pid = proc.pid

    try:
        rss_boot = _print_phase("BOOT", pid, t0)

        # ── Phase 2: attach sessions ──
        clients = []
        for i in range(args.sessions):
            ws = await _connect_and_welcome(port)
            await _rpc(ws, "attach_session", {"session_id": f"s-{i}"},
                       timeout=15)
            clients.append(ws)
        rss_attached = _print_phase("ATTACHED", pid, t0)

        # ── Phase 3: LOAD ──
        per_session_sleep = args.sessions / args.rate
        stop = asyncio.Event()

        async def driver(ws):
            while not stop.is_set():
                try:
                    await _rpc(ws, "get_status", timeout=5)
                except Exception:
                    pass
                await asyncio.sleep(per_session_sleep)

        driver_tasks = [asyncio.create_task(driver(c)) for c in clients]
        load_start, load_end = await _watch_phase(
            "LOAD", pid, t0, args.load_seconds, sample_every=5.0
        )
        stop.set()
        for t in driver_tasks:
            t.cancel()
        await asyncio.gather(*driver_tasks, return_exceptions=True)

        # ── Phase 4: QUIESCE ──
        # No driver traffic; BE just sits with the sessions attached.
        quiesce_start, quiesce_end = await _watch_phase(
            "QUIESCE", pid, t0, args.quiesce_seconds, sample_every=3.0
        )

        # ── Phase 5: DROP — close every WS client ──
        for ws in clients:
            await ws.close()
        # Give the WS handler's ``finally`` block time to remove the
        # client from ``_conns`` on the BE side.
        await asyncio.sleep(0.5)
        drop_start, drop_end = await _watch_phase(
            "DROP", pid, t0, 10.0, sample_every=2.0
        )

        # ── Phase 6: explicit GC + evict_idle via SIGUSR1 ──
        # The handler schedules both ``gc.collect`` AND
        # ``pool.evict_idle()``. We give the BE 8s — eviction calls
        # ``backend.shutdown()`` on every idle runtime, which awaits
        # Agno's session-save tail and can take a beat. With 4
        # sessions × ~1s shutdown each = ~4s minimum.
        os.kill(pid, signal.SIGUSR1)
        # Wait long enough for ``backend.shutdown()`` × N runtimes to
        # complete (Agno's session-save tail can take a beat each).
        # Sample every 2s so we can see RSS drop during eviction, not
        # only at the end.
        for sec in range(2, 22, 2):
            await asyncio.sleep(2.0)
            rss_mid = _rss_mb(pid)
            elapsed_now = time.monotonic() - t0
            print(f"# [{elapsed_now:>5.1f}s]    post-USR1 sample  "
                  f"RSS = {rss_mid:>7.1f} MiB  (+{sec}s after signal)")
        rss_gc = _print_phase("AFTER GC+EVICT", pid, t0)

        # ── Summary ──
        print()
        print("# Phase-by-phase deltas:")
        print(f"#   BOOT         {rss_boot:>7.1f} MiB")
        print(f"#   ATTACHED     {rss_attached:>7.1f} MiB  "
              f"({rss_attached-rss_boot:+.1f})")
        print(f"#   LOAD start   {load_start:>7.1f} MiB")
        print(f"#   LOAD end     {load_end:>7.1f} MiB     "
              f"({load_end-load_start:+.1f})")
        print(f"#   QUIESCE end  {quiesce_end:>7.1f} MiB     "
              f"({quiesce_end-load_end:+.1f}, idle window)")
        print(f"#   DROP end     {drop_end:>7.1f} MiB     "
              f"({drop_end-quiesce_end:+.1f}, after disconnect)")
        print(f"#   AFTER GC     {rss_gc:>7.1f} MiB     "
              f"({rss_gc-drop_end:+.1f}, after gc.collect)")
        print()
        # Verdict
        net = rss_gc - rss_attached
        if net <= 5:
            print("# verdict: HEALTHY — BE returns to attached-baseline RSS after release")
        elif net <= 20:
            print(f"# verdict: OK — small persistent retention ({net:+.1f} MiB) "
                  "from pools/caches that legitimately hold state")
        else:
            print(f"# verdict: WARN — {net:+.1f} MiB retained vs ATTACHED baseline; "
                  "investigate what didn't release")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        # Dump any "evicted" / "session pool" log lines from stderr —
        # makes it easy to confirm whether eviction actually ran.
        try:
            stderr_tail = proc.stderr.read().decode()
            evict_lines = [
                line for line in stderr_tail.splitlines()
                if "evict" in line.lower() or "session pool" in line.lower()
                or "SIGUSR1" in line or "gc.collect" in line.lower()
            ]
            if evict_lines:
                print()
                print("# BE-side eviction/GC log lines:")
                for line in evict_lines[-30:]:
                    print(f"#   {line}")
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sessions", type=int, default=4)
    ap.add_argument("--rate", type=float, default=300.0,
                    help="Target req/s across all sessions during LOAD")
    ap.add_argument("--load-seconds", type=float, default=20.0)
    ap.add_argument("--quiesce-seconds", type=float, default=15.0)
    ap.add_argument(
        "--idle-timeout",
        type=float,
        default=0,
        help="Override EMBER_SESSION_IDLE_TIMEOUT in the spawned BE (seconds).",
    )
    args = ap.parse_args()
    asyncio.run(_drive(args))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
