"""CodeIndex warmup phase.

Sweep orphaned chroma dirs, kick the cloud resolver, run an
initial sync, refresh availability if the flag flipped, evict
idle commit chromas, and start the HEAD watcher — all fire-and-
forget on the running loop so session boot doesn't wait on the
initial resolve/sync round-trip.

Each step is a private method with its own try/except so a
failure in an early step (filesystem sweep, cloud resolver) can't
prevent the later steps (sync, watcher) from running.
"""

from __future__ import annotations

import logging

from ember_code.core.session.startup.base import SessionStartupPhase

logger = logging.getLogger(__name__)


class CodeIndexWarmupPhase(SessionStartupPhase):
    """Fire-and-forget CodeIndex warmup (sweep + resolve + sync +
    refresh + clean + watcher).

    ``CodeIndex.clean()`` drops every commit that isn't HEAD,
    isn't a branch tip, and hasn't been touched in the last 30
    days. We run it once per session after the initial sync so
    the cutoff applies to a freshly-refreshed manifest.
    """

    def start_background(self) -> None:
        """Kick the CodeIndex bootstrap sequence in the background."""
        self._schedule_on_loop(self._bootstrap)

    async def _bootstrap(self) -> None:
        """Run every warmup step in order, swallowing per-step failures
        so a downstream step still fires when an earlier one blows up.
        """
        self._sweep()
        await self._resolve()
        await self._initial_sync()
        self._refresh_availability_after_sync()
        await self._auto_clean()
        await self._start_watcher()

    def _sweep(self) -> None:
        """Sweep orphaned chroma dirs from prior sessions BEFORE we
        open any client. ``CodeIndex.clean`` defers the filesystem
        ``rmtree`` until startup so it doesn't pull the rug out from
        under a live chromadb client (same trap that bit
        ``forget_commit`` in v0.5.8). The first safe chance to finish
        that eviction is right here, before ``sync_now`` constructs
        the first PersistentClient.
        """
        session = self.session
        try:
            swept = session.code_index.sweep_stale_dirs()
            if swept:
                logger.info(
                    "Reclaimed %d orphaned chroma dir(s): %s",
                    len(swept),
                    ", ".join(s[:8] for s in swept[:5]) + ("…" if len(swept) > 5 else ""),
                )
        except Exception as exc:
            self._log_swallowed(exc, "sweep_stale_dirs")

    async def _resolve(self) -> None:
        """Resolve the repository against the cloud once on startup.

        ``sync_now`` short-circuits when HEAD is already indexed
        locally (the common reattach case), so without an explicit
        kick the resolver never runs and the panel shows
        install_state="unknown" forever.
        """
        session = self.session
        try:
            resolver = session.code_index_sync.resolver
            if resolver is not None:
                await resolver.resolve()
        except Exception as exc:
            self._log_swallowed(exc, "codeindex resolver kick")

    async def _initial_sync(self) -> None:
        """Run the initial sync — unconditional (``sync_now`` is
        already a fast no-op when HEAD is up to date)."""
        await self.session.code_index_sync.sync_now()

    def _refresh_availability_after_sync(self) -> None:
        """If the initial sync populated the chroma (most common
        case: fresh checkout, prior session wiped, first install),
        the agent built earlier in ``__init__`` has the wrong system
        prompt — recheck and rebuild if so.
        """
        refresh = self.session.refresh_codeindex_availability()
        if not refresh.ok:
            logger.debug(
                "refresh_codeindex_availability after initial sync failed (%s)",
                refresh.error,
            )

    async def _auto_clean(self) -> None:
        """Evict idle commit chromas per the 30-day cutoff."""
        try:
            dropped = await self.session.code_index.clean()
            if dropped:
                logger.info(
                    "Auto-clean dropped %d idle commit chroma(s): %s",
                    len(dropped),
                    ", ".join(s[:8] for s in dropped[:5]) + ("…" if len(dropped) > 5 else ""),
                )
        except Exception as exc:
            self._log_swallowed(exc, "Auto-clean")

    async def _start_watcher(self) -> None:
        """Start the HEAD watcher so branch flips keep the pool /
        main-team in sync."""
        await self.session.code_index_sync.start_watcher()
