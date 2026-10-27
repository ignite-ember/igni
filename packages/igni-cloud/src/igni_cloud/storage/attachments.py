"""Uploads in PostgreSQL: a large object per upload, a row per object.

``docs/CLOUD.md`` §9. Written and read through the server-side ``lo_*``
functions, inside one transaction — so a failed upload leaves neither a
row nor an orphaned object behind — and a chunk at a time, so an upload
is never held whole in memory. Named ``attachment:<id>``; a reader asks
:meth:`LargeObjectAttachments.local_path` for a readable copy.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable, Iterator
from pathlib import Path

from sqlalchemy import delete, select, text

from ember_code.core.db.database import Database
from ember_code.core.db.urls import Dialect
from ember_code.core.paths import CONFIG_DIR
from ember_code.core.utils.attachment_store import AttachmentModel, Attachments, StoredAttachment


class LargeObjectAttachments(Attachments):
    """The cloud's: a PostgreSQL large object per upload, a row per object."""

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


__all__ = ["LargeObjectAttachments"]
