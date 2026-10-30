"""Per-client UI state.

The web UI, the JetBrains plugin, and the VSCode extension all need a
small amount of state that survives across reloads: which session a
window is bound to, sidebar collapsed/open, composer drafts, future
toggles. Each client stores nothing locally except a stable
``client_id``; everything else lives in this table — the desktop's
global ``ember.db``, the shared database in the cloud — so the
behaviour is identical regardless of the host.

The store is keyed by ``(client_id, key)`` within its scope — clients
enumerate their own state with a single ``get_for_client(client_id)``
call and write individual entries with ``set_value``. A window belongs
to its producer, not to one project, so the project column is empty.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from sqlalchemy import Integer, PrimaryKeyConstraint, String, Text, delete, select
from sqlalchemy.orm import Mapped, mapped_column

from ember_code.core.db.base import Base
from ember_code.core.db.database import Database
from ember_code.core.db.locator import StorageLocator
from ember_code.core.db.scope import ScopedColumns
from ember_code.core.db.upsert import ConflictInsert

logger = logging.getLogger(__name__)


class ClientStateModel(ScopedColumns, Base):
    __tablename__ = "ember_client_state"
    __table_args__ = (
        PrimaryKeyConstraint(
            "org", "producer", "project", "client_id", "key", name="pk_ember_client_state"
        ),
    )

    client_id: Mapped[str] = mapped_column(String, nullable=False)
    key: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class ClientStateStore:
    """(client_id, key) → value KV store (global)."""

    def __init__(self, db: Database | str | Path) -> None:
        self._db = Database.of(db)

    @classmethod
    def from_locator(cls, locator: StorageLocator) -> ClientStateStore:
        return cls(locator.global_database())

    def get_for_client(self, client_id: str) -> dict[str, str]:
        if not client_id:
            return {}
        try:
            with self._db.sync_session() as session:
                rows = session.execute(
                    select(ClientStateModel.key, ClientStateModel.value).where(
                        self._db.scope.where(ClientStateModel),
                        ClientStateModel.client_id == client_id,
                    )
                ).all()
        except Exception as exc:
            logger.debug("client_state get_for_client failed: %s", exc)
            return {}
        return {key: value for key, value in rows}

    def set_value(self, client_id: str, key: str, value: str) -> None:
        if not client_id or not key:
            return
        now = int(time.time())
        try:
            with self._db.sync_session() as session, session.begin():
                stmt = ConflictInsert.into(session, ClientStateModel).values(
                    **self._db.scope.stamp(),
                    client_id=client_id,
                    key=key,
                    value=value,
                    updated_at=now,
                )
                session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["org", "producer", "project", "client_id", "key"],
                        set_={"value": value, "updated_at": now},
                    )
                )
        except Exception as exc:
            logger.debug("client_state set_value failed: %s", exc)

    def delete_value(self, client_id: str, key: str) -> None:
        if not client_id or not key:
            return
        try:
            with self._db.sync_session() as session, session.begin():
                session.execute(
                    delete(ClientStateModel).where(
                        self._db.scope.where(ClientStateModel),
                        ClientStateModel.client_id == client_id,
                        ClientStateModel.key == key,
                    )
                )
        except Exception as exc:
            logger.debug("client_state delete_value failed: %s", exc)
