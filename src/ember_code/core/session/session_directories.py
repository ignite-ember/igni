"""Global session → project-directory registry.

Sessions can live in different project directories (one BE, N
sessions, each potentially a different repo). A project's own state
can't answer "which directory does session X belong to?" — that's the
question you ask BEFORE you know the directory — so this registry is
GLOBAL: the desktop's ``<data_dir>/ember.db``, or the shared database
in the cloud, where the lookup filters by org and producer only (the
project is what it answers) and the row records the project it names.

Written whenever a runtime starts or renews its session id; read by
the session pool when lazily resuming a session a view asked for.
Unknown sessions fall back to the BE's boot project dir, which is
exactly the pre-pool behaviour.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from sqlalchemy import Integer, PrimaryKeyConstraint, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from ember_code.core.db.base import Base
from ember_code.core.db.database import Database
from ember_code.core.db.locator import StorageLocator
from ember_code.core.db.scope import ScopedColumns
from ember_code.core.db.upsert import ConflictInsert

logger = logging.getLogger(__name__)


class SessionDirectoryModel(ScopedColumns, Base):
    __tablename__ = "ember_session_directories"
    # A session is in one project, so the project is not part of the key.
    __table_args__ = (
        PrimaryKeyConstraint("org", "producer", "session_id", name="pk_ember_session_directories"),
    )

    session_id: Mapped[str] = mapped_column(String, nullable=False)
    project_dir: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class SessionDirectoryStore:
    """session_id → project_dir mapping (global)."""

    def __init__(self, db: Database | str | Path) -> None:
        self._db = Database.of(db)

    @classmethod
    def from_locator(cls, locator: StorageLocator) -> SessionDirectoryStore:
        return cls(locator.global_database())

    def set_dir(self, session_id: str, project_dir: str | Path) -> None:
        """Upsert. Best-effort — a failed write only means a later
        resume falls back to the BE's boot directory."""
        if not session_id:
            return
        scope = self._db.scope.for_project(project_dir)
        now = int(time.time())
        try:
            with self._db.sync_session() as session, session.begin():
                stmt = ConflictInsert.into(session, SessionDirectoryModel).values(
                    **scope.stamp(),
                    session_id=session_id,
                    project_dir=str(project_dir),
                    updated_at=now,
                )
                session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["org", "producer", "session_id"],
                        set_={
                            "project": scope.project,
                            "project_dir": str(project_dir),
                            "updated_at": now,
                        },
                    )
                )
        except Exception as exc:
            logger.debug("set_dir failed for %s: %s", session_id, exc)

    def get_dir(self, session_id: str) -> str | None:
        try:
            with self._db.sync_session() as session:
                return session.scalar(
                    select(SessionDirectoryModel.project_dir).where(
                        self._db.scope.tenant_where(SessionDirectoryModel),
                        SessionDirectoryModel.session_id == session_id,
                    )
                )
        except Exception as exc:
            logger.debug("get_dir failed for %s: %s", session_id, exc)
            return None
