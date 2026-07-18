#!/usr/bin/env python3
"""Seed ``state.db`` with a fake orphan row + spawn the matching
OS process.

Used by the live-BE Playwright suite
(``watcher-footer-pill``, ``watcher-orphan-live``,
``watcher-orphan-with-logs``) to stand in for the "harness shell
script" those test headers reference. The watcher panel's
orphan-rehydrate boot pass picks up the row, the BE serves it via
``list_background_processes``, and the FE renders it as a row in
the WatcherPanel.

Two scenarios — each test wants a *different* orphan (the count
asserts are ``toHaveCount(1)``), so each scenario maps to a single
fresh run:

* ``sleep`` — cmd ``sleep 600``, no per-pid log. Used by
  ``watcher-footer-pill`` and ``watcher-orphan-live`` (which
  asserts ``cmd`` contains ``"sleep 600"``). The empty log
  surfaces the orphan's "no buffered output" placeholder.
* ``dev_server`` — a bash loop that prints
  ``listening on http://127.0.0.1:3000`` once then sleeps; the
  per-pid log is pre-seeded with the same string so the orphan's
  ``read()`` returns it. Used by ``watcher-orphan-with-logs``.

Usage::

    # Phase A — sleep orphan for footer-pill + orphan-live
    .venv/bin/python scripts/seed_watcher_e2e_orphan.py --scenario sleep

    # (run BE + tests; restart BE for next phase)

    # Phase B — dev_server orphan for orphan-with-logs
    .venv/bin/python scripts/seed_watcher_e2e_orphan.py --scenario dev_server

    # Reset — kills every spawned process + drops every row
    .venv/bin/python scripts/seed_watcher_e2e_orphan.py --cleanup
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from ember_code.core.tools.process_store import BackgroundProcessStore
from ember_code.core.tools.process_store_schemas import BackgroundProcessRow

# Anchor the script to the repo root — the project's .ember/ lives
# there. ``scripts/seed_*.py`` always lives one level below root.
REPO_ROOT = Path(__file__).resolve().parent.parent

_SCENARIOS = ("sleep", "dev_server")

# Per-pid log path mirrors ``ProcessLogStore.path()``:
#   ``<project_dir>/.ember/process_logs/<pid>.log``.
def per_pid_log_path(project_dir: Path, pid: int) -> Path:
    return project_dir / ".ember" / "process_logs" / f"{pid}.log"


def _spawn_sleep() -> subprocess.Popen[bytes]:
    """Spawn ``sleep 600`` detached from our session so the
    rehydrator's ``probe_alive(pid)`` succeeds after the BE
    restarts. ``start_new_session=True`` puts the child in its own
    process group, which the kill path needs for ``os.killpg``."""
    return subprocess.Popen(
        ["sleep", "600"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _spawn_dev_server() -> subprocess.Popen[bytes]:
    """Spawn a long-lived dev-server stand-in. The ``while true``
    loop prints the literal substring the watcher-with-logs test
    asserts on, then idles. The per-pid log file is pre-seeded
    separately so the orphan reader picks it up at rehydrate."""
    return subprocess.Popen(
        [
            "bash",
            "-c",
            "echo listening on http://127.0.0.1:3000; "
            "while true; do sleep 600; done",
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def _seed(scenario: str, project_dir: Path) -> int:
    """Spawn the scenario process, pre-seed the per-pid log if
    needed, insert the ``background_processes`` row. Returns the
    new pid."""
    if scenario == "sleep":
        proc = _spawn_sleep()
        cmd = "sleep 600"
        log_content = ""  # orphan-without-logs uses the placeholder
    elif scenario == "dev_server":
        proc = _spawn_dev_server()
        cmd = "node server.js  # dev-server stub (seeded by harness)"
        log_content = (
            "listening on http://127.0.0.1:3000\n"
            "ready to accept connections\n"
        )
    else:
        raise ValueError(f"unknown scenario: {scenario}")

    pid = proc.pid
    pgid = os.getpgid(pid)

    if log_content:
        log_path = per_pid_log_path(project_dir, pid)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(log_content, encoding="utf-8")

    store = BackgroundProcessStore(project_dir=project_dir)
    result = await store.upsert(
        BackgroundProcessRow.new(pid=pid, cmd=cmd, pgid=pgid)
    )
    if not result.ok:
        # Don't leave a half-baked process behind if the DB write
        # failed. Kill the spawned process group before bailing.
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        raise SystemExit(f"upsert failed: {result.reason}")

    log_path = per_pid_log_path(project_dir, pid)
    print(f"scenario={scenario} pid={pid} pgid={pgid} cmd={cmd!r}")
    if log_content:
        print(f"per-pid log: {log_path}")
    return pid


async def _cleanup(project_dir: Path) -> None:
    """Drop every row in ``background_processes`` and kill every
    referenced process group. Idempotent — safe to run against a
    fresh DB."""
    store = BackgroundProcessStore(project_dir=project_dir)
    rows = await store.list_all()
    for row in rows:
        result = await store.remove(row.pid)
        if row.pgid is not None:
            try:
                os.killpg(row.pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        log_path = per_pid_log_path(project_dir, row.pid)
        if log_path.exists():
            log_path.unlink()
        print(f"cleaned pid={row.pid} cmd={row.cmd!r} removed={result.removed}")
    if not rows:
        print("no rows to clean")


def _argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--scenario",
        choices=_SCENARIOS,
        help="Which orphan scenario to seed. Required unless --cleanup.",
    )
    p.add_argument(
        "--cleanup",
        action="store_true",
        help="Drop every background_processes row + kill every referenced pid.",
    )
    p.add_argument(
        "--project-dir",
        type=Path,
        default=REPO_ROOT,
        help="Project root whose .ember/state.db and process_logs/ are written. "
        "Default: %(default)s",
    )
    return p


def main(args: argparse.Namespace) -> None:
    project_dir: Path = args.project_dir.resolve(strict=False)

    if args.cleanup:
        asyncio.run(_cleanup(project_dir))
        return

    if not args.scenario:
        raise SystemExit("--scenario is required (or pass --cleanup)")
    asyncio.run(_seed(args.scenario, project_dir))


if __name__ == "__main__":
    args = _argparser().parse_args()
    main(args)