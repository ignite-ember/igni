"""Per-project SQLite-backed store for scheduled tasks (async via SQLAlchemy).

Tasks live in ``~/.igni/projects/<project_hash>/state.db`` so each project
has its own scheduler queue — switching projects doesn't surface another
project's pending ``/loop`` jobs.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from ember_code.core.config.settings import Settings
from ember_code.core.data_paths import state_db_path
from ember_code.core.db.database import Database
from ember_code.core.paths import DEFAULT_DATA_DIR
from ember_code.core.scheduler.db_models import ScheduledTaskModel
from ember_code.core.scheduler.models import ScheduledTask, TaskStatus
from ember_code.core.scheduler.recurrence import Recurrence

logger = logging.getLogger(__name__)


def _resolve_db_path(
    db_path: str | Path | None,
    project_dir: str | Path | None,
) -> Path:
    """Pick a SQLite path. Explicit ``db_path`` wins; otherwise derive from project."""
    if db_path is not None:
        return Path(str(db_path)).expanduser()
    project = project_dir if project_dir is not None else Path.cwd()
    try:
        data_dir = Settings().storage.data_dir
    except Exception:
        data_dir = DEFAULT_DATA_DIR
    return state_db_path(project, data_dir=data_dir)


class TaskStore:
    """Persists scheduled tasks in a per-project SQLite file (async)."""

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
        path = _resolve_db_path(db_path, project_dir)
        self._db = Database(path)

    async def add(self, task: ScheduledTask) -> None:
        async with self._db.session() as session, session.begin():
            session.add(_task_to_row(task))

    async def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        result: str = "",
        error: str = "",
        *,
        unless_cancelled: bool = False,
    ) -> bool:
        """Write a task's status. Returns ``True`` if the write landed.

        ``unless_cancelled`` makes the write lose to a cancel that
        arrived first. The runner needs it: its execution window is up
        to ``task_timeout`` (300s by default) and a user only reaches
        for cancel *because* the task is misbehaving, so an
        unconditional terminal write at the end of that window silently
        overwrote the ``cancelled`` the user had just set — and the
        recurring task the cancel promised to stop was then rescheduled.
        The read and the write share one transaction, so the cancel
        either commits before it (and wins) or after it (and stands).
        """
        async with self._db.session() as session, session.begin():
            row = await session.get(ScheduledTaskModel, task_id)
            if row is None:
                return False
            if unless_cancelled and row.status == TaskStatus.cancelled.value:
                return False
            row.status = status.value
            row.result = result
            row.error = error
            return True

    async def remove(self, task_id: str) -> bool:
        async with self._db.session() as session, session.begin():
            result = await session.execute(
                delete(ScheduledTaskModel).where(ScheduledTaskModel.id == task_id)
            )
            return (result.rowcount or 0) > 0

    async def get_due_tasks(self) -> list[ScheduledTask]:
        """Get all pending tasks whose scheduled time has passed."""
        now = datetime.now()
        async with self._db.session() as session:
            result = await session.execute(
                select(ScheduledTaskModel)
                .where(
                    ScheduledTaskModel.status == TaskStatus.pending.value,
                    ScheduledTaskModel.scheduled_at <= now,
                )
                .order_by(ScheduledTaskModel.scheduled_at)
            )
            rows = result.scalars().all()
        return [_row_to_task(r) for r in rows]

    async def get_all(self, include_done: bool = False) -> list[ScheduledTask]:
        """Get all tasks, optionally including completed/failed/cancelled.

        Ordered by ``scheduled_at`` descending — tasks set to run
        furthest in the future surface first. Both the TUI tasks
        panel and the agent-facing ``list_scheduled_tasks`` tool
        render the store's order verbatim.
        """
        stmt = select(ScheduledTaskModel).order_by(ScheduledTaskModel.scheduled_at.desc())
        if not include_done:
            stmt = stmt.where(
                ScheduledTaskModel.status.in_((TaskStatus.pending.value, TaskStatus.running.value))
            )
        async with self._db.session() as session:
            result = await session.execute(stmt)
            rows = result.scalars().all()
        return [_row_to_task(r) for r in rows]

    async def get(self, task_id: str) -> ScheduledTask | None:
        async with self._db.session() as session:
            row = await session.get(ScheduledTaskModel, task_id)
        return _row_to_task(row) if row else None


def _task_to_row(task: ScheduledTask) -> ScheduledTaskModel:
    return ScheduledTaskModel(
        id=task.id,
        description=task.description,
        scheduled_at=task.scheduled_at,
        created_at=task.created_at,
        status=task.status.value,
        result=task.result,
        error=task.error,
        recurrence=task.recurrence,
    )


def _canonical_recurrence(raw: str) -> str:
    """Normalize a persisted recurrence that predates the canonical form.

    Rows written before :class:`ScheduledTask` validated the field hold
    user-facing phrases like ``daily`` / ``weekly`` — the model's own
    docstring used to give those as examples. Constructing the model
    from such a row raises ``ValidationError``, and that error escapes
    ``get_due_tasks`` into the poll loop's catch-all, so a single legacy
    row stopped *every* task in the project from running again. Convert
    what parses, drop what does not, keep the loop alive.
    """
    if not raw or Recurrence.from_canonical(raw) is not None:
        return raw
    parsed = Recurrence.parse(raw)
    if parsed is not None:
        return parsed.recurrence.canonical()
    logger.warning("Dropping unparseable recurrence %r — task becomes one-shot", raw)
    return ""


def _row_to_task(row: Any) -> ScheduledTask:
    return ScheduledTask(
        id=row.id,
        description=row.description,
        scheduled_at=row.scheduled_at,
        created_at=row.created_at,
        status=TaskStatus(row.status),
        result=row.result or "",
        error=row.error or "",
        recurrence=_canonical_recurrence(row.recurrence or ""),
    )
