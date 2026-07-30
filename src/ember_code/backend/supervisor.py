"""Backend lifecycle supervisor.

Owns everything about "when does the BE start / when does it stop":

* the shutdown ``asyncio.Event``,
* signal handlers (SIGTERM / SIGINT / SIGUSR1) so a Ctrl-C in the FE
  flips the same lever the parent-watchdog uses,
* the parent-PID watchdog loop (the belt to the signal suspenders),
* the transport-close-on-shutdown task (WS connections don't hang up
  when the FE exits, so the receive-loop needs a shove),
* the periodic session-pool evictor sweep,
* the discovery lockfile lifecycle,
* the final teardown ordering.

Extracted out of :mod:`ember_code.backend.__main__` where these lived
as five separate nested closures + four independent boolean flags.
The single-source-of-truth is now :attr:`BackendSupervisor.phase`
(:class:`LifecyclePhase`).
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import logging
import os
import re
import signal
from pathlib import Path
from typing import Any

from ember_code.backend.lockfile import Lockfile
from ember_code.backend.schemas_lockfile import LockfilePayload
from ember_code.backend.schemas_rpc import LifecyclePhase

logger = logging.getLogger(__name__)


class BackendSupervisor:
    """Runs the BE's non-message-handling lifecycle machinery.

    The class doesn't own the transport or the pool — those are
    passed in via constructor / setter injection. It owns the
    background tasks and signals that make sure the process comes
    up in the right order and goes down cleanly.
    """

    def __init__(
        self,
        *,
        transport: Any,
        loop: asyncio.AbstractEventLoop,
        project_dir: Path,
    ) -> None:
        self._transport = transport
        self._loop = loop
        self._project_dir = project_dir
        self._shutdown_event = asyncio.Event()
        self._phase = LifecyclePhase.BOOTING
        self._parent_pid = self._resolve_parent_pid()
        self._pool: Any = None
        self._backend: Any = None  # fallback shutdown target if pool is absent
        # Task handles — kept as strong refs so the loop doesn't GC them.
        self._parent_watch_task: asyncio.Task[None] | None = None
        self._transport_close_task: asyncio.Task[None] | None = None
        self._evictor_task: asyncio.Task[None] | None = None
        self._lockfile: Lockfile | None = None

    # ── State access ─────────────────────────────────────────────

    @property
    def phase(self) -> LifecyclePhase:
        return self._phase

    @property
    def shutdown_event(self) -> asyncio.Event:
        return self._shutdown_event

    def request_shutdown(self) -> None:
        """Ask the BE to stop. Idempotent — multiple callers race
        signals + the parent watchdog + the transport close hook."""
        self._shutdown_event.set()

    # ── Wiring ───────────────────────────────────────────────────

    def set_pool(self, pool: Any) -> None:
        """Late-bind the session pool.

        Signal handlers are installed BEFORE the pool exists (the
        SIGTERM handler must be up during boot in case the parent
        dies mid-init). SIGUSR1 uses whatever pool is currently
        bound; a SIGUSR1 in the boot gap loses one diagnostic
        ``gc.collect`` — an acceptable tradeoff for keeping the
        signal handler simple.
        """
        self._pool = pool

    def set_backend_fallback(self, backend: Any) -> None:
        """Fallback shutdown target used if startup crashed before
        the pool got wired. Without it, teardown would ``NameError``
        on early failures."""
        self._backend = backend

    # ── Startup ──────────────────────────────────────────────────

    def install_signal_handlers(self) -> None:
        """POSIX signals → ``shutdown_event`` + SIGUSR1 → gc probe.

        ``loop.add_signal_handler`` raises ``NotImplementedError`` on
        Windows — the asyncio Proactor / Selector loops don't
        implement POSIX signals. On Windows we fall back to Ctrl-C
        propagating out of ``asyncio.run`` + the parent-PID watchdog.
        """
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                self._loop.add_signal_handler(sig, self._on_terminate_signal)
        with contextlib.suppress(NotImplementedError):
            self._loop.add_signal_handler(signal.SIGUSR1, self._on_release_signal)

    def start_parent_watchdog(self) -> None:
        self._parent_watch_task = asyncio.create_task(self._watch_parent_loop())

    def start_transport_close_watcher(self) -> None:
        self._transport_close_task = asyncio.create_task(self._close_transport_on_shutdown())

    def start_evictor(self, sweep_interval_seconds: float = 5 * 60) -> None:
        """Kick off the periodic eviction sweep. Requires
        :meth:`set_pool` to have been called first."""
        if self._pool is None:  # pragma: no cover — defensive
            logger.warning("start_evictor called before set_pool; no sweeps will run")
            return
        self._evictor_task = asyncio.create_task(self._evictor_loop(sweep_interval_seconds))

    def mark_running(self) -> None:
        """Boot finished successfully; transition BOOTING → RUNNING."""
        self._phase = LifecyclePhase.RUNNING

    # ── Discovery lockfile ───────────────────────────────────────

    def write_discovery_lockfile(self, ws_port: int) -> None:
        """Publish the bound WS port at ``<project>/.ember/backend.lock``
        so a second client opening the same project can discover this
        BE and connect to it instead of spawning a duplicate.

        Silent no-op on write errors (permissions, missing
        ``.ember`` dir): discovery is a nice-to-have; a duplicate BE
        is a real cost but the FE can survive it.
        """
        version = self._resolve_wire_version()
        lock = Lockfile(self._project_dir)
        payload = LockfilePayload.now(
            pid=os.getpid(),
            port=ws_port,
            wire_version=version,
        )
        result = lock.write(payload)
        if not result.ok:
            logger.warning("could not write backend lockfile: %s", result.reason)
            return
        self._lockfile = lock

    # ── Main-loop hooks ──────────────────────────────────────────

    async def wait_for_shutdown(self) -> None:
        await self._shutdown_event.wait()

    async def teardown(self) -> None:
        """Final ordered shutdown. Called from the ``finally`` block
        so it runs even if the receive-loop raised."""
        self._phase = LifecyclePhase.SHUTTING_DOWN

        # Cancel background tasks first (they'd otherwise hold refs
        # to the pool + transport we're about to shut down).
        for task in (self._parent_watch_task, self._transport_close_task, self._evictor_task):
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        # Pool teardown handles every runtime, including the default;
        # if the pool never got wired (startup crash), fall back to
        # shutting down whatever backend the caller registered.
        # ``SessionPool.shutdown`` returns a list of per-runtime
        # :class:`ShutdownReport` envelopes — log the failures so
        # they don't disappear into the void.
        if self._pool is not None:
            try:
                reports = await self._pool.shutdown()
            except Exception as exc:  # pragma: no cover — defensive
                logger.debug("pool shutdown crashed: %s", exc)
            else:
                for report in reports:
                    if not report.ok:
                        logger.warning(
                            "session %s shutdown failed: %s",
                            report.session_id or "<unknown>",
                            report.error,
                        )
        elif self._backend is not None:
            with contextlib.suppress(Exception):
                await self._backend.shutdown()

        with contextlib.suppress(Exception):
            await self._transport.close()

        if self._lockfile is not None:
            with contextlib.suppress(Exception):
                self._lockfile.remove()

        self._phase = LifecyclePhase.STOPPED
        logger.info("Backend shut down")

    # ── Signal callbacks ─────────────────────────────────────────

    def _on_terminate_signal(self) -> None:
        self._shutdown_event.set()

    def _on_release_signal(self) -> None:
        """SIGUSR1 → diagnostic ``gc.collect()`` + immediate pool
        evict sweep. Used by the release-phase profiler to
        demonstrate the downward RSS trend without waiting for the
        5-minute sweep interval."""
        before = gc.get_count()
        collected = gc.collect()
        logger.info(
            "SIGUSR1: forced gc.collect — collected %d objects (generation counts before: %s)",
            collected,
            before,
        )
        if self._pool is not None:
            self._loop.create_task(self._pool.evict_idle())

    # ── Background loops ─────────────────────────────────────────

    async def _watch_parent_loop(self) -> None:
        """Self-terminate if the FE parent process dies.

        Signals are the primary cleanup path (FE's process_manager
        kills the BE process group on exit), but signals can be
        missed on hard crashes or hangups. This watchdog is the belt
        to those suspenders — without it we ended up with an 11-day-old
        runaway BE that had burned 117 hours of CPU after the FE died
        unexpectedly.
        """
        parent_pid = self._parent_pid
        if parent_pid <= 0:
            return
        while not self._shutdown_event.is_set():
            try:
                os.kill(parent_pid, 0)  # signal 0 = liveness probe only
            except ProcessLookupError:
                logger.warning("Parent FE (pid=%s) died; BE shutting down", parent_pid)
                self._shutdown_event.set()
                # Nudge the receive loop: SIGTERM so
                # ``transport.receive()`` unblocks even if no more FE
                # messages arrive.
                with contextlib.suppress(ProcessLookupError):
                    os.kill(os.getpid(), signal.SIGTERM)
                # Last-resort escape hatch: if graceful shutdown
                # stalls, hard-exit after a grace period so we never
                # linger as a zombie burning CPU.
                await asyncio.sleep(5)
                logger.error("BE failed to shut down 5s after parent death; forcing exit")
                os._exit(1)
            except PermissionError:
                # PID exists but isn't ours — treat as alive.
                pass
            await asyncio.sleep(2)

    async def _close_transport_on_shutdown(self) -> None:
        """SIGTERM/SIGINT only set ``shutdown_event`` — the main loop
        blocks in ``transport.receive()``. The Unix transport unblocks
        when the dying FE closes the socket; the WS transport does
        NOT (an idle webview keeps the connection open). Closing the
        transport on shutdown pushes the close sentinel through
        ``receive()`` for both transports."""
        await self._shutdown_event.wait()
        with contextlib.suppress(Exception):
            await self._transport.close()

    async def _evictor_loop(self, sweep_interval: float) -> None:
        """Sweep the session pool every ``sweep_interval`` seconds
        and drop runtimes idle longer than
        ``idle_timeout_seconds``. The default runtime + any
        currently-processing runtimes are spared (see
        ``SessionPool.evict_idle``)."""
        assert self._pool is not None  # start_evictor guarded above
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=sweep_interval)
                return  # Shutdown fired — exit cleanly.
            except asyncio.TimeoutError:
                pass
            try:
                report = await self._pool.evict_idle()
                if report.evicted:
                    logger.info(
                        "session pool: evicted %d idle session(s): %s",
                        len(report.evicted),
                        report.evicted_ids,
                    )
                for entry in report.evicted:
                    if not entry.shutdown.ok:
                        logger.warning(
                            "evicted session %s shutdown failed: %s",
                            entry.session_id,
                            entry.shutdown.error,
                        )
            except Exception as exc:
                logger.warning("evictor sweep failed: %s", exc)

    # ── Boot helpers ─────────────────────────────────────────────

    @staticmethod
    def _resolve_parent_pid() -> int:
        raw = os.environ.get("EMBER_PARENT_PID", "0") or "0"
        try:
            return int(raw)
        except ValueError:
            return 0

    @staticmethod
    def _resolve_wire_version() -> str:
        """Parse ``__version__`` out of the top-level package init
        without importing the package (avoids a circular import
        during boot). Returns ``"0.0.0"`` on any parse failure —
        discovery still works, just with an unknown-version stamp."""
        try:
            init_text = (Path(__file__).parent.parent / "__init__.py").read_text()
            m = re.search(r'__version__\s*=\s*"([^"]+)"', init_text)
            return m.group(1) if m else "0.0.0"
        except OSError:
            return "0.0.0"
