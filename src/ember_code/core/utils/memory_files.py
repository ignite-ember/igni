"""Where a project's ``MEMORY.md`` memory files are kept.

On the desktop they are files — ``~/.igni/projects/<slug>/memory/`` —
and stay exactly that (:class:`DirectoryMemoryFiles`). In the cloud a
producer's VM is a cache that can be lost, so they are rows in the
shared database, one per file and scope (:class:`DatabaseMemoryFiles`,
``docs/CLOUD.md`` section 9). :class:`MemoryFiles` is what both answer
to; :meth:`MemoryFiles.for_project` picks by configuration.

**How the agent writes them in the cloud.** The agent writes memory
files the way Claude Code's agent does: with its ordinary file tools, to
the memory directory its instructions name — and a shell command can
write there as well as ``save_file`` can. No tool of its own could stop
that, so the store is not a tool: :class:`MemoryMirror` makes the
directory a write-through cache of the rows. At the start of a session
it is filled from the database; after every tool call the main agent
makes (``core/hooks/memory_sync.py``) and after every run it is compared
with what the database holds, and each file written, changed or removed
— by whatever tool — becomes the row's new state. A ``MEMORY.md``
written with ``save_file`` or ``echo >`` in the cloud lands in the
database like any other write; there is no way around the store.
"""

from __future__ import annotations

import hashlib
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


class MemoryMirror:
    """The cloud's memory directory, written through to the database.

    The directory is what the agent's tools see; the rows are what lasts.
    :meth:`fill` makes the directory the rows, :meth:`sync` makes the
    rows the directory. Only plain files directly in the directory are
    memory files, as on the desktop.
    """

    def __init__(self, files: MemoryFiles, directory: Path) -> None:
        self._files = files
        self._directory = directory
        #: What the database holds, by name, as a digest of the text.
        self._known: dict[str, str] = {}

    @property
    def directory(self) -> Path:
        return self._directory

    @staticmethod
    def _digest(content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()

    def fill(self) -> int:
        """Write every stored file into the directory. Returns how many."""
        self._directory.mkdir(parents=True, exist_ok=True)
        stored = {name: self._files.read(name) or "" for name in self._files.names()}
        for path in self._directory.iterdir():
            if path.is_file() and path.name not in stored:
                path.unlink()
        for name, content in stored.items():
            (self._directory / name).write_text(content)
        self._known = {name: self._digest(content) for name, content in stored.items()}
        return len(stored)

    def sync(self) -> int:
        """Store what changed in the directory since the last look.

        Returns how many files were written or removed. Best-effort: a
        failed write is logged and retried at the next sync, since the
        file is still there and still differs.
        """
        changed = 0
        try:
            present: dict[str, str] = {}
            if self._directory.is_dir():
                for path in self._directory.iterdir():
                    if path.is_file():
                        try:
                            present[path.name] = path.read_text()
                        except (OSError, UnicodeDecodeError):
                            continue
            for name, content in present.items():
                digest = self._digest(content)
                if self._known.get(name) != digest:
                    self._files.write(name, content)
                    self._known[name] = digest
                    changed += 1
            for name in [n for n in self._known if n not in present]:
                self._files.delete(name)
                del self._known[name]
                changed += 1
        except Exception as exc:
            logger.warning("memory files: sync failed: %s", exc)
        return changed


__all__ = [
    "DatabaseMemoryFiles",
    "DirectoryMemoryFiles",
    "MemoryFileModel",
    "MemoryFiles",
    "MemoryMirror",
]
