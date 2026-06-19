"""Sustained-load memory profile for the BE.

Boots the real BE, opens N sessions, then hammers them with RPCs
continuously for ``--duration`` seconds while sampling RSS every
``--interval`` seconds. The point is to surface slow leaks: any
component that grows N bytes per request will show as a linear RSS
slope, even if the per-request leak is too small to spot in a single
run.

What's measured at each sample:
  * BE process-tree RSS (MiB)
  * RPCs completed since last sample (throughput)
  * RPC RTT p95 over the last window (latency)
  * Outstanding task count via ``ps`` (proxy for runaway tasks)

The summary at the end fits a linear regression to RSS-vs-time and
calls out a leak if the slope is meaningfully positive.

Run with::

    .venv/bin/python scripts/profile_memory_over_time.py \\
        --sessions 4 --duration 60 --interval 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import websockets


REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = REPO_ROOT / ".venv" / "bin" / "python"


async def _connect_and_welcome(port: int):
    ws = await websockets.connect(f"ws://127.0.0.1:{port}")
    raw = await asyncio.wait_for(ws.recv(), 10.0)
    welcome = json.loads(raw)
    assert welcome["type"] == "welcome", welcome
    return ws, welcome["client_id"]


async def _rpc(ws, method: str, args: dict | None = None, timeout: float = 5.0):
    req_id = f"rpc-{time.monotonic_ns()}"
    await ws.send(
        json.dumps(
            {
                "type": "rpc_request",
                "id": req_id,
                "method": method,
                "args": args or {},
            }
        )
    )
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError(f"rpc {method!r} timed out")
        raw = await asyncio.wait_for(ws.recv(), remaining)
        msg = json.loads(raw)
        if msg.get("id") == req_id:
            return msg


def _rss_mb(pid: int) -> float:
    """Total RSS for ``pid`` + descendants via ``ps``."""
    try:
        out = subprocess.run(
            ["ps", "-o", "rss=,pid=,ppid=", "-ax"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 0.0
    if out.returncode != 0:
        return 0.0
    tree: dict[int, tuple[int, int]] = {}
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            tree[int(parts[1])] = (int(parts[0]), int(parts[2]))
        except ValueError:
            continue
    if pid not in tree:
        return 0.0
    total_kb = tree[pid][0]
    seen = {pid}
    queue = [pid]
    while queue:
        parent = queue.pop()
        for p, (rss, pp) in tree.items():
            if pp == parent and p not in seen:
                seen.add(p)
                total_kb += rss
                queue.append(p)
    return total_kb / 1024.0


def _child_count(pid: int) -> int:
    try:
        out = subprocess.run(
            ["ps", "-o", "pid=,ppid=", "-ax"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 0
    tree = {}
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                tree[int(parts[0])] = int(parts[1])
            except ValueError:
                continue
    descendants = 0
    queue = [pid]
    seen = {pid}
    while queue:
        parent = queue.pop()
        for p, pp in tree.items():
            if pp == parent and p not in seen:
                seen.add(p)
                descendants += 1
                queue.append(p)
    return descendants


async def _run(sessions: int, duration: float, interval: float, rate: float) -> None:
    # Convert global rate (req/s) to per-session sleep between requests.
    per_session_sleep = sessions / rate if rate > 0 else 0.01
    print(f"# Target rate: {rate:.0f} req/s total  ({per_session_sleep*1000:.0f}ms/req/session)")
    project = Path(tempfile.mkdtemp(prefix="ember-memprof-"))
    print(f"# Project dir: {project}")

    t_spawn = time.monotonic()
    proc = subprocess.Popen(
        [
            str(PYTHON),
            "-m",
            "ember_code.backend",
            "--ws-port",
            "0",
            "--project-dir",
            str(project),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=REPO_ROOT,
        env={**os.environ, "EMBER_PARENT_PID": str(os.getpid())},
    )
    # Read ready line.
    ws_url = None
    while True:
        line = proc.stdout.readline()
        if not line:
            err = proc.stderr.read().decode()
            raise RuntimeError(f"BE exited before ready.\nstderr:\n{err}")
        try:
            envelope = json.loads(line.decode().strip())
        except Exception:
            continue
        if envelope.get("status") == "ready" and envelope.get("ws_url"):
            ws_url = envelope["ws_url"]
            break
    boot_secs = time.monotonic() - t_spawn
    port = int(ws_url.rsplit(":", 1)[-1])
    rss_initial = _rss_mb(proc.pid)
    print(
        f"# Cold boot: {boot_secs:.2f}s  ·  RSS at ready: {rss_initial:.1f} MiB"
    )

    # Open N sessions.
    clients = []
    for i in range(1, sessions + 1):
        ws, _ = await _connect_and_welcome(port)
        await _rpc(ws, "attach_session", {"session_id": f"sess-{i}"}, timeout=15)
        clients.append(ws)
    rss_attached = _rss_mb(proc.pid)
    print(
        f"# After {sessions} sessions attached: RSS {rss_attached:.1f} MiB "
        f"(+{rss_attached - rss_initial:.1f} MiB vs boot)"
    )

    # Drivers: each session fires get_status in a tight loop.
    stop = asyncio.Event()
    counters = {"completed": 0}
    rtt_window: list[float] = []
    rtt_lock = asyncio.Lock()

    async def driver(ws):
        while not stop.is_set():
            t0 = time.monotonic()
            try:
                await _rpc(ws, "get_status", timeout=10)
            except Exception:
                continue
            dt_ms = (time.monotonic() - t0) * 1000
            counters["completed"] += 1
            async with rtt_lock:
                rtt_window.append(dt_ms)
            # Pacing: ``--rate`` total RPCs/s across all sessions.
            await asyncio.sleep(per_session_sleep)

    print(
        f"# Driving for {duration:.0f}s ({sessions} sessions, sample every "
        f"{interval:.0f}s)…"
    )
    print(
        f"# {'t (s)':>6}  {'RSS (MiB)':>10}  {'Δ (MiB)':>8}  "
        f"{'rps':>6}  {'p95 ms':>7}  {'kids':>5}"
    )

    driver_tasks = [asyncio.create_task(driver(c)) for c in clients]
    t0 = time.monotonic()
    samples: list[tuple[float, float]] = []  # (t, rss)
    last_completed = 0
    deadline = t0 + duration
    while time.monotonic() < deadline:
        await asyncio.sleep(interval)
        t = time.monotonic() - t0
        rss = _rss_mb(proc.pid)
        kids = _child_count(proc.pid)
        completed = counters["completed"]
        rps = (completed - last_completed) / interval
        last_completed = completed
        async with rtt_lock:
            window = rtt_window[:]
            rtt_window.clear()
        if window:
            p95 = sorted(window)[int(len(window) * 0.95) - 1]
        else:
            p95 = 0.0
        delta = rss - rss_attached
        sign = "+" if delta >= 0 else ""
        print(
            f"# {t:>6.1f}  {rss:>10.1f}  {sign}{delta:>7.1f}  "
            f"{rps:>6.0f}  {p95:>7.2f}  {kids:>5d}"
        )
        samples.append((t, rss))

    stop.set()
    for t in driver_tasks:
        t.cancel()
    await asyncio.gather(*driver_tasks, return_exceptions=True)

    # Linear-regression slope on RSS vs time.
    if len(samples) >= 2:
        n = len(samples)
        xs = [s[0] for s in samples]
        ys = [s[1] for s in samples]
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        den = sum((x - mean_x) ** 2 for x in xs)
        slope_mb_per_s = num / den if den else 0.0
        slope_mb_per_min = slope_mb_per_s * 60
        first_rss = samples[0][1]
        last_rss = samples[-1][1]
        peak = max(ys)
        print()
        print(
            f"# RSS: start={first_rss:.1f} MiB  end={last_rss:.1f} MiB  "
            f"peak={peak:.1f} MiB"
        )
        print(
            f"# Slope: {slope_mb_per_min:+.2f} MiB/min  "
            f"({slope_mb_per_s * 1024:+.0f} KiB/s)"
        )
        # Verdict thresholds chosen from local-IPC scale: a real leak
        # producing N bytes/request at 100 req/s would show double-digit
        # MiB/min. Single-digit MiB/min on a 60-90 s window is noise.
        if abs(slope_mb_per_min) < 5:
            verdict = "HEALTHY — no measurable leak in this window"
        elif slope_mb_per_min < 0:
            verdict = "RSS shrinking — GC reclaiming, no leak"
        elif slope_mb_per_min < 20:
            verdict = (
                "MILD — small positive slope; could be GC noise, "
                "re-run with --duration 120 to confirm"
            )
        else:
            verdict = (
                f"WARN — RSS growing at {slope_mb_per_min:.0f} MiB/min "
                f"under sustained load; investigate"
            )
        print(f"# verdict: {verdict}")
        rps_overall = counters["completed"] / duration
        print(
            f"# Throughput: {counters['completed']} RPCs in {duration:.0f}s = "
            f"{rps_overall:.0f} req/s sustained"
        )

    for ws in clients:
        try:
            await ws.close()
        except Exception:
            pass
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sessions", type=int, default=4)
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--rate", type=float, default=400.0,
                    help="Target req/s across all sessions (default 400)")
    args = ap.parse_args()
    asyncio.run(
        _run(
            sessions=args.sessions,
            duration=args.duration,
            interval=args.interval,
            rate=args.rate,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
