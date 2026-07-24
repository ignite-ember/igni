"""ProcessRegistry — lifecycle owner for background-shell processes.

Split out of :mod:`process_supervisor` when the audit flagged the
supervisor as a data-behaviour god: the registry was a passive
dict guarded by a lock while the *coordination* logic (announce
start + persist, remove + persist_remove + cancel eviction, arm
eviction task, emit line, emit completion) sat on
:class:`ProcessSupervisor` operating on the registry from outside.

This module gives that behaviour a home. Constructor takes the
three collaborators the coordination logic needs (bus, log store,
persistence store) plus a TTL knob; the fire-and-forget scheduler
lives on the registry so ``_persist_*`` don't have to be
re-implemented at every subsystem that hits the same pattern.

Public collaborators the supervisor still needs (bus, log_store,
persistence configuration) are exposed as attributes / methods so
the small handful of sync-import call sites can reach them via
``supervisor.registry.bus`` etc.

Threading model:

* :class:`ProcessRegistry` is sync-lock guarded — safe from both
  async tools and the sync ``cancel_foreground`` path.
* :class:`ProcessEventBus` (owned as ``self.bus``) is also
  sync-lock guarded.
* Eviction task handles live on the registry as a
  ``dict[int, asyncio.Task]`` keyed by pid — previously reached
  in via a private attr on :class:`ManagedProcess`, which broke
  encapsulation the audit called out.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any

from ember_code.core.tools.async_fire_and_forget import AsyncFireAndForget
from ember_code.core.tools.process_bus import ProcessEventBus
from ember_code.core.tools.process_events import (
    ProcessExitEvent,
    ProcessLineEvent,
    ProcessStartEvent,
)
from ember_code.core.tools.process_log import ProcessLogStore
from ember_code.core.tools.process_store import (
    BackgroundProcessRow,
)

logger = logging.getLogger(__name__)

#: Default finished-process eviction TTL (10 minutes after the
#: most recent read).
DEFAULT_FINISHED_PROCESS_TTL_SECONDS = 600.0


class ProcessRegistry:
    """Owning collaborator for every backgrounded shell process.

    Holds the pid → ``ManagedProcess`` (or ``OrphanProcess``) map,
    the per-pid eviction task handles, the event bus, the on-disk
    log store, and the optional persistence store. Announce /
    deregister / completion methods co-locate the emit + persist
    steps with the registry mutation so callers can't accidentally
    skip half the lifecycle.

    Constructor collaborators are all optional so tests can build
    a bare ``ProcessRegistry()`` and get sensible defaults (fresh
    bus, log store with ``project_dir=None``, no persistence).
    Production wiring passes real instances via
    :meth:`attach_persistence` (persistence) and
    :meth:`ProcessSupervisor.configure_log_store` (log store root).
    """

    def __init__(
        self,
        bus: ProcessEventBus | None = None,
        log_store: ProcessLogStore | None = None,
        persistence: Any | None = None,
        ttl_seconds: float = DEFAULT_FINISHED_PROCESS_TTL_SECONDS,
    ) -> None:
        self._processes: dict[int, Any] = {}
        self._eviction_tasks: dict[int, asyncio.Task[Any]] = {}
        self._lock = threading.Lock()
        self.bus: ProcessEventBus = bus or ProcessEventBus()
        self.log_store: ProcessLogStore = log_store or ProcessLogStore(None)
        self._persistence: Any | None = persistence
        self._ttl_seconds: float = ttl_seconds
        self._scheduler = AsyncFireAndForget()

    # ── Persistence wiring ──────────────────────────────────────

    def attach_persistence(self, store: Any | None) -> None:
        """Point this registry at a :class:`BackgroundProcessStore`
        (or ``None`` to unwire for test isolation). Called from
        :meth:`~ember_code.core.tools.orphan_rehydrator.OrphanRehydrator.run`
        during BE startup once the DB is live."""
        self._persistence = store

    @property
    def persistence(self) -> Any | None:
        """Currently-attached persistence store, or ``None``."""
        return self._persistence

    # ── TTL knob ────────────────────────────────────────────────

    def set_ttl_seconds(self, seconds: float) -> None:
        """Override the finished-process eviction TTL. Value is
        read fresh on every :meth:`arm_eviction` call so the
        change takes effect on subsequent arms."""
        self._ttl_seconds = seconds

    @property
    def ttl_seconds(self) -> float:
        """Current finished-process eviction TTL."""
        return self._ttl_seconds

    # ── Add / remove ────────────────────────────────────────────

    def add(self, mp: Any) -> int:
        """Add ``mp`` to the pid map WITHOUT firing subscribers or
        persisting.

        The shell tool's spawn path uses this to make the process
        readable from :meth:`get` immediately, then calls
        :meth:`announce_start` once the caller has decided the
        process is worth surfacing (backgrounded, or foreground
        that timed out and got auto-promoted). Also used by
        orphan rehydration, where we DON'T want to re-emit start
        for a process that already exists.
        """
        pid = mp.proc.pid
        with self._lock:
            self._processes[pid] = mp
        return pid

    def announce_start(self, mp: Any) -> None:
        """Emit the ``start`` event AND persist the DB row.

        Called from the two paths that surface a process to the
        watcher: backgrounded spawn, and foreground-that-timed-out
        auto-promotion. Persist FIRST so a subscriber that crashes
        can't sink the orphan-recovery DB row (subscribers are FE
        pushes; the DB row is load-bearing for cross-restart
        tracking).
        """
        self._persist_add(mp.proc.pid, mp.cmd)
        self.bus.emit(
            "start",
            ProcessStartEvent(pid=mp.proc.pid, cmd=mp.cmd, started_at=time.time()),
        )

    def remove(self, pid: int) -> None:
        """Remove ``pid`` from the pid map. Also delete the
        persisted row and cancel any pending eviction task —
        this is the single "tear down for this pid" entry point.

        Idempotent: extra calls no-op.
        """
        self._persist_remove(pid)
        with self._lock:
            self._processes.pop(pid, None)
            task = self._eviction_tasks.pop(pid, None)
        if task is not None and not task.done():
            task.cancel()

    def emit_completion(self, mp: Any) -> None:
        """Fire ``exit`` subscribers AND delete the persisted row.
        Called from the reader task once stdout closes.

        Prunes the persisted row BEFORE the FE-facing subscribers
        fire: otherwise a BE restart between exit and the FE
        seeing the ``process_exited`` push would leave a dead pid
        in the store, surfacing as an orphan that's already gone.
        """
        self._persist_remove(mp.proc.pid)
        self.bus.emit(
            "exit",
            ProcessExitEvent(
                pid=mp.proc.pid,
                cmd=mp.cmd,
                exit_code=mp.proc.returncode,
                duration_seconds=time.monotonic() - mp.started_at,
                output_tail=mp.read(tail=40),
            ),
        )

    def emit_line(self, pid: int, line: str) -> None:
        """Notify ``line`` subscribers. Hot path — the bus
        snapshots the subscriber list under its own lock, so this
        stays contention-free at the callsite."""
        self.bus.emit("line", ProcessLineEvent(pid=pid, line=line))

    # ── Query ───────────────────────────────────────────────────

    def get(self, pid: int) -> Any | None:
        with self._lock:
            return self._processes.get(pid)

    def all_running(self) -> list[tuple[int, str, float]]:
        """Return ``(pid, cmd, elapsed_seconds)`` for every running
        process. Uses polymorphic ``mp.elapsed()`` — both
        :class:`ManagedProcess` (monotonic) and
        :class:`~ember_code.core.tools.orphan_process.OrphanProcess`
        (epoch) implement it, no duck-check needed here."""
        with self._lock:
            result: list[tuple[int, str, float]] = []
            for pid, mp in self._processes.items():
                if mp.is_running():
                    result.append((pid, mp.cmd, mp.elapsed()))
            return result

    def clear(self) -> None:
        """Drop every entry + every pending eviction task. Test-
        isolation helper."""
        with self._lock:
            self._processes.clear()
            tasks = list(self._eviction_tasks.values())
            self._eviction_tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()

    def kill_all(self) -> int:
        """Kill all tracked processes synchronously. Returns count
        killed. Used at BE shutdown — we just send SIGKILL and
        don't wait for the processes to fully reap; the async
        loop is already torn down so we can't ``await proc.wait()``."""
        with self._lock:
            count = 0
            for mp in self._processes.values():
                if mp.is_running():
                    mp.kill()
                    count += 1
            self._processes.clear()
            tasks = list(self._eviction_tasks.values())
            self._eviction_tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        return count

    # ── Eviction ────────────────────────────────────────────────

    def arm_eviction(self, mp: Any) -> None:
        """Arm/refresh the async eviction task for a finished
        process. Called from ``read_process_output`` whenever the
        agent reads a finished process; cancels any existing task
        first so the TTL resets to "10 min after the *most recent*
        read", not "10 min after the first read"."""
        pid = mp.proc.pid
        with self._lock:
            existing = self._eviction_tasks.get(pid)
        if existing is not None and not existing.done():
            existing.cancel()

        # Read TTL fresh so ``set_ttl_seconds`` calls made between
        # the previous arm and this one take effect.
        ttl = self._ttl_seconds

        async def _evict() -> None:
            try:
                await asyncio.sleep(ttl)
            except asyncio.CancelledError:
                return
            with self._lock:
                current = self._processes.get(pid)
                should_evict = current is mp
                if should_evict:
                    self._processes.pop(pid, None)
                    self._eviction_tasks.pop(pid, None)
            if should_evict:
                logger.debug("evicted process pid=%d after TTL", pid)
                # Per-pid log file follows the same TTL — the
                # agent's in-memory buffer and the on-disk tail-
                # able copy should go away together. Skipped if
                # the registry row was already swapped (pid reuse
                # edge case).
                self.log_store.cleanup(pid)

        task = asyncio.create_task(_evict())
        with self._lock:
            self._eviction_tasks[pid] = task

    # ── Persistence internals ───────────────────────────────────

    def _persist_add(self, pid: int, cmd: str) -> None:
        """Fire-and-forget upsert. See :class:`AsyncFireAndForget`
        for the "why this checks the loop first" story.

        ``store.upsert`` returns a typed
        :class:`~ember_code.core.tools.process_store_schemas.UpsertResult`
        so a DB failure at the fire-and-forget boundary logs a
        typed reason instead of getting swallowed by the
        scheduler's default exception handler.
        """
        store = self._persistence
        if store is None:
            return
        try:
            pgid = os.getpgid(pid)
        except (ProcessLookupError, OSError):
            pgid = None
        # Row stamps its own ``started_at`` — the former
        # module-level ``now_epoch`` helper moved onto the model
        # with :meth:`BackgroundProcessRow.new`.
        row = BackgroundProcessRow.new(pid=pid, cmd=cmd, pgid=pgid)
        self._scheduler.schedule(self._await_upsert(store, row))

    def _persist_remove(self, pid: int) -> None:
        """Fire-and-forget delete. Same semantics as
        :meth:`_persist_add`."""
        store = self._persistence
        if store is None:
            return
        self._scheduler.schedule(self._await_remove(store, pid))

    async def _await_upsert(self, store: Any, row: BackgroundProcessRow) -> None:
        """Await ``store.upsert`` and log a DEBUG reason on
        failure. Wraps the coroutine so the scheduler surfaces a
        clean coroutine (not a bare unawaited return) and the
        typed :class:`UpsertResult.reason` still gets logged.
        """
        result = await store.upsert(row)
        if not getattr(result, "ok", True):
            reason = getattr(result, "reason", "")
            logger.debug("persist_add failed: %s", reason)

    async def _await_remove(self, store: Any, pid: int) -> None:
        """Await ``store.remove`` and log a DEBUG reason on
        failure. Same shape as :meth:`_await_upsert`."""
        result = await store.remove(pid)
        if not getattr(result, "ok", True):
            reason = getattr(result, "reason", "")
            logger.debug("persist_remove pid=%s failed: %s", pid, reason)

    # ── Test / reset helpers ────────────────────────────────────

    def reset(self) -> None:
        """Drop subscribers + registry entries + eviction tasks +
        persistence handle. Fixture-teardown helper — production
        BE never calls this."""
        self.bus.reset()
        self.clear()
        self._persistence = None
