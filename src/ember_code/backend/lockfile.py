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
  WebSocket port is bound (see ``BackendSupervisor.write_discovery_lockfile``).

The lockfile is JSON so it's trivial to inspect and debug from a
shell (``cat .ember/backend.lock | jq``). Atomic write via
``os.replace`` — the file is either the previous BE's or the new
BE's, never a half-written blend.

Module contents:

* :class:`Lockfile` — file-I/O over a :class:`LockfilePayload`.
  Reads/writes the JSON payload atomically. Returns
  :class:`WriteLockfileResult` / :class:`RemoveLockfileResult`
  envelopes instead of raising for expected failures
  (:file:`CODE_STANDARDS.md` Pattern 3).
* :class:`BackendDiscovery` — coordinator that composes a
  :class:`Lockfile` and two probe callables (PID liveness, TCP
  reachability) into a single :meth:`probe` call returning a
  discriminated :data:`DiscoveryResult` union. Probes are injectable
  seams so tests never need to monkeypatch module-level helpers.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from pathlib import Path

from ember_code.backend.schemas_lockfile import (
    DiscoveryResult,
    LiveBackend,
    LockfilePayload,
    NoBackend,
    RemoveLockfileResult,
    VersionMismatch,
    WriteLockfileResult,
)

logger = logging.getLogger(__name__)


LOCKFILE_NAME = "backend.lock"


