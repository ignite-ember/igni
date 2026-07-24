"""Background HEAD watcher that fires ``sync_now`` on branch changes.

Extracted from :meth:`CodeIndexSyncManager._watch_loop` /
``start_watcher`` / ``stop_watcher``. Now a self-contained class
that owns:

* the :class:`asyncio.Task` lifecycle,
* the 1Hz poll cadence,
* the two retrigger predicates (HEAD moved vs. IN_PROGRESS
  retry due) — expressed as :meth:`_should_sync`,
* the post-sync ledger update (mark IN_PROGRESS or clear).

Dependencies are injected via the constructor so the watcher
never reaches back into the manager's private state: it
receives a ``get_head`` callable, an ``run_sync`` async
callable, a shared :class:`InProgressRetryLedger`, and a getter
for the manager's ``last_synced_sha`` (still lives on the
manager because ``_sync_locked`` mutates it).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from ember_code.core.code_index.sync.retry_ledger import InProgressRetryLedger
from ember_code.core.code_index.sync.schemas import SyncResult

logger = logging.getLogger(__name__)


class HeadWatcher:
    """Poll ``git HEAD`` and dispatch ``sync_now`` on relevant changes.

    Two trigger conditions for calling ``run_sync``:

    - HEAD moved: the new sha hasn't been synced yet and isn't
      already the sha we're polling in_progress.
    - Retry due: the interval on
      :class:`InProgressRetryLedger` elapsed for the current
      in_progress sha (still equal to HEAD).

    ``git rev-parse HEAD`` is microseconds; the only network
    call is ``run_sync`` itself, and that only fires when one
    of the triggers above is true.
    """

    DEFAULT_INTERVAL_SECONDS: float = 1.0

    def __init__(
        self,
        *,
        get_head: Callable[[], str | None],
        run_sync: Callable[[str], Awaitable[SyncResult]],
        retry_ledger: InProgressRetryLedger,
        last_synced_sha_getter: Callable[[], str | None],
        interval_seconds: float | None = None,
    ) -> None:
        self._get_head = get_head
        self._run_sync = run_sync
        self._retry_ledger = retry_ledger
        self._last_synced_sha_getter = last_synced_sha_getter
        self._interval = (
            interval_seconds if interval_seconds is not None else self.DEFAULT_INTERVAL_SECONDS
        )
        self._task: asyncio.Task | None = None

    async def start(self, *, interval_seconds: float | None = None) -> None:
        """Begin polling. No-op if the loop is already running."""
        if self._task is not None and not self._task.done():
            return
        if interval_seconds is not None:
            self._interval = interval_seconds
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Cancel the poll task and await its teardown."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task
        self._task = None

    def _should_sync(self, sha: str, *, now: float) -> bool:
        """Compose the HEAD-changed / retry-due predicates."""
        head_changed = (
            sha != self._last_synced_sha_getter() and sha != self._retry_ledger.in_progress_sha
        )
        return head_changed or self._retry_ledger.should_retry(sha, now=now)

    async def _loop(self) -> None:
        """1Hz local poll on git HEAD.

        ``get_head`` is offloaded via :func:`asyncio.to_thread`
        so every tick doesn't pause the BE event loop for the
        duration of ``git rev-parse``.
        """
        loop = asyncio.get_running_loop()
        while True:
            try:
                await asyncio.sleep(self._interval)
                sha = await asyncio.to_thread(self._get_head)
                if not sha:
                    continue

                # HEAD moved away from the in_progress sha → drop retry state.
                if self._retry_ledger.in_progress_sha and not self._retry_ledger.matches(sha):
                    self._retry_ledger.clear()

                if not self._should_sync(sha, now=loop.time()):
                    continue

                result = await self._run_sync(sha)
                if result.in_progress:
                    self._retry_ledger.mark(sha, now=loop.time())
                else:
                    self._retry_ledger.clear()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover — defensive
                logger.exception("HEAD watcher iteration failed")


__all__ = ["HeadWatcher"]
