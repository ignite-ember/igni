"""One incarnation's data, as the admin role: exported, then erased.

``docs/CLOUD.md`` §9. A person removed from an org is a removed
**incarnation** (igni server's ``service-identity.md`` §11.3): their data
is the org's, kept for the org's retention period (30 days unless it says
otherwise), then erased; a re-added person is a new incarnation and never
sees it. During retention an org admin may ask for a copy.

* :meth:`ProducerData.export` writes a portable archive — a zip with
  ``manifest.json``, ``rows.jsonl`` (one row of one table per line) and
  ``attachments/<id>/<filename>`` (each upload's bytes) — read in one
  repeatable-read snapshot, so the rows and files agree;
* :meth:`ProducerData.erase` deletes, in one transaction, every row of
  the incarnation in every igni table and in Agno's, unlinks its uploads'
  large objects and drops its role. Run again, it finds nothing and says
  so: it is idempotent. Its :class:`ErasureSummary` is the audit record.

Both find the rows the way the policies do: igni's tables by org and the
incarnation's producer value
(:attr:`~igni_cloud.producer.ProducerIncarnation.stored`), Agno's by the
``<org>/<producer>@<incarnation>/`` prefix of ``user_id``. Both need an
admin role that bypasses row-level security (a superuser, or
``BYPASSRLS``), as data migrations do: the policies admit producer roles
only. Like the rest of :mod:`igni_cloud.db.admin_cli`, nothing here
imports Agno or the rest of igni.
"""

from __future__ import annotations

import base64
import json
import os
import zipfile
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from ember_code.core.db.urls import DatabaseUrl
from igni_cloud.db.rls import AGNO_SCHEMA, ProducerRoles, RowLevelSecurity
from igni_cloud.producer import ProducerIncarnation


class AdminCannotSeeRows(PermissionError):
    """The admin role is held to row-level security: it would find nothing."""


class IncarnationScope(BaseModel):
    """Which rows are one incarnation's, as the policies see them."""

    #: The uploads' table (``AttachmentModel``): its rows name large objects.
    ATTACHMENTS: ClassVar[str] = "attachments"

    org: str
    producer: ProducerIncarnation

    @property
    def stored(self) -> str:
        return self.producer.stored

    @property
    def agno_prefix(self) -> str:
        """``<org>/<producer>@<incarnation>/``: the start of its Agno ``user_id``."""
        return f"{self.org}/{self.stored}/"

    def tables(self, c: Connection) -> Iterator[tuple[str, str, dict[str, str]]]:
        """Each table holding its rows: the qualified name, the condition, its parameters."""
        for table in RowLevelSecurity.tenant_tables(c):
            yield (
                f'public."{table}"',
                "org = :org AND producer = :producer",
                {"org": self.org, "producer": self.stored},
            )
        for table in RowLevelSecurity.agno_scoped_tables(c):
            yield (
                f'{AGNO_SCHEMA}."{table}"',
                "starts_with(user_id, :prefix)",
                {"prefix": self.agno_prefix},
            )

    def uploads(self, c: Connection) -> list[Any]:
        """Its uploads whose large object still exists: id, filename, oid, size."""
        return list(
            c.execute(
                text(
                    f'SELECT a.id, a.filename, a.oid, a.size FROM public."{self.ATTACHMENTS}" a '
                    "JOIN pg_largeobject_metadata m ON m.oid = a.oid::oid "
                    "WHERE a.org = :org AND a.producer = :producer ORDER BY a.id"
                ),
                {"org": self.org, "producer": self.stored},
            ).all()
        )

    @staticmethod
    def require_bypass(c: Connection) -> None:
        if not c.scalar(
            text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ):
            raise AdminCannotSeeRows(
                "the admin role must bypass row-level security (a superuser, or BYPASSRLS): "
                "the policies admit producer roles only"
            )


class ErasureSummary(BaseModel):
    """What one erasure removed: the audit record ``erase-producer`` prints."""

    org: str
    producer: str
    incarnation: str
    #: Rows deleted per table that had any (``public."t"`` / ``ai."t"``).
    rows: dict[str, int] = Field(default_factory=dict)
    #: Uploads' large objects unlinked, and their bytes.
    large_objects: int = 0
    large_object_bytes: int = 0
    #: Whether a role (or its mapping) was there to drop.
    role_dropped: bool = False
    erased_at: datetime

    @property
    def nothing_left(self) -> bool:
        return not self.rows and not self.large_objects and not self.role_dropped


class ExportManifest(BaseModel):
    """``manifest.json`` in an export: whose data, when, and what is in it."""

    FORMAT: ClassVar[str] = "igni-producer-export/1"

    format: str = FORMAT
    org: str
    producer: str
    incarnation: str
    exported_at: datetime
    #: Rows per table in ``rows.jsonl``.
    rows: dict[str, int] = Field(default_factory=dict)
    #: Files under ``attachments/``.
    attachments: int = 0
    attachment_bytes: int = 0


