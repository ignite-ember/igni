"""SessionPool — route protocol messages to per-session BE runtimes.

One BE process, N live sessions, each owned by its own
``BackendServer`` (its own Agno team, run lock, HITL state). Views
bind to a session by stamping ``session_id`` on their messages; the
pool routes to the matching runtime, lazily resuming sessions that
aren't loaded yet. Runs on different sessions execute in parallel —
nothing is shared between runtimes except the process.

Id aliasing: ``/clear`` renews a runtime's internal session id, but
attached views keep stamping the id they bound to until they learn
the new one. Every id a runtime has EVER carried stays in
``known_ids`` so those in-flight messages still route to the same
runtime instead of spawning a ghost resume of the old id.

The default runtime (the one created at boot) handles messages with
an empty ``session_id`` — which is every message from the TUI, so
pre-pool views work unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from ember_code.backend.schemas_sessions import (
    BackendLike,
    EvictedRuntimeReport,
    EvictionReport,
    ShutdownReport,
    TransportLike,
)

# Re-export for one release cycle so any lagging in-tree importer of
# ``session_pool.SessionStampingTransport`` keeps working. New code
# should import from :mod:`session_stamping_transport` directly.
from ember_code.backend.session_stamping_transport import SessionStampingTransport

logger = logging.getLogger(__name__)


class SessionRuntime:
    """One live session: its BackendServer + per-runtime wiring.

    Promoted from a dataclass-shaped bag-of-fields to a behavior-
    owning class: :class:`SessionPool` and
    :class:`SessionOrchestrator` used to reach across the module
    boundary to mutate/read this state directly (and even monkey-
    patch a private attribute onto it). Now every lifetime concern
    the runtime owns (touching last-used, checking idle, safe
    shutdown, dir-registry deduping) is a method here.
    """

    def __init__(
        self,
        *,
        backend: BackendLike,
        rpc_table: dict[str, Any],
        queue: list[str],
        transport: TransportLike,
        known_ids: set[str] | None = None,
        last_used_at: float = 0.0,
    ) -> None:
        self.backend = backend
        self.rpc_table = rpc_table
        self.queue = queue
        self.transport = transport
        self.known_ids: set[str] = set(known_ids) if known_ids else set()
        # Monotonic timestamp of the most recent ``find`` hit. Used by
        # the idle-eviction sweep so sessions a user hasn't touched
        # in a while can release their in-memory state (Agno team,
        # chroma client, cached embeddings). State on disk is
        # unchanged — the next message for an evicted session
        # re-spawns a runtime via the resume path.
        # ``0.0`` means "never accessed" — older than every
        # initialised runtime, so an evictor sweeping at boot would
        # NOT touch any never-used runtime by mistake (we set this
        # at creation in the pool).
        self.last_used_at: float = last_used_at
        # Dedupe key for the session-directory registry write.
        # Promoted from a monkey-patched private attribute (set by
        # ``SessionOrchestrator.dispatch`` via
        # ``rt._last_dir_registered = ...  # type: ignore``) to a
        # first-class field with a typed accessor
        # (:meth:`record_dir_registered`).
        self.last_dir_registered: tuple[str, str] | None = None
        # Strong refs to fire-and-forget auto-name tasks so
        # ``asyncio``'s weak task registry doesn't GC them mid-flight
        # (a real footgun — a naming task that never completes lets
        # the session boot without a display name). Owned by the
        # runtime rather than a module global so each runtime's task
        # lifetime is bounded by the runtime's own lifetime.
        self._auto_name_tasks: set[asyncio.Task[None]] = set()

    # ── State queries ────────────────────────────────────────────

    def current_session_id(self) -> str:
        """Runtime's live session id — the one ``BackendServer``
        currently answers to. Views may still be stamping older ids,
        which is what :attr:`known_ids` exists for.
        """
        return self.backend.session_id

    def is_busy(self) -> bool:
        """True while a run is in flight on this runtime — the
        eviction sweep spares busy runtimes so a mid-stream tool
        call is never cancelled by teardown.
        """
        return bool(self.backend.processing)

    def is_idle_since(self, cutoff: float) -> bool:
        """``last_used_at < cutoff`` — the eviction sweep's
        "colder than the threshold" test. Kept on the runtime so
        the pool doesn't touch the field directly.
        """
        return self.last_used_at < cutoff

    # ── State mutations ──────────────────────────────────────────

    def touch(self, now: float) -> None:
        """Mark this runtime as active at ``now``.

        Every :meth:`SessionPool.find` hit + the initial pool
        construction stamps ``last_used_at`` — the evictor
        thresholds relative to it.
        """
        self.last_used_at = now

    def register_id(self, sid: str | None = None) -> None:
        """Record ``sid`` (or the runtime's current id if omitted)
        as a routing alias.

        The pool's ``find`` walk checks ``sid in known_ids`` on each
        runtime, so every id this runtime has ever answered to keeps
        routing here even after ``/clear`` renames its live id.
        """
        target = sid if sid is not None else self.backend.session_id
        if target:
            self.known_ids.add(target)

    def record_dir_registered(self, session_id: str, project_dir: object) -> bool:
        """Compare-and-swap the (session_id, project_dir) dedupe key.

        Returns ``True`` when the key changed (caller writes the
        dir registry), ``False`` on a repeat call.
        """
        key = (session_id, str(project_dir))
        if key == self.last_dir_registered:
            return False
        self.last_dir_registered = key
        return True

    # ── Lifetime ─────────────────────────────────────────────────

    async def shutdown_safely(self) -> ShutdownReport:
        """Shut the backend down, capturing failures into a typed
        :class:`ShutdownReport` so the pool's evictor can surface
        real teardown bugs instead of DEBUG-swallowing them.
        """
        sid = self.backend.session_id
        try:
            await self.backend.shutdown()
        except Exception as exc:
            return ShutdownReport(ok=False, session_id=sid, error=str(exc))
        return ShutdownReport(ok=True, session_id=sid)

    def spawn_auto_name(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Fire-and-forget task that survives until it completes.

        Adds itself to ``_auto_name_tasks`` on creation and removes
        itself on completion, guaranteeing the task handle stays
        strongly referenced for its whole lifetime.
        """
        task = asyncio.create_task(coro)
        self._auto_name_tasks.add(task)
        task.add_done_callback(self._auto_name_tasks.discard)
        return task


# Default: 30 minutes. Long enough that a user briefly switching to
# another project doesn't lose warm state when they come back; short
# enough that a forgotten BE doesn't hold every session it has ever
# seen forever. Tunable via ``SessionPool(idle_timeout_seconds=...)``.
_DEFAULT_IDLE_TIMEOUT = 30 * 60


class SessionPool:
    """Find-or-create SessionRuntimes keyed by (current or past) id.

    The pool now talks to :class:`SessionRuntime` through its typed
    method surface — no more reaching into ``rt.last_used_at``,
    ``rt.backend.processing``, ``rt.backend.session_id`` or
    ``rt.backend.shutdown()`` directly. Every state mutation goes
    through :meth:`SessionRuntime.touch` /
    :meth:`SessionRuntime.register_id` /
    :meth:`SessionRuntime.shutdown_safely`.
    """

    def __init__(
        self,
        default: SessionRuntime,
        factory: Callable[[str], Awaitable[SessionRuntime]],
        *,
        idle_timeout_seconds: float = _DEFAULT_IDLE_TIMEOUT,
        clock: Callable[[], float] | None = None,
    ) -> None:
        # ``clock`` is injectable so tests can fast-forward without
        # sleeping the wall clock. Default to ``time.monotonic`` — its
        # tick is independent of the event loop's, which matters when
        # the loop is paused for a debugger.
        self._clock = clock if clock is not None else time.monotonic
        default.register_id()
        # Stamp default's last_used_at so the eviction sweep doesn't
        # treat the boot runtime as "never used" → infinitely idle.
        default.touch(self._clock())
        self._runtimes: list[SessionRuntime] = [default]
        self._factory = factory
        self._idle_timeout = idle_timeout_seconds
        # Serialises creation so two messages for the same not-yet-
        # loaded session don't resume it twice. Also held during
        # ``evict_idle`` so we never evict a runtime mid-resume.
        self._create_lock = asyncio.Lock()

    @property
    def default(self) -> SessionRuntime:
        return self._runtimes[0]

    @property
    def runtimes(self) -> list[SessionRuntime]:
        return list(self._runtimes)

    def find(self, session_id: str) -> SessionRuntime | None:
        if not session_id:
            self.default.touch(self._clock())
            return self.default
        for rt in self._runtimes:
            rt.register_id()
            if session_id in rt.known_ids:
                rt.touch(self._clock())
                return rt
        return None

    async def get_or_create(self, session_id: str) -> SessionRuntime:
        rt = self.find(session_id)
        if rt is not None:
            return rt
        async with self._create_lock:
            # Re-check: another message may have created it while we
            # waited on the lock.
            rt = self.find(session_id)
            if rt is not None:
                return rt
            logger.info("session pool: resuming session %s", session_id)
            rt = await self._factory(session_id)
            rt.register_id(session_id)
            rt.register_id()
            rt.touch(self._clock())
            self._runtimes.append(rt)
            return rt

    async def evict_idle(self) -> EvictionReport:
        """Drop runtimes idle longer than ``idle_timeout_seconds``.

        The default runtime (index 0) is NEVER evicted — it serves
        empty-``session_id`` traffic (the TUI's default behaviour)
        and there's no way to lazily resume "the default session"
        if it disappears.

        A runtime that's currently processing a run
        (:meth:`SessionRuntime.is_busy`) is also skipped — evicting
        mid-stream would cancel the active run and confuse the FE.

        Returns an :class:`EvictionReport` with per-runtime idle
        seconds + shutdown outcome so the supervisor can log
        failures instead of silently discarding them. Note: Python's
        allocator typically does not return freed pages to the OS,
        so process-RSS won't shrink immediately — but the memory IS
        reclaimed and reused by subsequent allocations, so a BE
        that cycles through many sessions reaches a steady
        working-set size instead of growing unboundedly.
        """
        async with self._create_lock:
            now = self._clock()
            cutoff = now - self._idle_timeout
            keep: list[SessionRuntime] = [self._runtimes[0]]
            evicted: list[EvictedRuntimeReport] = []
            for rt in self._runtimes[1:]:
                if not rt.is_idle_since(cutoff):
                    keep.append(rt)
                    continue
                if rt.is_busy():
                    # Mid-run — leave alone; the next sweep picks it
                    # up once the run finishes and idle time grows.
                    keep.append(rt)
                    continue
                idle_seconds = now - rt.last_used_at
                report = await rt.shutdown_safely()
                if not report.ok:
                    logger.debug(
                        "evict shutdown failed for %s: %s",
                        report.session_id or "<unknown>",
                        report.error,
                    )
                evicted.append(
                    EvictedRuntimeReport(
                        session_id=report.session_id or "<unknown>",
                        idle_seconds=idle_seconds,
                        shutdown=report,
                    )
                )
                logger.info(
                    "session pool: evicted idle session %s (idle %.0fs)",
                    report.session_id or "<unknown>",
                    idle_seconds,
                )
            self._runtimes = keep
            return EvictionReport(evicted=evicted, kept=len(keep))

    async def shutdown(self) -> list[ShutdownReport]:
        """Shut down every runtime and return their outcomes.

        Failures used to be swallowed into a DEBUG log line — now
        each runtime's :meth:`SessionRuntime.shutdown_safely` yields
        a typed :class:`ShutdownReport` and the caller (typically
        :class:`BackendSupervisor.teardown`) can log or surface
        them however it likes.
        """
        reports: list[ShutdownReport] = []
        for rt in self._runtimes:
            reports.append(await rt.shutdown_safely())
        return reports


__all__ = [
    "SessionPool",
    "SessionRuntime",
    "SessionStampingTransport",
]
