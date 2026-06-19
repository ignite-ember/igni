"""Watch FD count across session-cycle churn.

Spawns the BE, then cycles: attach N sessions → drive RPCs → close
clients → force eviction. Samples FD count at every step. A healthy
BE should show:

  * FD count rises during ATTACH (one socket per WS client + a small
    delta for per-session SQLite connections in flight).
  * FD count drops after the clients close (sockets freed).
  * FD count is roughly constant across multiple cycles (the
    sessions evict, their sqlite handles + WS state release).

A monotonic climb across cycles points at a leak in something
session-scoped that never releases its FDs: chroma client, agno
team's HTTP pool, an unclosed file handle in a third-party lib.

Categorises FDs by kind so a leak in (say) raw files vs sockets is
obvious. macOS ``lsof`` is verbose but stable; on linux the same
counts come from ``ls /proc/<pid>/fd``.

Run with::

    .venv/bin/python scripts/profile_fd_count.py \\
        --sessions 6 --cycles 4
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

import websockets


REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = REPO_ROOT / ".venv" / "bin" / "python"


def _fd_breakdown(pid: int) -> tuple[int, Counter[str]]:
    """Return (total_fds, by_kind) using ``lsof``.

    Categories: socket (TCP/UDP/unix), pipe, regular (REG), dir,
    char-dev, other. Keeps the count comparison meaningful when the
    OS adds noise (DEVICE columns vary by version).
    """
    try:
        out = subprocess.run(
            ["lsof", "-p", str(pid), "-n", "-P"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 0, Counter()
    lines = out.stdout.splitlines()[1:]  # drop header
    by_kind: Counter[str] = Counter()
    total = 0
    for line in lines:
        parts = line.split()
        if len(parts) < 5:
            continue
        fd_col = parts[3]
        type_col = parts[4]
        # ``lsof`` lists a few non-numeric FDs per process (cwd, txt,
        # rtd, mem, NOFD) that aren't real file descriptors — skip
        # them so the count matches what /proc/<pid>/fd would show.
        if not fd_col[:-1].rstrip().isdigit() and not fd_col.rstrip("uwr").isdigit():
            continue
        total += 1
        if type_col in ("IPv4", "IPv6"):
            by_kind["socket"] += 1
        elif type_col == "unix":
            by_kind["unix-socket"] += 1
        elif type_col == "PIPE":
            by_kind["pipe"] += 1
        elif type_col == "REG":
            by_kind["regular"] += 1
        elif type_col == "DIR":
            by_kind["dir"] += 1
        else:
            by_kind[type_col.lower()] += 1
    return total, by_kind


def _format_breakdown(b: Counter[str]) -> str:
    """Compact one-line render — only kinds with count > 0."""
    if not b:
        return "(none)"
    return "  ".join(
        f"{k}={v}" for k, v in sorted(b.items(), key=lambda kv: (-kv[1], kv[0]))
    )


async def _connect_and_welcome(port: int):
    ws = await websockets.connect(f"ws://127.0.0.1:{port}")
    await asyncio.wait_for(ws.recv(), 10.0)
    return ws


async def _rpc(ws, method: str, args=None, timeout=10.0):
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


async def _drive(args) -> None:
    project = Path(tempfile.mkdtemp(prefix="ember-fd-"))
    print(f"# Project dir: {project}")

    env = {
        **os.environ,
        "EMBER_PARENT_PID": str(os.getpid()),
        # 1s timeout so each cycle's evict is fast.
        "EMBER_SESSION_IDLE_TIMEOUT": "1",
    }
    proc = subprocess.Popen(
        [str(PYTHON), "-m", "ember_code.backend",
         "--ws-port", "0", "--project-dir", str(project)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=REPO_ROOT, env=env,
    )

    # Read ready line.
    ws_url = None
    while True:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("BE exited before ready")
        try:
            envelope = json.loads(line.decode().strip())
            if envelope.get("status") == "ready":
                ws_url = envelope["ws_url"]
                break
        except Exception:
            continue
    port = int(ws_url.rsplit(":", 1)[-1])
    pid = proc.pid

    def sample(label: str) -> int:
        total, kinds = _fd_breakdown(pid)
        print(f"# [{label:<22}] FDs = {total:>4}   {_format_breakdown(kinds)}")
        return total

    try:
        boot_fds = sample("BOOT")

        cycle_baselines = []
        for cycle in range(1, args.cycles + 1):
            # ── Attach ──
            clients = []
            for i in range(args.sessions):
                ws = await _connect_and_welcome(port)
                await _rpc(
                    ws,
                    "attach_session",
                    {"session_id": f"c{cycle}-s{i}"},
                    timeout=20,
                )
                clients.append(ws)
            fds_attached = sample(f"cycle{cycle} ATTACHED")

            # ── Drive a small burst so any per-RPC FDs get exercised ──
            for _ in range(args.requests_per_cycle):
                await _rpc(clients[_ % len(clients)], "get_status")
            fds_after_load = sample(f"cycle{cycle} AFTER LOAD")

            # ── Disconnect ──
            for ws in clients:
                await ws.close()
            await asyncio.sleep(0.5)  # let WS handler finish its finally
            fds_disconnected = sample(f"cycle{cycle} DISCONNECTED")

            # ── Force evict + GC ──
            os.kill(pid, signal.SIGUSR1)
            await asyncio.sleep(args.evict_wait)
            fds_evicted = sample(f"cycle{cycle} AFTER EVICT")
            cycle_baselines.append(fds_evicted)

        # ── Summary ──
        print()
        print("# Cycle baselines (FDs after each evict):")
        for i, fds in enumerate(cycle_baselines, 1):
            delta_boot = fds - boot_fds
            print(f"#   cycle {i}: {fds:>4}  ({delta_boot:+d} vs boot)")
        # Verdict: drift across cycles should be bounded.
        if len(cycle_baselines) >= 2:
            drift = cycle_baselines[-1] - cycle_baselines[0]
            if drift <= 5:
                verdict = "HEALTHY — FD count returns to baseline after each cycle"
            elif drift <= 20:
                verdict = (
                    f"MILD — small drift ({drift:+d}) across cycles; "
                    "could be a third-party cache, monitor under longer runs"
                )
            else:
                verdict = (
                    f"WARN — FD count grew {drift:+d} across cycles; "
                    "something session-scoped isn't releasing"
                )
            print(f"# verdict: {verdict}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sessions", type=int, default=6)
    ap.add_argument("--cycles", type=int, default=4)
    ap.add_argument("--requests-per-cycle", type=int, default=20)
    ap.add_argument(
        "--evict-wait",
        type=float,
        default=8.0,
        help="Seconds to wait after SIGUSR1 for evict shutdowns to complete.",
    )
    args = ap.parse_args()
    asyncio.run(_drive(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