class PortableJson:
    """Row values as JSON: dates as ISO 8601, bytes as base64, the rest as text."""

    @staticmethod
    def default(value: Any) -> Any:
        if isinstance(value, datetime | date):
            return value.isoformat()
        if isinstance(value, bytes | memoryview):
            return base64.b64encode(bytes(value)).decode("ascii")
        if isinstance(value, Decimal | UUID):
            return str(value)
        raise TypeError(f"{type(value).__name__} is not exported")

    @classmethod
    def line(cls, table: str, row: dict[str, Any]) -> bytes:
        return (
            json.dumps({"table": table, "row": row}, default=cls.default, ensure_ascii=False) + "\n"
        ).encode()


class ProducerData:
    """Exports and erases one incarnation's data, as the admin role."""

    #: Read from a large object at a time.
    CHUNK = 1 << 20
    MANIFEST = "manifest.json"
    ROWS = "rows.jsonl"
    ATTACHMENTS_DIR = "attachments"

    def __init__(self, admin: DatabaseUrl) -> None:
        self._rls = RowLevelSecurity(admin)

    def erase(self, org: str, producer: ProducerIncarnation) -> ErasureSummary:
        scope = IncarnationScope(org=org, producer=producer)
        summary = ErasureSummary(
            org=org,
            producer=producer.producer.ref,
            incarnation=producer.incarnation,
            erased_at=datetime.now(UTC),
        )
        engine = self._rls.engine()
        try:
            # One transaction: the admin engine autocommits, this does not.
            with (
                engine.connect().execution_options(isolation_level="READ COMMITTED") as c,
                c.begin(),
            ):
                scope.require_bypass(c)
                for _id, _name, oid, size in scope.uploads(c):
                    c.execute(text("SELECT lo_unlink(:oid)"), {"oid": oid})
                    summary.large_objects += 1
                    summary.large_object_bytes += int(size)
                for table, condition, params in scope.tables(c):
                    deleted = c.execute(text(f"DELETE FROM {table} WHERE {condition}"), params)
                    if deleted.rowcount:
                        summary.rows[table] = deleted.rowcount
                # Whatever else it owned (an object no row names) goes with it.
                summary.role_dropped = ProducerRoles.drop(
                    c, ProducerRoles.role_name(org, producer), keep_owned=False
                )
        finally:
            engine.dispose()
        return summary

    def export(self, org: str, producer: ProducerIncarnation, out: Path) -> ExportManifest:
        scope = IncarnationScope(org=org, producer=producer)
        manifest = ExportManifest(
            org=org,
            producer=producer.producer.ref,
            incarnation=producer.incarnation,
            exported_at=datetime.now(UTC),
        )
        partial = out.with_name(out.name + ".partial")
        engine = self._rls.engine()
        try:
            with (
                # One snapshot: the rows and the files agree.
                engine.connect().execution_options(isolation_level="REPEATABLE READ") as c,
                c.begin(),
                zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive,
            ):
                scope.require_bypass(c)
                c.execute(text("SET TRANSACTION READ ONLY"))
                with archive.open(self.ROWS, "w", force_zip64=True) as rows:
                    for table, condition, params in scope.tables(c):
                        found = c.execution_options(stream_results=True).execute(
                            text(f"SELECT * FROM {table} WHERE {condition}"), params
                        )
                        for row in found.mappings():
                            rows.write(PortableJson.line(table, dict(row)))
                            manifest.rows[table] = manifest.rows.get(table, 0) + 1
                for attachment_id, filename, oid, _size in scope.uploads(c):
                    name = f"{self.ATTACHMENTS_DIR}/{attachment_id}/{Path(filename).name}"
                    with archive.open(name, "w", force_zip64=True) as target:
                        for chunk in self._object(c, oid):
                            target.write(chunk)
                            manifest.attachment_bytes += len(chunk)
                    manifest.attachments += 1
                archive.writestr(self.MANIFEST, manifest.model_dump_json(indent=2))
            os.replace(partial, out)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        finally:
            engine.dispose()
        return manifest

    def _object(self, c: Connection, oid: int) -> Iterator[bytes]:
        offset = 0
        while True:
            chunk = c.scalar(
                text("SELECT lo_get(:oid, :offset, :size)"),
                {"oid": oid, "offset": offset, "size": self.CHUNK},
            )
            if not chunk:
                return
            yield bytes(chunk)
            offset += len(chunk)


__all__ = [
    "AdminCannotSeeRows",
    "ErasureSummary",
    "ExportManifest",
    "IncarnationScope",
    "PortableJson",
    "ProducerData",
]
