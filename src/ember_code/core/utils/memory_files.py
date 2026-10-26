"""Where a project's ``MEMORY.md`` memory files are kept.

On the desktop they are files — ``~/.igni/projects/<slug>/memory/`` —
and stay exactly that (:class:`DirectoryMemoryFiles`). In the cloud a
producer's VM is a cache that can be lost, so they are rows in the
shared database, one per file and scope (:class:`DatabaseMemoryFiles`,
``docs/CLOUD.md`` section 9). :class:`MemoryFiles` is what both answer
to; :meth:`MemoryFiles.for_project` picks by configuration.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path

from sqlalchemy import Integer, PrimaryKeyConstraint, String, Text, delete, select
from sqlalchemy.orm import Mapped, mapped_column

from ember_code.core.db.base import Base
from ember_code.core.db.database import Database
from ember_code.core.db.locator import StorageLocator
from ember_code.core.db.scope import ScopedColumns
from ember_code.core.db.upsert import ConflictInsert

logger = logging.getLogger(__name__)


class MemoryFileModel(ScopedColumns, Base):
    """One memory file of one project, in the cloud."""

    __tablename__ = "memory_files"
    __table_args__ = (
        PrimaryKeyConstraint("org", "producer", "project", "name", name="pk_memory_files"),
    )

    #: The file's name in the memory directory, e.g. ``MEMORY.md``.
    name: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class MemoryFiles(ABC):
    """A project's memory files, by name."""

    @abstractmethod
    def read(self, name: str) -> str | None:
        """The file's text, or ``None`` when there is no such file."""

    @abstractmethod
    def write(self, name: str, content: str) -> None: ...

    @abstractmethod
    def names(self) -> list[str]:
        """Every file's name, sorted."""

    @abstractmethod
    def delete(self, name: str) -> bool:
        """``True`` when there was a file to delete."""

    @staticmethod
    def for_project(
        project_dir: Path, directory: Path, locator: StorageLocator | None = None
    ) -> MemoryFiles:
        """``directory`` on the desktop; the shared database in the cloud."""
        storage = locator if locator is not None else StorageLocator.current()
        if storage.shared:
            return DatabaseMemoryFiles(storage.project_database(project_dir))
        return DirectoryMemoryFiles(directory)


class DirectoryMemoryFiles(MemoryFiles):
    """The desktop's: plain files in one directory."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    @property
    def directory(self) -> Path:
        return self._directory

    def read(self, name: str) -> str | None:
        path = self._directory / name
        try:
            if not path.is_file():
                return None
            return path.read_text()
        except (OSError, UnicodeDecodeError):
            return None

    def write(self, name: str, content: str) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        (self._directory / name).write_text(content)

    def names(self) -> list[str]:
        if not self._directory.is_dir():
            return []
        return sorted(p.name for p in self._directory.iterdir() if p.is_file())

    def delete(self, name: str) -> bool:
        path = self._directory / name
        if not path.is_file():
            return False
        path.unlink()
        return True


class DatabaseMemoryFiles(MemoryFiles):
    """The cloud's: one row per file, in the project's scope."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def read(self, name: str) -> str | None:
        with self._db.sync_session() as session:
            return session.scalar(
                select(MemoryFileModel.content).where(
                    self._db.scope.where(MemoryFileModel), MemoryFileModel.name == name
                )
            )

    def write(self, name: str, content: str) -> None:
        now = int(time.time())
        with self._db.sync_session() as session, session.begin():
            stmt = ConflictInsert.into(session, MemoryFileModel).values(
                **self._db.scope.stamp(), name=name, content=content, updated_at=now
            )
            session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["org", "producer", "project", "name"],
                    set_={"content": content, "updated_at": now},
                )
            )

    def names(self) -> list[str]:
        with self._db.sync_session() as session:
            names = session.scalars(
                select(MemoryFileModel.name).where(self._db.scope.where(MemoryFileModel))
            ).all()
        # Sorted here, not by the database: a server's collation may
        # ignore case, and the desktop's directory listing does not.
        return sorted(names)

    def delete(self, name: str) -> bool:
        with self._db.sync_session() as session, session.begin():
            result = session.execute(
                delete(MemoryFileModel).where(
                    self._db.scope.where(MemoryFileModel), MemoryFileModel.name == name
                )
            )
            return bool(result.rowcount)  # type: ignore[attr-defined]


__all__ = [
    "DatabaseMemoryFiles",
    "DirectoryMemoryFiles",
    "MemoryFileModel",
    "MemoryFiles",
]
