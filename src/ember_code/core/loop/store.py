"""Per-project SQLite stores for the active ``/loop`` and its progress.

Both stores share the project's ``state.db`` (the same file the
scheduler writes to) so they're scoped per-project: switching
projects doesn't expose another project's loop or its progress.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, select

from ember_code.core.code_index.paths import state_db_path
from ember_code.core.config.settings import Settings
from ember_code.core.db.database import Database
from ember_code.core.loop.db_models import LoopProgressModel, LoopStateModel
from ember_code.core.loop.models import LoopState


def _resolve_db_path(
    db_path: str | Path | None,
    project_dir: str | Path | None,
) -> Path:
    """Pick a SQLite path. Mirrors the scheduler's resolver."""
    if db_path is not None:
        return Path(str(db_path)).expanduser()
    project = project_dir if project_dir is not None else Path.cwd()
    try:
        data_dir = Settings().storage.data_dir
    except Exception:
        data_dir = "~/.ember"
    return state_db_path(project, data_dir=data_dir)


# ── LoopStore ─────────────────────────────────────────────────────


class LoopStore:
    """Persists the active ``/loop`` state in the single-row
    ``loop_state`` table.

    Three operations: ``load`` reads the row (or ``None`` when no
    loop is active), ``save`` upserts the row, ``clear`` deletes
    it. Every state mutation on :class:`Session` calls ``save``
    (or ``clear``) so a restart sees the latest state.
    """

    _SINGLETON_ID = 1

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

    async def load(self) -> LoopState | None:
        async with self._db.session() as session:
            row = await session.get(LoopStateModel, self._SINGLETON_ID)
            if row is None:
                return None
            return LoopState(
                run_id=row.run_id,
                prompt=row.prompt,
                iteration_index=row.iteration_index,
                iterations_remaining=row.iterations_remaining,
                cap_explicit=row.cap_explicit,
            )

    async def save(self, state: LoopState) -> None:
        """Upsert the singleton row.

        Created on first ``save``; subsequent calls update in
        place — ``created_at`` is preserved from the first save,
        ``updated_at`` ticks every time.
        """
        now = datetime.now()
        async with self._db.session() as session, session.begin():
            row = await session.get(LoopStateModel, self._SINGLETON_ID)
            if row is None:
                session.add(
                    LoopStateModel(
                        id=self._SINGLETON_ID,
                        run_id=state.run_id,
                        prompt=state.prompt,
                        iteration_index=state.iteration_index,
                        iterations_remaining=state.iterations_remaining,
                        cap_explicit=state.cap_explicit,
                        created_at=now,
                        updated_at=now,
                    )
                )
                return
            row.run_id = state.run_id
            row.prompt = state.prompt
            row.iteration_index = state.iteration_index
            row.iterations_remaining = state.iterations_remaining
            row.cap_explicit = state.cap_explicit
            row.updated_at = now

    async def clear(self) -> bool:
        async with self._db.session() as session, session.begin():
            result = await session.execute(
                delete(LoopStateModel).where(LoopStateModel.id == self._SINGLETON_ID)
            )
            return (result.rowcount or 0) > 0


# ── LoopProgressStore ────────────────────────────────────────────


class LoopProgressStore:
    """Per-(run_id, key) key/value rows.

    The model uses this to mark sections as completed across
    iterations — iteration N reads the rows iteration N-1 wrote
    so it can skip work that's already done. ``run_id`` scopes
    every operation so a fresh loop run can't accidentally see
    progress from an older run.

    All methods take ``run_id`` explicitly (rather than reading
    from :class:`Session`) so the store stays mockable in tests
    and the tool layer is responsible for current-run resolution.
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
        self._db = Database(_resolve_db_path(db_path, project_dir))

    async def get(self, run_id: str, key: str) -> str | None:
        async with self._db.session() as session:
            result = await session.execute(
                select(LoopProgressModel.value).where(
                    LoopProgressModel.run_id == run_id,
                    LoopProgressModel.key == key,
                )
            )
            row = result.scalar_one_or_none()
            return row

    async def set(self, run_id: str, key: str, value: str) -> None:
        """Upsert a single (run_id, key) row.

        The model uses this on every "section verified" event;
        the row is keyed on (run_id, key) so calling ``set`` twice
        for the same section just updates the value (e.g. extra
        notes appended) rather than throwing on the unique
        constraint.
        """
        now = datetime.now()
        async with self._db.session() as session, session.begin():
            result = await session.execute(
                select(LoopProgressModel).where(
                    LoopProgressModel.run_id == run_id,
                    LoopProgressModel.key == key,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                session.add(
                    LoopProgressModel(
                        run_id=run_id,
                        key=key,
                        value=value,
                        created_at=now,
                        updated_at=now,
                    )
                )
                return
            row.value = value
            row.updated_at = now

    async def list(self, run_id: str) -> list[tuple[str, str]]:
        """Return all (key, value) pairs for ``run_id``, ordered
        by insertion time so iteration history reads chronologically."""
        async with self._db.session() as session:
            result = await session.execute(
                select(LoopProgressModel.key, LoopProgressModel.value)
                .where(LoopProgressModel.run_id == run_id)
                .order_by(LoopProgressModel.created_at)
            )
            return [(k, v) for k, v in result.all()]

    async def delete(self, run_id: str, key: str) -> bool:
        async with self._db.session() as session, session.begin():
            result = await session.execute(
                delete(LoopProgressModel).where(
                    LoopProgressModel.run_id == run_id,
                    LoopProgressModel.key == key,
                )
            )
            return (result.rowcount or 0) > 0

    async def clear(self, run_id: str) -> int:
        """Delete every progress row for ``run_id``. Returns the
        deleted-row count so the caller can surface "cleared N
        progress entries" in chat."""
        async with self._db.session() as session, session.begin():
            result = await session.execute(
                delete(LoopProgressModel).where(LoopProgressModel.run_id == run_id)
            )
            return result.rowcount or 0
