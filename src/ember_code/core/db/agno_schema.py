"""Agno's tables in a desktop database, brought up to the Agno igni runs on.

Agno 3 moved a session's runs out of its row and into a table of their
own (``<session_table>_runs``), and its migration (``MigrationManager``,
``agno/db/migrations/versions/v3_0_0.py``) copies them across. Nothing runs
it for you: a database Agno 2.6 wrote opens, its sessions read back —
Agno merges the old in-row runs with the new table — but until it is
migrated a history igni clears or cuts (``run_history``) comes back from
the old copy on the next read. So the first time a process opens an
existing database, its Agno tables are migrated, before any session is
read.

The migration leaves the old ``runs`` column in place as a backup (Agno
drops it only on ``cleanup_legacy_runs_column``), so a migrated database
can still be read by igni built on Agno 2.6, minus the runs written since.

PostgreSQL is not migrated here: the cloud's schema is made by
``igni migrate`` as the admin role, and so is this.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any, ClassVar

from agno.db.migrations.manager import MigrationManager
from packaging.version import Version
from sqlalchemy import inspect, text
from agno.db.sqlite import AsyncSqliteDb

from ember_code.core.activity import background

logger = logging.getLogger(__name__)


class AgnoSchema:
    """Migrates Agno's tables in a SQLite file once per process."""

    #: The Agno tables igni writes: sessions (and their runs), memories, learnings.
    TABLE_TYPES: ClassVar[tuple[str, ...]] = ("sessions", "memories", "learnings")

    _lock: ClassVar[threading.Lock] = threading.Lock()
    _current: ClassVar[set[str]] = set()

    @classmethod
    def ensure_current(cls, path: Path, *, session_table: str, memory_table: str) -> None:
        """Migrate ``path``'s Agno tables to Agno's current schema, if it has any."""
        if not path.exists():
            # A new file: Agno creates its tables at the current version.
            return
        key = str(path.resolve())
        with cls._lock:
            if key in cls._current:
                return
            # A thread of its own, with its own loop and its own engine: the
            # caller may be inside a running loop, and blocks until it is done
            # so no session is read from a half-migrated file.
            failure: list[BaseException] = []
            worker = background.thread(
                lambda: cls._run(path, session_table, memory_table, failure),
                name="agno-schema-migration",
            )
            worker.join()
            if failure:
                raise failure[0]
            cls._current.add(key)

    @classmethod
    def _run(
        cls, path: Path, session_table: str, memory_table: str, failure: list[BaseException]
    ) -> None:
        try:
            asyncio.run(cls._upgrade(path, session_table, memory_table))
        except BaseException as exc:  # noqa: BLE001 — handed to the caller's thread
            failure.append(exc)

    @staticmethod
    async def retire_legacy_runs(db: Any, session_table: str) -> None:
        """Empty the old in-row copy of each session's runs, once they are in their table.

        Agno reads a session's runs from the runs table *merged with* that
        copy whenever it is not empty, so a run igni deletes (compaction, a
        rewind) would come back from it. The migration copies every run
        across and leaves the column as a backup; emptied here, the table is
        the one history. The column stays, so a database still opens in igni
        on Agno 2.6, without the history (see the module docstring).
        """
        versions = await db.get_latest_schema_version(session_table)
        if versions is None or Version(versions) < Version("3.0.0"):
            return
        schema = getattr(db, "db_schema", None)
        async with db.db_engine.begin() as connection:
            columns = await connection.run_sync(
                lambda sync: [c["name"] for c in inspect(sync).get_columns(session_table, schema=schema)]
            )
            if "runs" not in columns:
                return
            # Agno's own SQLAlchemy table no longer declares the column, so
            # it is named here; both names are igni's own constants.
            qualified = f'"{schema}"."{session_table}"' if schema else f'"{session_table}"'
            await connection.execute(
                text(f"UPDATE {qualified} SET runs = NULL WHERE runs IS NOT NULL")  # noqa: S608
            )

    @classmethod
    async def _upgrade(cls, path: Path, session_table: str, memory_table: str) -> None:
        db = AsyncSqliteDb(
            db_file=str(path), session_table=session_table, memory_table=memory_table
        )
        # Agno narrates each step at INFO on stdout — the stream the backend's
        # ready line and its protocol travel on. Its warnings still show.
        agno_log = logging.getLogger("agno")
        level = agno_log.level
        agno_log.setLevel(max(level, logging.WARNING))
        try:
            migrations = MigrationManager(db)
            for table_type in cls.TABLE_TYPES:
                await migrations.up(table_type=table_type)
            await cls.retire_legacy_runs(db, session_table)
        finally:
            agno_log.setLevel(level)
            await db.db_engine.dispose()


__all__ = ["AgnoSchema"]
