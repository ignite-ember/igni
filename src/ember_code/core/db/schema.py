"""Is this SQLite file's schema current — answered without alembic.

alembic, with the migration scripts it loads, is about 11 MB of a
backend's memory and only does work when a file is behind. So a
:class:`Database` first reads the file's revision with :mod:`sqlite3`
and compares it with :data:`HEAD_REVISION`; only a file that is new or
behind gets :mod:`ember_code.core.db.migrations` imported and run. A
backend whose files are current never imports alembic at all.

:data:`HEAD_REVISION` is a copy of alembic's head, and
``tests/test_migrations_stay_off_the_start_path.py`` fails the moment a
migration is added without updating it.

The cloud runs migrations once, when a producer's volume is prepared
(``igni migrate``), so its backends start with every file current.
"""

from __future__ import annotations

import importlib
import sqlite3
import threading
from pathlib import Path

#: alembic's head revision for ``ember_code/migrations``. Kept equal to
#: it by a test; update it with every new migration.
HEAD_REVISION = "c7f1a9e2d8b4"

#: Where the migrator lives. Imported by name, and only when a file is
#: behind — this is the database layer's one run-time import, the
#: counterpart of the slash-command registry's.
MIGRATIONS_MODULE = "ember_code.core.db.migrations"


class SchemaRevision:
    """The revision a SQLite file says it is at."""

    @staticmethod
    def of(db_path: Path) -> str | None:
        """``alembic_version.version_num``, or ``None`` for a file that
        does not exist or was never migrated."""
        if not db_path.is_file():
            return None
        try:
            connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            return None
        try:
            row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        except sqlite3.Error:
            return None
        finally:
            connection.close()
        return str(row[0]) if row else None

    @classmethod
    def is_current(cls, db_path: Path) -> bool:
        return cls.of(db_path) == HEAD_REVISION


class SchemaKeeper:
    """Brings a file to the head revision, importing alembic only if it must.

    Remembers the files it has seen current in this process, so a
    second :class:`Database` on the same file costs nothing.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: set[str] = set()

    def ensure_current(self, db_path: Path) -> None:
        resolved = db_path.expanduser().resolve()
        key = str(resolved)
        with self._lock:
            if key in self._current:
                return
            if not SchemaRevision.is_current(resolved):
                importlib.import_module(MIGRATIONS_MODULE).upgrade_to_head(resolved)
            self._current.add(key)


#: Process-wide keeper, shared by every :class:`Database`.
keeper = SchemaKeeper()


__all__ = ["HEAD_REVISION", "SchemaKeeper", "SchemaRevision", "keeper"]