class Lockfile:
    """Read/write per-project backend discovery file.

    Instances are cheap — construct one per project_dir. Methods
    are synchronous file I/O; the writes are small (~200 bytes)
    and infrequent (once at BE startup, once at BE shutdown), so
    no async wrapper is worth the complexity.

    All I/O goes through :class:`LockfilePayload` — no raw
    ``dict[str, Any]`` ever crosses the module boundary.
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

    def read(self) -> LockfilePayload | None:
        """Return the lockfile's parsed payload, or ``None`` if it's
        absent, unreadable, or unparseable.

        Unparseable / schema-mismatched files are treated as stale
        and get overwritten by the next :meth:`write` — no reason
        to fail loud when the state is disposable anyway.
        """
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
        try:
            return LockfilePayload.model_validate(data)
        except Exception as exc:
            logger.warning(
                "lockfile at %s failed schema validation (%s); treating as stale",
                self._path,
                exc,
            )
            return None

    def write(self, payload: LockfilePayload) -> WriteLockfileResult:
        """Atomically write ``payload`` to disk.

        Uses ``os.replace`` so a concurrent reader always sees either
        the previous contents or the new contents — never a partial
        write.

        Expected write failures (permissions, missing parent
        directory that can't be created, read-only filesystem)
        surface as ``WriteLockfileResult(ok=False, reason=...)``
        instead of a raised ``OSError`` — Pattern 3 (expected
        failures returned as values).
        """
        try:
            self._atomic_write_json(payload)
        except OSError as exc:
            logger.debug("lockfile write failed: %s", exc)
            return WriteLockfileResult(ok=False, reason=str(exc), payload=None)

        logger.info(
            "wrote %s (pid=%d port=%d version=%s)",
            self._path,
            payload.pid,
            payload.port,
            payload.wire_version,
        )
        return WriteLockfileResult(ok=True, payload=payload)

    def remove(self) -> RemoveLockfileResult:
        """Delete the lockfile. Idempotent — a missing file returns
        ``ok=True, existed=False``.

        Permissions / other unlink failures surface as
        ``ok=False, reason=...`` instead of raising.
        """
        try:
            self._path.unlink()
        except FileNotFoundError:
            return RemoveLockfileResult(ok=True, existed=False)
        except OSError as exc:
            logger.debug("lockfile remove failed: %s", exc)
            return RemoveLockfileResult(ok=False, existed=True, reason=str(exc))
        logger.info("removed %s", self._path)
        return RemoveLockfileResult(ok=True, existed=True)

    # ── Internals ────────────────────────────────────────────────

    def _atomic_write_json(self, payload: LockfilePayload) -> None:
        """Write ``payload`` as JSON via a temp-file + ``os.replace``
        rename so concurrent readers never see a half-written file.

        Extracted onto :class:`Lockfile` so external callers (tests,
        supervisor) don't reach into private attributes for the
        temp-path dance.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        # ``model_dump`` preserves field-declaration order so the
        # on-disk JSON keeps ``{pid, port, wire_version, created_at}``
        # — same key-set out-of-tree readers (jq, VSCode, JB) parse
        # today. Byte-stable wire format is a hard requirement.
        tmp.write_text(json.dumps(payload.model_dump()) + "\n", encoding="utf-8")
        os.replace(tmp, self._path)


class BackendDiscovery:
    """Coordinator: 'is there a live BE for this project already?'

    Owns three concerns behind one :meth:`probe` call:

    1. Read the on-disk :class:`Lockfile` payload.
    2. Confirm the recorded PID + port are both alive + reachable
       (via injected probes).
    3. Enforce exact-string wire-version match; a mismatch keeps the
       lockfile intact and signals ``VersionMismatch`` so the caller
       can surface a user-facing notification.

    Both probes and the time source are constructor-injected
    :class:`Callable` seams so tests never need to monkeypatch
    module-level helpers or wall-clock time — instantiate the
    coordinator with fakes and assert on the returned
    :data:`DiscoveryResult` union directly.
    """

    def __init__(
        self,
        project_dir: str | Path,
        expected_wire_version: str,
        *,
        pid_probe: Callable[[LockfilePayload], bool] | None = None,
        port_probe: Callable[[LockfilePayload], bool] | None = None,
    ) -> None:
        self._lockfile = Lockfile(project_dir)
        self._expected_wire_version = expected_wire_version
        self._pid_probe = pid_probe if pid_probe is not None else self._default_pid_probe
        self._port_probe = port_probe if port_probe is not None else self._default_port_probe

    @property
    def lockfile_path(self) -> Path:
        """On-disk lockfile path — exposed for logging without leaking
        the composed :class:`Lockfile` (which owns write/remove
        semantics that must go through :meth:`probe`)."""
        return self._lockfile.path

    def probe(self) -> DiscoveryResult:
        """Return the current discovery outcome as a tagged union.

        Callers connect to :attr:`LiveBackend.payload.port` on a
        :class:`LiveBackend` result. A :class:`NoBackend` means the
        caller should spawn a fresh BE (any stale lockfile has
        already been removed for them). A :class:`VersionMismatch`
        keeps the lockfile intact — the running BE is legitimately
        owned by a different-version client — and the caller
        surfaces a user-facing notification.

        The version check is exact-string — patch-level mismatch
        counts as incompatible. Wire changes between patches are
        rare but not impossible, and we prefer a false-alarm 'spawn
        new' over a false-clear 'protocol drift silently corrupts
        state'.
        """
        payload = self._lockfile.read()
        if payload is None:
            return NoBackend()

        if not self._pid_probe(payload):
            logger.info("lockfile present but pid %d is dead; removing", payload.pid)
            self._lockfile.remove()
            return NoBackend()

        if not self._port_probe(payload):
            logger.info(
                "lockfile pid %d alive but port %d unreachable; removing",
                payload.pid,
                payload.port,
            )
            self._lockfile.remove()
            return NoBackend()

        if not payload.matches_version(self._expected_wire_version):
            logger.info(
                "lockfile version %s != expected %s; caller should notify user",
                payload.wire_version,
                self._expected_wire_version,
            )
            return VersionMismatch(
                payload=payload,
                expected=self._expected_wire_version,
            )

        return LiveBackend(payload=payload)

    # ── Default probes ───────────────────────────────────────────

    @staticmethod
    def _default_pid_probe(payload: LockfilePayload) -> bool:
        """Delegates to :meth:`LockfilePayload.is_pid_alive` — kept
        as a staticmethod seam so tests can pass a fake probe with
        the same ``Callable[[LockfilePayload], bool]`` signature."""
        return payload.is_pid_alive()

    @staticmethod
    def _default_port_probe(payload: LockfilePayload) -> bool:
        """Delegates to :meth:`LockfilePayload.is_port_reachable`."""
        return payload.is_port_reachable()
