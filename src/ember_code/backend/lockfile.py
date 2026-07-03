"""Per-project backend lockfile — discovery + shared-BE coordination.

When a client (VSCode extension, JB plugin, Tauri shell) wants a
backend for a project, it should first check
``<project>/.ember/backend.lock``:

- If the lockfile exists AND the recorded PID is alive AND the
  recorded port answers a TCP connect AND the wire version matches,
  the client connects to that BE instead of spawning its own. Both
  clients then talk to the same Python process, share in-memory
  session/queue/stream state, and every push emitted by the BE is
  broadcast to both webviews.

- If any check fails, the client removes the stale lock and spawns
  a fresh BE. The new BE writes its own lock as soon as its
  WebSocket port is bound (see ``write_lockfile`` below).

The lockfile is JSON so it's trivial to inspect and debug from a
shell (``cat .ember/backend.lock | jq``). Atomic write via
``os.replace`` — the file is either the previous BE's or the new
BE's, never a half-written blend.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


LOCKFILE_NAME = "backend.lock"


class Lockfile:
    """Read/write per-project backend discovery file.

    Instances are cheap — construct one per project_dir. Methods
    are synchronous file I/O; the writes are small (~200 bytes)
    and infrequent (once at BE startup, once at BE shutdown), so
    no async wrapper is worth the complexity.
    """

    def __init__(self, project_dir: str | Path):
        # ``resolve`` mirrors the canonicalisation ``__main__.py``
        # does before passing project_dir to the BE — the lockfile
        # lives next to ``state.db`` inside the resolved project,
        # not at whatever path the caller happened to pass in.
        self._dir = Path(project_dir).resolve() / ".ember"
        self._path = self._dir / LOCKFILE_NAME

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> dict[str, Any] | None:
        """Return the lockfile's payload, or ``None`` if it's absent
        or unparseable. Unparseable files are treated as stale and
        get overwritten by the next ``write`` — no reason to fail
        loud when the state is disposable anyway."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.debug("lockfile read failed: %s", exc)
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("lockfile at %s is not valid JSON; treating as stale", self._path)
            return None
        if not isinstance(data, dict):
            return None
        return data

    def write(self, *, pid: int, port: int, wire_version: str) -> None:
        """Atomically write ``{pid, port, wire_version, created_at}``.

        Uses ``os.replace`` so a concurrent reader always sees either
        the previous contents or the new contents — never a partial
        write. ``created_at`` lets a future GC step (``ember doctor
        --clean``) prune ancient locks whose BEs have long since
        died without cleaning up.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": pid,
            "port": port,
            "wire_version": wire_version,
            "created_at": int(time.time()),
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        os.replace(tmp, self._path)
        logger.info("wrote %s (pid=%d port=%d version=%s)", self._path, pid, port, wire_version)

    def remove(self) -> None:
        """Delete the lockfile. Idempotent — missing is fine."""
        try:
            self._path.unlink()
            logger.info("removed %s", self._path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.debug("lockfile remove failed: %s", exc)


def is_pid_alive(pid: int) -> bool:
    """Cross-POSIX check: is ``pid`` still a live process?

    ``os.kill(pid, 0)`` is the canonical no-signal probe — succeeds
    if the process exists, ``ProcessLookupError`` if it's gone,
    ``PermissionError`` if it exists but we can't signal it (which
    still means alive for our purposes: same user's own processes
    always allow signal 0).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        logger.debug("is_pid_alive(%d) failed: %s", pid, exc)
        return False
    return True


def is_port_reachable(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    """Cheap TCP connect probe — the port is reachable if the
    ``connect`` call completes without raising. We don't
    hand-shake at the WebSocket layer here; that would require an
    async client and add complexity. The lockfile's ``pid`` check
    already tells us the process is alive; the TCP probe just
    confirms it's still listening.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def discover(project_dir: str | Path, expected_wire_version: str) -> dict[str, Any] | None:
    """Return the running BE's lockfile payload if it's live, healthy,
    AND version-matched; ``None`` otherwise.

    Callers use the returned ``port`` to connect. A ``None`` return
    means the caller should spawn a fresh BE (and any stale lockfile
    has already been removed for them).

    The version check is exact-string — patch-level mismatch counts
    as incompatible. Wire changes between patches are rare but not
    impossible, and we prefer a false-alarm "spawn new" over a
    false-clear "protocol drift silently corrupts state".

    Returns ``None`` and logs an INFO line when a lockfile is
    present but doesn't pass — that log is the diagnostic breadcrumb
    for "why did my client spawn a new BE".
    """
    lock = Lockfile(project_dir)
    data = lock.read()
    if data is None:
        return None

    pid = int(data.get("pid") or 0)
    port = int(data.get("port") or 0)
    version = str(data.get("wire_version") or "")

    if not is_pid_alive(pid):
        logger.info("lockfile present but pid %d is dead; removing", pid)
        lock.remove()
        return None
    if not is_port_reachable(port):
        logger.info("lockfile pid %d alive but port %d unreachable; removing", pid, port)
        lock.remove()
        return None
    if version != expected_wire_version:
        # Version mismatch — DO NOT remove the lockfile; the
        # running BE is legitimately owned by a different-version
        # client. Signal to the caller via a distinct return
        # shape so it can surface a user-facing notification.
        logger.info(
            "lockfile version %s != expected %s; caller should notify user",
            version,
            expected_wire_version,
        )
        return {**data, "_version_mismatch": True}

    return data
