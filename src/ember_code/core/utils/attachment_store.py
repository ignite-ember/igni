"""Where files a person uploads into a chat are kept.

On the desktop they are files under ``<project>/.igni/attachments/
<session_id>/`` and stay exactly that (:class:`DirectoryAttachments`).
In the cloud they are PostgreSQL large objects with a row each in the
shared database, scoped like every other row
(:class:`LargeObjectAttachments`, ``docs/CLOUD.md`` section 9).

Either way an upload arrives as chunks and is written as it arrives, and
a stored one is read back as chunks: an attachment is never held whole
in memory by the store.

**Reading one.** A message names an upload by the reference the upload
returned — a path on the desktop, ``attachment:<id>`` in the cloud —
and every reader goes through the store, which resolves either:
:meth:`Attachments.open` streams its bytes, and
:meth:`Attachments.local_path` gives a file the agent's tools and the
vision models' media can read, writing a large object out to it chunk by
chunk the first time (a cache under the session's attachments
directory; the database stays the record).
:class:`AttachmentReferences` puts those paths in place of the
references in a message before anything reads it. A large object is written and read through the
server-side ``lo_*`` functions, inside one transaction — so a failed
upload leaves neither a row nor an orphaned object behind.
"""

from __future__ import annotations

import base64
import re
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel
from sqlalchemy import BigInteger, Index, Integer, String, delete, select, text
from sqlalchemy.orm import Mapped, mapped_column

from ember_code.core.db.base import Base
from ember_code.core.db.database import Database
from ember_code.core.db.locator import StorageLocator
from ember_code.core.db.scope import ScopedColumns
from ember_code.core.db.urls import Dialect
from ember_code.core.paths import CONFIG_DIR


