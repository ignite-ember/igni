"""Quick multi-session BE profile.

Spawns the real BE, opens N WS clients each binding to its own
session, and measures:

* Cold boot time: process start → ``ready`` line on stdout
* RSS at ready
* Per-client RPC round-trip latency at idle
* Per-session creation cost (RAM + time) as sessions stack up
* RPC RTT distribution while N sessions are alive (the event-loop
  responsiveness gate — should be flat across N=1..N=K)

Doesn't run the agent loop (no LLM calls) — focuses on the BE's
dispatcher + session-pool overhead, which is what multi-session
correctness actually rides on.

Run with::

    .venv/bin/python scripts/profile_multi_session.py --sessions 6
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

import resource
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
        # Reply correlation is by ``id``; the BE may send either a
        # plain ``rpc_response`` or a typed message (e.g. ``StatusUpdate``)
        # echoed with the request id — both count.
        if msg.get("id") == req_id:
            return msg


async def _measure_rpc_latency(ws, *, samples: int = 10) -> dict:
    times_ms: list[float] = []
    for _ in range(samples):
        t0 = time.monotonic()
        await _rpc(ws, "get_status")
        times_ms.append((time.monotonic() - t0) * 1000)
    return {
        "p50_ms": round(statistics.median(times_ms), 2),
        "p95_ms": round(sorted(times_ms)[int(len(times_ms) * 0.95) - 1], 2),
        "max_ms": round(max(times_ms), 2),
        "n": samples,
    }


def _rss_mb(proc: subprocess.Popen) -> float:
    """Resident-set size of the BE process tree in MiB. Uses ``ps`` so we
    don't add a ``psutil`` dep just for this script."""
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
    # Build pid -> (rss_kb, ppid)
    tree: dict[int, tuple[int, int]] = {}
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            rss_kb, pid, ppid = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        tree[pid] = (rss_kb, ppid)
    # DFS from target.
    target = proc.pid
    if target not in tree:
        return 0.0
    seen = {target}
    total_kb = tree[target][0]
    queue = [target]
    while queue:
        parent = queue.pop()
        for pid, (rss_kb, ppid) in tree.items():
            if ppid == parent and pid not in seen:
                seen.add(pid)
                total_kb += rss_kb
                queue.append(pid)
    return total_kb / 1024.0


async def _drive(n_sessions: int) -> None:
    project = Path(tempfile.mkdtemp(prefix="ember-prof-"))
    print(f"# Project dir: {project}")

    print("# Spawning BE…")
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

    # Wait for the ready line.
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
    rss_at_ready = _rss_mb(proc)
    print(
        f"# Cold boot: {boot_secs:.2f}s  ·  RSS at ready: {rss_at_ready:.1f} MiB"
    )

    try:
        # ── Single-session baseline ──
        ws_first, _ = await _connect_and_welcome(port)
        # Attach to a session id; the BE returns the resolved id.
        await _rpc(
            ws_first,
            "attach_session",
            {"session_id": "session-1"},
            timeout=10,
        )
        single = await _measure_rpc_latency(ws_first, samples=15)
        print(
            f"# 1 session  ·  RPC RTT  p50={single['p50_ms']}ms  "
            f"p95={single['p95_ms']}ms  max={single['max_ms']}ms"
        )

        # ── Spin up additional sessions, one at a time ──
        clients = [ws_first]
        per_session_overhead = []
        for i in range(2, n_sessions + 1):
            t0 = time.monotonic()
            rss_before = _rss_mb(proc)
            ws, _ = await _connect_and_welcome(port)
            await _rpc(
                ws,
                "attach_session",
                {"session_id": f"session-{i}"},
                timeout=15,
            )
            elapsed_ms = (time.monotonic() - t0) * 1000
            rss_after = _rss_mb(proc)
            delta_mb = rss_after - rss_before
            per_session_overhead.append((elapsed_ms, delta_mb))
            clients.append(ws)
            sign = "+" if delta_mb >= 0 else ""
            print(
                f"# session {i:>2} create: {elapsed_ms:6.0f}ms  ·  "
                f"{sign}{delta_mb:.1f} MiB  ·  total RSS {rss_after:.1f} MiB"
            )

        # ── Stress: every session fires RPCs concurrently ──
        print(f"# {n_sessions} sessions, concurrent RPC RTT …")

        async def stress_one(ws) -> list[float]:
            times_ms = []
            for _ in range(10):
                t0 = time.monotonic()
                await _rpc(ws, "get_status")
                times_ms.append((time.monotonic() - t0) * 1000)
            return times_ms

        t_stress = time.monotonic()
        results = await asyncio.gather(*(stress_one(c) for c in clients))
        stress_secs = time.monotonic() - t_stress
        flat = [t for sub in results for t in sub]
        flat_sorted = sorted(flat)

        def pct(p: float) -> float:
            return round(flat_sorted[int(len(flat_sorted) * p) - 1], 2)

        print(
            f"# Under load ({n_sessions}× 10 RPCs in {stress_secs:.2f}s)  ·  "
            f"p50={pct(0.50)}ms  p95={pct(0.95)}ms  "
            f"p99={pct(0.99)}ms  max={round(max(flat), 2)}ms"
        )
        # Verdict on absolute RTT: anything under ~20 ms p95 means the
        # dispatcher is keeping up with concurrent load; over that
        # suggests a sync block is sneaking back in somewhere.
        p95 = pct(0.95)
        if p95 < 20:
            verdict = "HEALTHY (event loop stays responsive)"
        elif p95 < 100:
            verdict = "OK — some scheduler noise, no clear block"
        else:
            verdict = "WARN — RTT > 100 ms p95, look for a sync block"
        print(f"# verdict: {verdict}  (p95={p95}ms)")

        # Per-session memory summary
        if per_session_overhead:
            mems = [m for _, m in per_session_overhead]
            print(
                f"# Per-session RSS delta  median={statistics.median(mems):.1f}MiB"
                f"  max={max(mems):.1f}MiB"
            )

        for ws in clients:
            await ws.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sessions", type=int, default=6)
    args = ap.parse_args()
    asyncio.run(_drive(args.sessions))
    return 0


if __name__ == "__main__":
    sys.exit(main())
