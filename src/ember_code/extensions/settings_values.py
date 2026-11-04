"""Where an extension's settings values are kept.

An extension's ``contributes.settings`` are values an administrator or
the person typed, kept beside the bundle rather than inside it. On the
desktop that is ``<store>/<id>.settings.json``, as it always was
(:class:`FileSettingsValues`). In the cloud a producer's VM is a cache,
so they are a row per extension in the shared database, scoped to the
producer like every other row (:class:`DatabaseSettingsValues`,
``docs/CLOUD.md`` section 9).
"""

from __future__ import annotations

import json
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


class ExtensionSettingsModel(ScopedColumns, Base):
    __tablename__ = "extension_settings"
    __table_args__ = (
        PrimaryKeyConstraint(
            "org", "producer", "project", "extension_id", name="pk_extension_settings"
        ),
    )

    extension_id: Mapped[str] = mapped_column(String, nullable=False)
    #: The stored values, as a JSON object.
    values: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class SettingsValues(ABC):
    """An extension's stored settings values, by extension id."""

    @abstractmethod
    def read(self, extension_id: str) -> dict[str, object]: ...

    @abstractmethod
    def write(self, extension_id: str, values: dict[str, object]) -> None: ...

    @abstractmethod
    def delete(self, extension_id: str) -> None: ...

    @staticmethod
    def for_store(root: Path, locator: StorageLocator | None = None) -> SettingsValues:
        """Files beside the bundles on the desktop; the database in the cloud."""
        storage = locator if locator is not None else StorageLocator.current()
        if storage.shared:
            return DatabaseSettingsValues(storage.global_database())
        return FileSettingsValues(root)


class FileSettingsValues(SettingsValues):
    def __init__(self, root: Path) -> None:
        self._root = root

    def path(self, extension_id: str) -> Path:
        return self._root / f"{extension_id}.settings.json"

    def read(self, extension_id: str) -> dict[str, object]:
        path = self.path(extension_id)
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # A corrupt settings file must not stop the extension
            # starting; defaults are a working configuration.
            logger.warning("ignoring %s: %s", path, exc)
            return {}
        return raw if isinstance(raw, dict) else {}

    def write(self, extension_id: str, values: dict[str, object]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        self.path(extension_id).write_text(
            json.dumps(values, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def delete(self, extension_id: str) -> None:
        self.path(extension_id).unlink(missing_ok=True)


class DatabaseSettingsValues(SettingsValues):
    """One row per extension, in the producer's scope."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def read(self, extension_id: str) -> dict[str, object]:
        with self._db.sync_session() as session:
            raw = session.scalar(
                select(ExtensionSettingsModel.values).where(
                    self._db.scope.where(ExtensionSettingsModel),
                    ExtensionSettingsModel.extension_id == extension_id,
                )
            )
        if raw is None:
            return {}
        try:
            values = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return values if isinstance(values, dict) else {}

    def write(self, extension_id: str, values: dict[str, object]) -> None:
        now = int(time.time())
        encoded = json.dumps(values, sort_keys=True)
        with self._db.sync_session() as session, session.begin():
            stmt = ConflictInsert.into(session, ExtensionSettingsModel).values(
                **self._db.scope.stamp(), extension_id=extension_id, values=encoded, updated_at=now
            )
            session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["org", "producer", "project", "extension_id"],
                    set_={"values": encoded, "updated_at": now},
                )
            )

    def delete(self, extension_id: str) -> None:
        with self._db.sync_session() as session, session.begin():
            session.execute(
                delete(ExtensionSettingsModel).where(
                    self._db.scope.where(ExtensionSettingsModel),
                    ExtensionSettingsModel.extension_id == extension_id,
                )
            )


__all__ = [
    "DatabaseSettingsValues",
    "ExtensionSettingsModel",
    "FileSettingsValues",
    "SettingsValues",
]
