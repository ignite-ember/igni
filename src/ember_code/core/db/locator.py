"""Where igni's state lives: files on the desktop, one database in the cloud.

The one place that turns configuration into a :class:`Database`:

* ``storage.database_url`` unset (the desktop) — a project's state is
  its ``state.db`` under ``<data_dir>/projects/<id>/``, and what is not
  any one project's (which folder a session is in, window state, local
  notifications) is the global ``<data_dir>/ember.db``. Unchanged paths,
  the empty scope.
* ``storage.database_url`` set (``IGNI_DATABASE_URL``; PostgreSQL in the
  cloud) — every project and the global state are the same database,
  told apart by the scope columns: the signed-in producer and org from
  :data:`~ember_code.core.db.scope.tenancy`, the project from the
  session's folder.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from ember_code.core.config.settings import Settings
from ember_code.core.data_paths import global_db_path, state_db_path
from ember_code.core.db.database import Database
from ember_code.core.db.scope import StorageScope, tenancy
from ember_code.core.db.urls import DatabaseUrl
from ember_code.core.paths import DEFAULT_DATA_DIR


class StorageLocator:
    """Turns ``storage.database_url`` and ``storage.data_dir`` into databases."""

    def __init__(self, *, database_url: str | None, data_dir: str | Path) -> None:
        self._database_url = DatabaseUrl.parse(database_url) if database_url else None
        self._data_dir = data_dir

    @classmethod
    def from_settings(cls, settings: Settings) -> StorageLocator:
        return cls(
            database_url=settings.storage.database_url,
            data_dir=settings.storage.data_dir,
        )

    @classmethod
    def current(cls) -> StorageLocator:
        """From this process's settings, for a store built without any.

        A configuration that does not load falls back to the defaults —
        the data directory igni would use anyway, and the database URL
        straight from the environment, which is where a deployment puts it.
        """
        try:
            return cls.from_settings(Settings())
        except (ImportError, FileNotFoundError, ValidationError):
            return cls(database_url=None, data_dir=DEFAULT_DATA_DIR)

    @property
    def shared(self) -> bool:
        """Several producers' rows in one database — the cloud."""
        return self._database_url is not None

    def project_url(self, project_dir: str | Path) -> DatabaseUrl:
        if self._database_url is not None:
            return self._database_url
        return DatabaseUrl.for_file(state_db_path(project_dir, data_dir=self._data_dir))

    def global_url(self) -> DatabaseUrl:
        if self._database_url is not None:
            return self._database_url
        return DatabaseUrl.for_file(global_db_path(self._data_dir))

    def scope(self, project_dir: str | Path | None = None) -> StorageScope:
        """Whose rows: nobody's in particular on the desktop, the
        signed-in producer's (in ``project_dir``) where they are shared."""
        return tenancy.scope(shared=self.shared).for_project(project_dir)

    def project_database(self, project_dir: str | Path | None = None) -> Database:
        """The database a project's state is in, scoped to the project."""
        project = project_dir if project_dir is not None else Path.cwd()
        return Database(self.project_url(project), scope=self.scope(project))

    def global_database(self) -> Database:
        """The database for what belongs to no one project."""
        return Database(self.global_url(), scope=self.scope().without_project())

    @classmethod
    def database_for(
        cls,
        db_path: str | Path | None,
        project_dir: str | Path | None,
    ) -> Database:
        """For a store's ``(db_path, project_dir)`` constructor pair.

        An explicit ``db_path`` is a file a caller (a test, usually)
        chose, and is the desktop's; otherwise the project's database
        from the current settings.
        """
        if db_path is not None:
            return Database(db_path)
        return cls.current().project_database(project_dir)


__all__ = ["StorageLocator"]
