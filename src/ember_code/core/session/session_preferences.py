"""Per-session preferences — durable across restarts.

Holds preferences that should be remembered when a session is
resumed via ``--continue``. Today only the active model name; the
table is keyed by ``session_id`` so other per-session knobs can be
added as additional columns without a migration story.

User-level defaults still live in ``~/.igni/config.yaml`` (written
by ``UserConfigStore.set_default_model``); this store layers on top
so a resumed session keeps the model it was last using even if the
user has since picked a different default for new sessions.

Lives in the project's database (the desktop's ``state.db``, the
same file Agno uses), keyed by scope and ``session_id``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from sqlalchemy import Integer, PrimaryKeyConstraint, String, select
from sqlalchemy.orm import Mapped, mapped_column

from ember_code.core.db.base import Base
from ember_code.core.db.database import Database
from ember_code.core.db.scope import ScopedColumns
from ember_code.core.db.upsert import ConflictInsert

logger = logging.getLogger(__name__)


class SessionPreferenceModel(ScopedColumns, Base):
    __tablename__ = "ember_session_preferences"
    __table_args__ = (
        PrimaryKeyConstraint(
            "org", "producer", "project", "session_id", name="pk_ember_session_preferences"
        ),
    )

    session_id: Mapped[str] = mapped_column(String, nullable=False)
    model_name: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class SessionPreferencesStore:
    """Key/value store for per-session preferences.

    Writes are tiny (one row, single column update) so the sync API
    is exposed directly. Async wrappers exist for hot paths invoked
    from the event loop.
    """

    def __init__(self, db: Database | str | Path) -> None:
        self._db = Database.of(db)

    def set_model(self, session_id: str, model_name: str) -> None:
        """Upsert the model preference for ``session_id``.

        Best-effort: persistence failures are logged but do not
        propagate. The in-memory switch has already taken effect by
        the time we get here — the only consequence of a failed save
        is that ``--continue`` won't restore this specific choice.
        """
        now = int(time.time())
        try:
            with self._db.sync_session() as session, session.begin():
                stmt = ConflictInsert.into(session, SessionPreferenceModel).values(
                    **self._db.scope.stamp(),
                    session_id=session_id,
                    model_name=model_name,
                    updated_at=now,
                )
                session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["org", "producer", "project", "session_id"],
                        set_={"model_name": model_name, "updated_at": now},
                    )
                )
        except Exception as exc:
            logger.debug("set_model failed for %s: %s", session_id, exc)

    def get_model(self, session_id: str) -> str | None:
        """Return the persisted model for ``session_id`` or None."""
        try:
            with self._db.sync_session() as session:
                return session.scalar(
                    select(SessionPreferenceModel.model_name).where(
                        self._db.scope.where(SessionPreferenceModel),
                        SessionPreferenceModel.session_id == session_id,
                    )
                )
        except Exception as exc:
            logger.debug("get_model failed for %s: %s", session_id, exc)
            return None

    # ── Async wrappers ────────────────────────────────────────────

    async def aset_model(self, session_id: str, model_name: str) -> None:
        await asyncio.to_thread(self.set_model, session_id, model_name)

    async def aget_model(self, session_id: str) -> str | None:
        return await asyncio.to_thread(self.get_model, session_id)
