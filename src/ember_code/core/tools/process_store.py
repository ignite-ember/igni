"""Cross-restart persistence for backgrounded shell processes.

Mirrors :class:`LoopStore`'s shape — per-project ``state.db``,
sync upsert/delete API for callers that don't want to await (the
shell tool's spawn path is async; the registry's ``add()`` /
``remove()`` are sync). The model lives here next to
:mod:`ember_code.core.tools.shell` because it's an extension of
that subsystem; nothing outside the shell-tool / watcher path
should be touching it.

The orphan-pid rehydration is the load-bearing feature. Without
it, a BE restart silently drops every backgrounded child the
agent had running — the OS keeps them alive (they were spawned
with ``start_new_session=True``) but the watcher can't see them
and there's no kill button anymore. That's the
"why-is-port-3000-occupied" gap that motivated this module.

Wire / result models (``BackgroundProcessRow``, ``UpsertResult``,
``RemoveResult``, ``ListResult``) live in the sibling
:mod:`process_store_schemas` module per the sibling schemas
convention. They are re-exported here so the historical import
path ``from ember_code.core.tools.process_store import
BackgroundProcessRow`` keeps working for downstream callers.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import Integer, Text, delete, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Mapped, mapped_column

from ember_code.core.code_index.paths import state_db_path
from ember_code.core.config.settings import Settings
from ember_code.core.db.base import Base
from ember_code.core.db.database import Database
from ember_code.core.tools.process_store_schemas import (
    BackgroundProcessRow,
    ListResult,
    RemoveResult,
    UpsertResult,
)

logger = logging.getLogger(__name__)


# Re-export the schemas so ``from ember_code.core.tools.process_store
# import BackgroundProcessRow`` keeps working after the schema move.
__all__ = [
    "BackgroundProcessModel",
    "BackgroundProcessRow",
    "BackgroundProcessStore",
    "ListResult",
    "RemoveResult",
    "UpsertResult",
]


# ── ORM model ────────────────────────────────────────────────


class BackgroundProcessModel(Base):
    __tablename__ = "background_processes"

    pid: Mapped[int] = mapped_column(Integer, primary_key=True)
    cmd: Mapped[str] = mapped_column(Text, nullable=False)
    pgid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[int] = mapped_column(Integer, nullable=False)
    #: How it ended, and when. Null while it is still running — or,
    #: after a BE restart, if we never got to see it end.
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finished_at: Mapped[int | None] = mapped_column(Integer, nullable=True)


# ── Store ────────────────────────────────────────────────────


def _resolve_db_path(
    db_path: str | Path | None,
    project_dir: str | Path | None,
) -> Path:
    """Module-level shim delegating to
    :meth:`BackgroundProcessStore._resolve_db_path`.

    Kept as a module-level function because the test suite
    monkey-patches ``ps_mod._resolve_db_path`` for tmp-path
    isolation (see :mod:`tests.test_process_orphan_rehydrate`).
    The real logic lives on the class per the OOP audit;
    :meth:`BackgroundProcessStore.__init__` calls the module-level
    shim so the monkey-patch seam still fires.
    """
    return BackgroundProcessStore._resolve_db_path(db_path, project_dir)


class BackgroundProcessStore:
    """Persists the backgrounded-process registry to per-project
    ``state.db``. Methods are async; the shell tool's spawn path
    schedules them via ``asyncio.create_task`` from its sync
    registry callbacks so the hot path stays non-blocking.

    Public methods return typed
    :class:`~ember_code.core.tools.process_store_schemas.UpsertResult`
    / :class:`RemoveResult` / :class:`ListResult` payloads so DB
    failures at the store boundary become observable ``reason``
    strings instead of exceptions the caller has to guess about.
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        project_dir: str | Path | None = None,
        db: Database | None = None,
    ) -> None:
        if db is not None:
            self._db = db
            return
        # Route through the module-level shim so tests that
        # monkey-patch ``ps_mod._resolve_db_path`` still hit their
        # patched resolver.
        self._db = Database(_resolve_db_path(db_path, project_dir))

    @staticmethod
    def _resolve_db_path(
        db_path: str | Path | None,
        project_dir: str | Path | None,
    ) -> Path:
        """Mirror of LoopStore's resolver — same SQLite file as the
        scheduler / loop / Agno session use.

        Narrowed the previous bare ``except Exception`` to the
        specific Settings-load failure classes: ``ImportError``
        catches an unresolved lazy import inside the settings
        package, ``FileNotFoundError`` catches a missing config
        file the loader dereferences, and ``ValidationError``
        catches a broken user YAML that Pydantic rejects. Any
        other exception is a genuine bug and now propagates.
        """
        if db_path is not None:
            return Path(str(db_path)).expanduser()
        project = project_dir if project_dir is not None else Path.cwd()
        try:
            data_dir = Settings().storage.data_dir
        except (ImportError, FileNotFoundError, ValidationError):
            data_dir = "~/.ember"
        return state_db_path(project, data_dir=data_dir)

    async def upsert(self, row: BackgroundProcessRow) -> UpsertResult:
        """Add or replace a row keyed by pid. Replace (not insert-
        only) so a freak pid reuse doesn't blow up; the latest
        spawn wins.

        SQLite-specific ``ON CONFLICT DO UPDATE`` — same flavour
        the loop store uses. Other databases would need different
        upsert syntax but this code only ever runs against
        SQLite (the file is the project's ``state.db``).

        Returns a typed :class:`UpsertResult`; DB failures are
        translated to ``ok=False`` + ``reason`` at the store
        boundary so :meth:`ProcessRegistry._persist_add`
        (fire-and-forget) can log the reason at DEBUG without an
        ``except Exception`` at the callsite.
        """
        values = row.to_upsert_values()
        try:
            async with self._db.session() as session:
                stmt = insert(BackgroundProcessModel).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[BackgroundProcessModel.pid],
                    set_={
                        "cmd": row.cmd,
                        "pgid": row.pgid,
                        "started_at": row.started_at,
                        # A reused pid is a new process, not a
                        # resurrection: clear any ending recorded
                        # against the previous occupant of this row.
                        "exit_code": row.exit_code,
                        "finished_at": row.finished_at,
                    },
                )
                await session.execute(stmt)
                await session.commit()
        except Exception as exc:
            # Log with exc_info so the trace isn't lost when the
            # caller only sees the summarised ``reason`` string.
            logger.debug("upsert pid=%s failed", row.pid, exc_info=exc)
            return UpsertResult(ok=False, reason=f"upsert(pid={row.pid}): {exc}")
        return UpsertResult(ok=True)

    async def finish(
        self,
        pid: int,
        exit_code: int | None,
        *,
        now: int | None = None,
        row: BackgroundProcessRow | None = None,
    ) -> UpsertResult:
        """Record how a process ended, keeping its row.

        The counterpart to :meth:`remove`, and the reason the table can
        answer "what ran here" and not only "what is running". Called
        where ``_persist_remove`` used to be, on completion.

        A pid with no row is the common case rather than an error: only
        processes that were *announced* get written on spawn, and a
        quick `$ ls` is never announced. Pass ``row`` and the ending is
        inserted whole — command, start, exit code — which is what
        makes a one-shot survive a restart instead of existing only in
        memory until the BE stops.
        """
        stamp = int(time.time()) if now is None else now
        try:
            async with self._db.session() as session:
                result = await session.execute(
                    update(BackgroundProcessModel)
                    .where(BackgroundProcessModel.pid == pid)
                    .values(exit_code=exit_code, finished_at=stamp)
                )
                await session.commit()
                matched = result.rowcount if result.rowcount is not None else 0
        except Exception as exc:
            logger.debug("finish pid=%s failed", pid, exc_info=exc)
            return UpsertResult(ok=False, reason=f"finish(pid={pid}): {exc}")

        if matched == 0 and row is not None:
            return await self.upsert(
                row.model_copy(update={"exit_code": exit_code, "finished_at": stamp})
            )
        return UpsertResult(ok=True)

    async def prune_history(self, *, older_than_seconds: float, now: int | None = None) -> int:
        """Drop finished rows that ended longer ago than the cutoff.

        History has to be bounded or the table grows for the life of
        the project. Only rows with a ``finished_at`` are eligible:
        a running process is never old enough to forget, however long
        it has been up.

        Returns the number of rows removed.
        """
        stamp = int(time.time()) if now is None else now
        cutoff = stamp - int(older_than_seconds)
        try:
            async with self._db.session() as session:
                result = await session.execute(
                    delete(BackgroundProcessModel).where(
                        BackgroundProcessModel.finished_at.is_not(None),
                        BackgroundProcessModel.finished_at < cutoff,
                    )
                )
                await session.commit()
                return int(result.rowcount or 0)
        except Exception as exc:
            logger.debug("prune_history failed", exc_info=exc)
            return 0

    async def remove(self, pid: int) -> RemoveResult:
        """Delete the row for ``pid`` if present. No-op when the
        row's already gone — keeps the hot path's "fire-and-
        forget" semantics safe under races.

        Returns a typed :class:`RemoveResult`. ``removed=True``
        signals the delete actually affected a row; ``removed=False``
        means the pid wasn't present (still ``ok=True``).
        """
        try:
            async with self._db.session() as session:
                result = await session.execute(
                    delete(BackgroundProcessModel).where(BackgroundProcessModel.pid == pid)
                )
                await session.commit()
                # ``rowcount`` is provider-specific; SQLite exposes
                # a valid count here. Treat unknown (-1) as
                # "assume removed" to avoid a false negative.
                affected = result.rowcount if result.rowcount is not None else 0
        except Exception as exc:
            logger.debug("remove pid=%s failed", pid, exc_info=exc)
            return RemoveResult(ok=False, reason=f"remove(pid={pid}): {exc}")
        return RemoveResult(ok=True, removed=affected != 0)

    async def list_all(self) -> list[BackgroundProcessRow]:
        """Read every row, oldest first. Caller probes each pid
        for liveness and prunes the dead ones via :meth:`remove`.

        DB failures propagate; :meth:`OrphanRehydrator.run` wraps
        the call in its own try/except so a corrupt state DB doesn't
        crash startup.
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(BackgroundProcessModel).order_by(BackgroundProcessModel.started_at.asc())
            )
            orm_rows = result.scalars().all()
            return [BackgroundProcessRow.from_model(m) for m in orm_rows]
