"""OrphanRehydrator — startup-pass coordinator for orphan pids.

Extracted from :mod:`shell_orphan` per the OOP audit: the previous
``rehydrate_orphan_processes`` was a ~70-LoC procedural coroutine
that took a project_dir first arg, reached for the module-level
supervisor + built a store inside three broad ``except Exception``
blocks that collapsed three failure states into a bare ``int``.

Post-refactor:

* :class:`OrphanRehydrator` — takes a
  :class:`~ember_code.core.tools.process_supervisor.ProcessSupervisor`
  and a :class:`~ember_code.core.tools.process_store.BackgroundProcessStore`
  by constructor injection (composition, not module-level singleton
  reach-in) so tests can wire fake collaborators without touching
  the process-wide supervisor.
* :meth:`OrphanRehydrator.run` — single entry point returning a
  typed :class:`RehydrateResult`. Each failure branch (list_all,
  per-row remove) populates ``reason`` so
  :class:`RehydrateController.orphan_processes` can plumb it into
  the :class:`RehydrateOutcome`.

The store-init failure branch stays on the caller (or the thin
backward-compat wrapper in :mod:`shell_orphan`) because it happens
BEFORE we have a rehydrator.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ember_code.core.tools.finished_process import FinishedProcess
from ember_code.core.tools.orphan_process import OrphanProcess
from ember_code.core.tools.process_store import (
    BackgroundProcessRow,
    BackgroundProcessStore,
)
from ember_code.core.tools.process_supervisor import ProcessSupervisor
from ember_code.core.tools.shell_orphan_schemas import RehydrateResult

logger = logging.getLogger(__name__)

#: How long a finished process stays in the history. Seven days is the
#: span over which "what did that build do on Tuesday" is still a
#: question someone asks; past that the row is clutter and the table
#: would grow for the life of the project.
HISTORY_RETENTION_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class _LoadRowsOutcome:
    """Internal outcome of :meth:`OrphanRehydrator._load_rows`.

    Local to this module — collapses the "rows + optional reason"
    return into a single object so ``run()`` doesn't juggle a
    tuple. Not exported; the public typed result is
    :class:`RehydrateResult`.
    """

    rows: list[BackgroundProcessRow]
    error: str | None


class OrphanRehydrator:
    """Startup-pass coordinator that re-adopts pids the previous
    BE lifetime spawned.

    Reads the persisted background-process rows, probes each pid
    for liveness (via :meth:`OrphanProcess.probe_alive`), injects
    alive orphans into the supervisor's registry as
    :class:`OrphanProcess` instances, surfaces finished ones as
    :class:`FinishedProcess` history, and prunes history older than
    :data:`HISTORY_RETENTION_SECONDS` in the same pass.

    Safe to call multiple times: :meth:`ProcessRegistry.add` is
    idempotent on pid, and pids the registry already tracks are
    skipped so a double-run doesn't double-surface anything.
    """

    def __init__(
        self,
        supervisor: ProcessSupervisor,
        store: BackgroundProcessStore,
    ) -> None:
        self._supervisor = supervisor
        self._store = store

    async def run(self) -> RehydrateResult:
        """Execute the rehydrate pass.

        Returns a typed :class:`RehydrateResult` so the caller
        (:class:`RehydrateController.orphan_processes`) can log
        the surfaced/pruned counts and plumb a failure reason
        into :class:`RehydrateOutcome` instead of swallowing at
        DEBUG. Best-effort throughout — no exception escapes.

        The store now returns typed
        :class:`~ember_code.core.tools.process_store_schemas.ListResult`
        / :class:`RemoveResult` payloads, so DB failures surface
        via ``result.ok``/``result.reason`` instead of exceptions.
        The bounding try/except is retained as a defensive belt
        for duck-typed test stores that still raise directly.
        """
        self._supervisor.registry.attach_persistence(self._store)

        list_reason = await self._load_rows()
        if list_reason.error is not None:
            return RehydrateResult(ok=False, surfaced=0, pruned=0, reason=list_reason.error)
        rows = list_reason.rows

        surfaced = 0
        pruned = 0
        remove_error: str | None = None
        for row in rows:
            # Liveness policy lives on :class:`OrphanProcess` so
            # the same probe used inside :meth:`is_running` runs
            # here too — one source of truth.
            if not OrphanProcess.probe_alive(row.pid):
                # A dead pid used to mean "delete the row", which is
                # what made history impossible: the watcher lost every
                # record of what had run the moment the BE restarted.
                #
                # A row that recorded its own ending is history and is
                # surfaced as such. One that did not is a process the
                # previous BE was still watching when it exited — the
                # ending was real but nobody saw it, so it is stamped
                # now with an unknown exit code rather than being
                # quietly dropped or dressed up as a clean exit.
                if not row.is_finished:
                    await self._mark_interrupted(row.pid)
                    row = row.model_copy(update={"finished_at": int(time.time())})
                if self._supervisor.registry.get(row.pid) is None:
                    self._supervisor.registry.add(
                        FinishedProcess.from_row(row, log_store=self._supervisor.log_store)
                    )
                    surfaced += 1
                continue

            # Skip pids the in-process registry already tracks —
            # shouldn't happen on a clean boot (the registry is
            # fresh) but defends against the BE somehow already
            # having spawned the same pid (impossible in practice).
            if self._supervisor.registry.get(row.pid) is not None:
                continue

            orphan = OrphanProcess.from_row(row, log_store=self._supervisor.log_store)
            self._supervisor.registry.add(orphan)
            surfaced += 1

        pruned = await self._prune_expired_history()

        if surfaced:
            logger.info(
                "orphan rehydrate: surfaced %d background process(es) from prior BE lifetime",
                surfaced,
            )

        return RehydrateResult(
            ok=remove_error is None,
            surfaced=surfaced,
            pruned=pruned,
            reason=remove_error or "",
        )

    async def _load_rows(self) -> _LoadRowsOutcome:
        """Call ``store.list_all`` and wrap any DB failure into a
        typed outcome so :meth:`run` can plumb the reason into its
        :class:`RehydrateResult` without a redundant try/except.
        """
        try:
            rows = await self._store.list_all()
        except Exception as exc:
            logger.debug("orphan rehydrate: list_all failed: %s", exc)
            return _LoadRowsOutcome(rows=[], error=f"list_all: {exc}")
        return _LoadRowsOutcome(rows=list(rows), error=None)

    async def _prune_expired_history(self) -> int:
        """Drop history rows past the retention window.

        The dead-row prune this replaces ran on liveness, which is why
        history could not exist: every finished process was deleted the
        next time the BE started. Retention is the bound instead — old
        enough, not merely over.
        """
        prune = getattr(self._store, "prune_history", None)
        if prune is None:
            return 0
        try:
            return int(await prune(older_than_seconds=HISTORY_RETENTION_SECONDS))
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.debug("orphan rehydrate: prune_history failed: %s", exc)
            return 0

    async def _mark_interrupted(self, pid: int) -> None:
        """Stamp a row whose process died without us seeing it.

        ``exit_code=None`` says "it ended, we do not know how" — the
        one honest answer available after the BE was not running to
        observe it.
        """
        finish = getattr(self._store, "finish", None)
        if finish is None:
            return
        try:
            await finish(pid, None)
        except Exception as exc:  # noqa: BLE001 — best-effort, as everywhere here
            logger.debug("orphan rehydrate: finish pid=%s failed: %s", pid, exc)

    async def _prune_row(self, pid: int) -> str | None:
        """Call ``store.remove`` for a dead pid; return a failure
        reason string or ``None`` on success. Same typed /
        legacy / raising handling as :meth:`_load_rows`.
        """
        try:
            raw = await self._store.remove(pid)
        except Exception as exc:
            logger.debug("orphan rehydrate: remove pid=%s failed: %s", pid, exc)
            return f"remove(pid={pid}): {exc}"

        if hasattr(raw, "ok") and not raw.ok:
            reason = getattr(raw, "reason", "") or f"remove(pid={pid}) failed"
            logger.debug("orphan rehydrate: remove pid=%s reason: %s", pid, reason)
            return reason
        return None


# Convenience helper for the thin backward-compat wrapper in
# :mod:`shell_orphan`. Keeps the store-init failure branch out of
# :meth:`OrphanRehydrator.run` (which shouldn't be responsible for
# build failures of its own collaborator) while still routing the
# failure into a typed :class:`RehydrateResult`.
def build_rehydrator(
    supervisor: ProcessSupervisor,
    project_dir: object,
) -> tuple[OrphanRehydrator | None, RehydrateResult | None]:
    """Construct an :class:`OrphanRehydrator` from a project_dir.

    Returns ``(rehydrator, None)`` on success or ``(None, result)``
    with a populated ``reason`` when the store constructor
    raises. Kept as a module-level helper (not a classmethod) so
    the constructor stays a plain composition seam.
    """
    try:
        store = BackgroundProcessStore(project_dir=project_dir)
    except Exception as exc:
        logger.debug("orphan rehydrate: store init failed: %s", exc)
        return None, RehydrateResult(ok=False, surfaced=0, pruned=0, reason=f"store_init: {exc}")
    return OrphanRehydrator(supervisor, store), None
