"""What a shared database of one SQL dialect needs beyond SQLAlchemy.

``docs/CLOUD.md`` §9. igni's own stores speak SQLAlchemy and work on any
dialect it accepts (:class:`~ember_code.core.db.urls.DatabaseUrl`); a few
things do not: Agno's storage for that dialect, where attachments go,
and what an admin's ``igni migrate`` prepares besides igni's tables. A
:class:`SharedStorage` answers those for one dialect. SQLite's is the
core's own; another dialect's is the entry point of its name in the
``igni.storage`` group, which a package declares — PostgreSQL's is
``igni-cloud``'s. Chosen by the URL, not by where igni runs: a desktop
with ``igni-cloud`` installed and a PostgreSQL URL works too, and one
without it is told what to install.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from agno.db import sqlite as _agno_sqlite

from ember_code.core.db.urls import DatabaseUrl, Dialect

if TYPE_CHECKING:
    from ember_code.core.db.database import Database
    from ember_code.core.db.scope import StorageScope


class SharedStorage(ABC):
    """One dialect's Agno storage, attachments and admin preparation."""

    @abstractmethod
    def agno_db(
        self,
        *,
        url: DatabaseUrl,
        scope: StorageScope,
        session_table: str,
        memory_table: str,
    ) -> Any:
        """Agno's storage on ``url``, for ``scope``'s rows."""

    def attachments(self, db: Database, project_dir: Path) -> Any:
        """Where uploads go in this database; ``None`` keeps them as files in the project."""
        return None

    def prepare(self, admin: DatabaseUrl) -> None:  # noqa: B027 - nothing, unless a dialect says
        """What ``igni migrate`` does as the admin role after igni's own tables."""


class SqliteStorage(SharedStorage):
    """A shared SQLite file (tests): Agno's own SQLite storage, unscoped."""

    def agno_db(
        self,
        *,
        url: DatabaseUrl,
        scope: StorageScope,
        session_table: str,
        memory_table: str,
    ) -> Any:
        assert url.file is not None
        url.file.parent.mkdir(parents=True, exist_ok=True)
        return _agno_sqlite.AsyncSqliteDb(
            db_file=str(url.file), session_table=session_table, memory_table=memory_table
        )


class StorageUnavailable(RuntimeError):
    """No installed package provides storage for the database's dialect."""


class SharedStorages:
    """Each dialect's :class:`SharedStorage`, found once."""

    GROUP: ClassVar[str] = "igni.storage"

    _lock: ClassVar[threading.Lock] = threading.Lock()
    _found: ClassVar[dict[Dialect, SharedStorage]] = {}

    @classmethod
    def for_url(cls, url: DatabaseUrl) -> SharedStorage:
        return cls.for_dialect(url.dialect)

    @classmethod
    def for_dialect(cls, dialect: Dialect) -> SharedStorage:
        with cls._lock:
            found = cls._found.get(dialect)
            if found is None:
                found = cls._find(dialect)
                cls._found[dialect] = found
            return found

    @classmethod
    def install(cls, dialect: Dialect, storage: SharedStorage) -> None:
        with cls._lock:
            cls._found[dialect] = storage

    @classmethod
    def _find(cls, dialect: Dialect) -> SharedStorage:
        if dialect is Dialect.SQLITE:
            return SqliteStorage()
        declared = [ep for ep in entry_points(group=cls.GROUP) if ep.name == dialect.value]
        if not declared:
            raise StorageUnavailable(
                f"no installed package stores igni's state in {dialect.value}: install "
                "igni-cloud for PostgreSQL"
            )
        storage = declared[0].load()()
        if not isinstance(storage, SharedStorage):
            raise StorageUnavailable(f"{declared[0].value} is not a SharedStorage")
        return storage


__all__ = ["SharedStorage", "SharedStorages", "SqliteStorage", "StorageUnavailable"]