class AttachmentModel(ScopedColumns, Base):
    """One uploaded file, in the cloud: its large object and what it was called."""

    __tablename__ = "attachments"
    __table_args__ = (
        Index("ix_attachments_scope_session", "org", "producer", "project", "session_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    #: The large object's oid (PostgreSQL's ``oid`` is unsigned 32-bit).
    oid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


class StoredAttachment(BaseModel):
    """Where an upload went: a path on the desktop, ``attachment:<id>`` in the cloud."""

    ref: str
    size: int


class Base64Chunks:
    """A base64 upload, decoded a slice at a time.

    The slice is a multiple of four characters, so each decodes on its
    own and only one slice's bytes exist at once. Whitespace a client
    wrapped the text with is dropped first, as :func:`base64.b64decode`
    would have.
    """

    #: Characters per slice — 48 KiB of bytes.
    SLICE = 64 * 1024

    def __init__(self, encoded: str) -> None:
        self._encoded = "".join(encoded.split()) if re.search(r"\s", encoded) else encoded

    def __iter__(self) -> Iterator[bytes]:
        for start in range(0, len(self._encoded), self.SLICE):
            yield base64.b64decode(self._encoded[start : start + self.SLICE])


class Attachments(ABC):
    """Uploaded files of one project."""

    #: How a file name is made safe to keep.
    _SAFE_NAME_RE: ClassVar[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._-]+")

    @classmethod
    def safe_name(cls, filename: str) -> str:
        return cls._SAFE_NAME_RE.sub("_", filename) or "file"

    @abstractmethod
    def save(self, session_id: str, filename: str, chunks: Iterable[bytes]) -> StoredAttachment:
        """Keep the upload, writing each chunk as it comes."""

    @abstractmethod
    def open(self, ref: str) -> Iterator[bytes]:
        """The stored bytes, a chunk at a time — for a path or an
        ``attachment:<id>`` the store holds."""

    @abstractmethod
    def local_path(self, ref: str) -> Path | None:
        """A readable file with the attachment's bytes, or ``None`` when
        there is no such attachment."""

    @staticmethod
    def _file_chunks(path: Path) -> Iterator[bytes]:
        with path.open("rb") as source:
            while chunk := source.read(LargeObjectAttachments.CHUNK):
                yield chunk

    @staticmethod
    def for_project(project_dir: Path, locator: StorageLocator) -> Attachments:
        """Large objects where the shared database is PostgreSQL; files otherwise."""
        if locator.shared and locator.project_url(project_dir).dialect is Dialect.POSTGRESQL:
            return LargeObjectAttachments(locator.project_database(project_dir), project_dir)
        return DirectoryAttachments(project_dir)


class DirectoryAttachments(Attachments):
    """The desktop's: ``<project>/.igni/attachments/<session_id>/<name>``."""

    def __init__(self, project_dir: Path) -> None:
        self._project_dir = project_dir

    def _destination(self, session_id: str, filename: str) -> Path:
        safe = self.safe_name(filename)
        dest_dir = self._project_dir / CONFIG_DIR / "attachments" / session_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / safe
        if dest.exists():
            stem, dot, ext = safe.rpartition(".")
            base = stem if dot else safe
            suffix = ext if dot else ""
            n = 2
            while dest.exists():
                dest = dest_dir / (f"{base}-{n}{('.' + suffix) if suffix else ''}")
                n += 1
        return dest

    def save(self, session_id: str, filename: str, chunks: Iterable[bytes]) -> StoredAttachment:
        dest = self._destination(session_id, filename)
        size = 0
        try:
            with dest.open("wb") as out:
                for chunk in chunks:
                    out.write(chunk)
                    size += len(chunk)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
        return StoredAttachment(ref=str(dest), size=size)

    def open(self, ref: str) -> Iterator[bytes]:
        if ref.startswith(LargeObjectAttachments.REF_PREFIX):
            raise FileNotFoundError(f"{ref}: the desktop keeps attachments as files")
        return self._file_chunks(Path(ref))

    def local_path(self, ref: str) -> Path | None:
        if ref.startswith(LargeObjectAttachments.REF_PREFIX):
            return None
        path = Path(ref)
        return path if path.is_file() else None


class LargeObjectAttachments(Attachments):
    """The cloud's: a PostgreSQL large object per upload, a row per object."""

    REF_PREFIX = "attachment:"
    #: Bytes per ``lowrite`` / ``loread``.
    CHUNK = 64 * 1024
    #: ``INV_WRITE`` and ``INV_READ`` from ``libpq-fs.h``.
    _INV_WRITE = 0x20000
    _INV_READ = 0x40000

    def __init__(self, db: Database, project_dir: Path | None = None) -> None:
        if db.url.dialect is not Dialect.POSTGRESQL:
            raise ValueError("large objects are PostgreSQL's")
        self._db = db
        #: Where :meth:`local_path` writes readable copies.
        self._project_dir = project_dir if project_dir is not None else Path.cwd()

    def save(self, session_id: str, filename: str, chunks: Iterable[bytes]) -> StoredAttachment:
        attachment_id = uuid.uuid4().hex
        size = 0
        with self._db.sync_session() as session, session.begin():
            connection = session.connection()
            oid = int(connection.scalar(text("SELECT lo_create(0)")))
            fd = connection.scalar(
                text("SELECT lo_open(:oid, :mode)"), {"oid": oid, "mode": self._INV_WRITE}
            )
            for chunk in chunks:
                if chunk:
                    connection.execute(
                        text("SELECT lowrite(:fd, :data)"), {"fd": fd, "data": chunk}
                    )
                    size += len(chunk)
            connection.execute(text("SELECT lo_close(:fd)"), {"fd": fd})
            session.add(
                AttachmentModel(
                    **self._db.scope.stamp(),
                    id=attachment_id,
                    session_id=session_id,
                    filename=self.safe_name(filename),
                    oid=oid,
                    size=size,
                    created_at=int(time.time()),
                )
            )
        return StoredAttachment(ref=f"{self.REF_PREFIX}{attachment_id}", size=size)

    def _row(self, ref: str) -> AttachmentModel | None:
        attachment_id = ref.removeprefix(self.REF_PREFIX)
        with self._db.sync_session() as session:
            return session.scalar(
                select(AttachmentModel).where(
                    self._db.scope.where(AttachmentModel), AttachmentModel.id == attachment_id
                )
            )

    def local_path(self, ref: str) -> Path | None:
        if not ref.startswith(self.REF_PREFIX):
            path = Path(ref)
            return path if path.is_file() else None
        row = self._row(ref)
        if row is None:
            return None
        cache = (
            self._project_dir
            / CONFIG_DIR
            / "attachments"
            / row.session_id
            / f"{row.id}-{row.filename}"
        )
        if cache.is_file() and cache.stat().st_size == row.size:
            return cache
        cache.parent.mkdir(parents=True, exist_ok=True)
        partial = cache.with_name(cache.name + ".partial")
        with partial.open("wb") as out:
            for chunk in self.open(ref):
                out.write(chunk)
        partial.replace(cache)
        return cache

    def open(self, ref: str) -> Iterator[bytes]:
        if not ref.startswith(self.REF_PREFIX):
            return self._file_chunks(Path(ref))
        return self._read_object(ref)

    def _read_object(self, ref: str) -> Iterator[bytes]:
        row = self._row(ref)
        if row is None:
            raise FileNotFoundError(ref)
        with self._db.sync_session() as session, session.begin():
            connection = session.connection()
            fd = connection.scalar(
                text("SELECT lo_open(:oid, :mode)"), {"oid": row.oid, "mode": self._INV_READ}
            )
            while True:
                chunk = connection.scalar(
                    text("SELECT loread(:fd, :size)"), {"fd": fd, "size": self.CHUNK}
                )
                if not chunk:
                    break
                yield bytes(chunk)
            connection.execute(text("SELECT lo_close(:fd)"), {"fd": fd})

    def delete(self, ref: str) -> bool:
        row = self._row(ref)
        if row is None:
            return False
        with self._db.sync_session() as session, session.begin():
            session.execute(text("SELECT lo_unlink(:oid)"), {"oid": row.oid})
            session.execute(
                delete(AttachmentModel).where(
                    self._db.scope.where(AttachmentModel), AttachmentModel.id == row.id
                )
            )
        return True


class AttachmentReferences:
    """The uploads a message names, made readable before anything reads it."""

    #: An upload kept in the database (``LargeObjectAttachments.REF_PREFIX``).
    PATTERN: ClassVar[re.Pattern[str]] = re.compile(r"attachment:[0-9a-f]{32}")

    @classmethod
    def present(cls, text: str) -> bool:
        return cls.PATTERN.search(text) is not None

    @classmethod
    def localize(cls, text: str, store: Attachments) -> str:
        """``text`` with each ``attachment:<id>`` the store holds replaced
        by the readable file :meth:`Attachments.local_path` gives."""

        def readable(match: re.Match[str]) -> str:
            path = store.local_path(match.group(0))
            return str(path) if path is not None else match.group(0)

        return cls.PATTERN.sub(readable, text)


__all__ = [
    "AttachmentModel",
    "AttachmentReferences",
    "Attachments",
    "Base64Chunks",
    "DirectoryAttachments",
    "LargeObjectAttachments",
    "StoredAttachment",
]
