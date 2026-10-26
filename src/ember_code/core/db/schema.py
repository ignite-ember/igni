"""Is this database's schema current — answered without alembic.

alembic, with the migration scripts it loads, is about 11 MB of a
backend's memory and only does work when a database is behind. So a
:class:`Database` first reads the database's revision through
SQLAlchemy and compares it with :data:`HEAD_REVISION`; only a database
that is new or behind gets :mod:`ember_code.core.db.migrations`
imported and run. A backend whose database is current never imports
alembic at all.

:data:`HEAD_REVISION` is a copy of alembic's head, and
``tests/test_migrations_stay_off_the_start_path.py`` fails the moment a
migration is added without updating it.

The cloud runs migrations once, when its database is prepared
(``igni migrate``), so its backends start with the schema current.
"""

from __future__ import annotations

import importlib
import threading

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ember_code.core.db.engine import cache
from ember_code.core.db.engine_registry import DatabaseTarget
from ember_code.core.db.urls import DatabaseUrl

#: alembic's head revision for ``ember_code/migrations``. Kept equal to
#: it by a test; update it with every new migration.
HEAD_REVISION = "f7c2a8e4b1d3"

#: Where the migrator lives. Imported by name, and only when a database
#: is behind — this is the database layer's one run-time import, the
#: counterpart of the slash-command registry's.
MIGRATIONS_MODULE = "ember_code.core.db.migrations"


class SchemaRevision:
    """The revision a database says it is at."""

    @staticmethod
    def of(target: DatabaseTarget) -> str | None:
        """``alembic_version.version_num``, or ``None`` for a database
        that does not exist or was never migrated.

        A SQLite file that does not exist is not created by asking.
        """
        url = DatabaseUrl.of(target)
        path = url.file
        if url.is_sqlite and (path is None or not path.is_file()):
            return None
        try:
            with cache.get_engine(url).connect() as connection:
                row = connection.execute(text("SELECT version_num FROM alembic_version")).first()
        except SQLAlchemyError:
            return None
        return str(row[0]) if row else None

    @classmethod
    def is_current(cls, target: DatabaseTarget) -> bool:
        return cls.of(target) == HEAD_REVISION


class SchemaKeeper:
    """Brings a database to the head revision, importing alembic only if it must.

    Remembers the databases it has seen current in this process, so a
    second :class:`Database` on the same one costs nothing.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: set[str] = set()

    def ensure_current(self, target: DatabaseTarget) -> None:
        url = DatabaseUrl.of(target)
        with self._lock:
            if url.url in self._current:
                return
            if not SchemaRevision.is_current(url):
                importlib.import_module(MIGRATIONS_MODULE).upgrade_to_head(url)
            self._current.add(url.url)


#: Process-wide keeper, shared by every :class:`Database`.
keeper = SchemaKeeper()


__all__ = ["HEAD_REVISION", "SchemaKeeper", "SchemaRevision", "keeper"]
