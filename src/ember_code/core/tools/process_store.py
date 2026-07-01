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
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import Integer, Text, delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Mapped, mapped_column

from ember_code.core.code_index.paths import state_db_path
from ember_code.core.config.settings import Settings
from ember_code.core.db.base import Base
from ember_code.core.db.database import Database

logger = logging.getLogger(__name__)


# ── ORM model ────────────────────────────────────────────────


class BackgroundProcessModel(Base):
    __tablename__ = "background_processes"

    pid: Mapped[int] = mapped_column(Integer, primary_key=True)
    cmd: Mapped[str] = mapped_column(Text, nullable=False)
    pgid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[int] = mapped_column(Integer, nullable=False)


# ── Domain row ───────────────────────────────────────────────


@dataclass(frozen=True)
class BackgroundProcessRow:
    """In-memory shape of a persisted background-process row.
    Frozen so the store hands out immutable snapshots."""

    pid: int
    cmd: str
    pgid: int | None
    started_at: int  # epoch seconds


# ── Store ────────────────────────────────────────────────────


def _resolve_db_path(
    db_path: str | Path | None,
    project_dir: str | Path | None,
) -> Path:
    """Mirror of LoopStore's resolver — same SQLite file as the
    scheduler / loop / Agno session use."""
    if db_path is not None:
        return Path(str(db_path)).expanduser()
    project = project_dir if project_dir is not None else Path.cwd()
    try:
        data_dir = Settings().storage.data_dir
    except Exception:
        data_dir = "~/.ember"
    return state_db_path(project, data_dir=data_dir)


class BackgroundProcessStore:
    """Persists the backgrounded-process registry to per-project
    ``state.db``. Methods are async; the shell tool's spawn path
    schedules them via ``asyncio.create_task`` from its sync
    registry callbacks so the hot path stays non-blocking."""

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
        self._db = Database(_resolve_db_path(db_path, project_dir))

    async def upsert(self, row: BackgroundProcessRow) -> None:
        """Add or replace a row keyed by pid. Replace (not insert-
        only) so a freak pid reuse doesn't blow up; the latest
        spawn wins.

        SQLite-specific ``ON CONFLICT DO UPDATE`` — same flavour
        the loop store uses. Other databases would need different
        upsert syntax but this code only ever runs against
        SQLite (the file is the project's ``state.db``).
        """
        async with self._db.session() as session:
            stmt = insert(BackgroundProcessModel).values(
                pid=row.pid,
                cmd=row.cmd,
                pgid=row.pgid,
                started_at=row.started_at,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[BackgroundProcessModel.pid],
                set_={
                    "cmd": row.cmd,
                    "pgid": row.pgid,
                    "started_at": row.started_at,
                },
            )
            await session.execute(stmt)
            await session.commit()

    async def remove(self, pid: int) -> None:
        """Delete the row for ``pid`` if present. No-op when the
        row's already gone — keeps the hot path's "fire-and-
        forget" semantics safe under races."""
        async with self._db.session() as session:
            await session.execute(
                delete(BackgroundProcessModel).where(BackgroundProcessModel.pid == pid)
            )
            await session.commit()

    async def list_all(self) -> list[BackgroundProcessRow]:
        """Read every row, oldest first. Caller probes each pid
        for liveness and prunes the dead ones via :meth:`remove`."""
        async with self._db.session() as session:
            result = await session.execute(
                select(BackgroundProcessModel).order_by(BackgroundProcessModel.started_at.asc())
            )
            rows = result.scalars().all()
            return [
                BackgroundProcessRow(
                    pid=r.pid,
                    cmd=r.cmd,
                    pgid=r.pgid,
                    started_at=r.started_at,
                )
                for r in rows
            ]


def now_epoch() -> int:
    """``int(time.time())`` — extracted as a helper so tests can
    monkeypatch the clock without reaching into ``time``
    everywhere."""
    return int(time.time())


# Silence unused-import warnings — keeping ``datetime`` available
# for future migrations on this module.
_ = datetime
