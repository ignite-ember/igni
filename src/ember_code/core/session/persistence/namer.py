"""Session-naming coordinator.

Owns the ``auto_name`` / ``rename`` / ``get_name`` flow that
used to sit inline in the pre-refactor
:class:`SessionPersistence`. Composes:

* :class:`SessionDataService` for reads (so a corrupt row on
  disk drops silently rather than sinking the whole read),
* Agno's ``db.rename_session`` directly for the write path (it's
  a dedicated column-level update, not a ``session_data`` merge).

The wrapper-stripping rule (``**Title**`` / ``# Title`` /
``"Title"``) is expressed via :class:`SessionTitle` — the model
owns the ``@model_validator`` that populates ``cleaned`` at
construction, so this class stays a small orchestrator.
"""

from __future__ import annotations

import logging
from typing import Any

from agno.db.base import SessionType

from ember_code.core.session.persistence.data_service import SessionDataService
from ember_code.core.session.persistence.db_protocol import AgnoSessionDb
from ember_code.core.session.schemas import PersistResult, SessionTitle

logger = logging.getLogger(__name__)


class SessionNamer:
    """Coordinator for the session name orbit."""

    def __init__(
        self,
        db: AgnoSessionDb | None,
        session_id: str,
        data_service: SessionDataService,
    ) -> None:
        self._db = db
        self._session_id = session_id
        self._data = data_service

    def rebind(self, new_session_id: str) -> None:
        """Retarget the coordinator at a new session id after a
        ``rotate_id`` / ``fork`` — mirrors the identity swap that
        also updates :attr:`SessionDataService.session_id`.
        """
        self._session_id = new_session_id

    async def auto_name(self, executor: Any) -> str:
        """Ask Agno to auto-generate a session name from
        conversation.

        Returns the resulting (cleaned) name, or an empty string
        when no name could be produced. Wrapper decoration is
        stripped via :class:`SessionTitle` before the cleaned
        form is persisted back — callers get a display-ready
        string and no longer see markdown-wrapped names.
        """
        # Fast path: if the session already has a name, return the
        # cleaned form without asking the model. Pre-cleanup DB
        # rows surface clean this way too — :meth:`get_name`
        # routes through :class:`SessionTitle` on every read.
        existing = await self.get_name()
        if existing:
            return existing
        try:
            if hasattr(executor, "aset_session_name"):
                await executor.aset_session_name(
                    session_id=self._session_id,
                    autogenerate=True,
                )
        except Exception as exc:
            logger.debug("Failed to auto-name session: %s", exc)
            return ""
        # Re-read the raw name Agno just wrote and clean it. If the
        # cleaned form differs from what Agno persisted, replace
        # it so the session list never surfaces the wrapped variant.
        raw_after = await self._get_name_raw()
        title = SessionTitle(raw=raw_after)
        if title.cleaned and title.cleaned != raw_after:
            await self.rename(title.cleaned)
        return title.cleaned

    async def rename(self, new_name: str) -> PersistResult:
        """Manually rename the current session.

        Returns a :class:`PersistResult` envelope. Legacy callers
        that don't check the return value keep working — the
        historic "log-and-swallow" semantic is preserved (no
        exception escapes).
        """
        if self._db is None:
            return PersistResult(ok=True)
        try:
            await self._db.rename_session(
                session_id=self._session_id,
                session_type=SessionType.AGENT,
                session_name=new_name,
            )
        except Exception as exc:
            logger.debug("Failed to rename session: %s", exc)
            return PersistResult(ok=False, error=str(exc))
        return PersistResult(ok=True)

    async def get_name(self) -> str:
        """Get the current session's name from the database.

        Routes the persisted value through :class:`SessionTitle`
        so pre-cleanup DB rows (persisted before :meth:`auto_name`
        learned to sanitise) surface clean too — the belt-and-
        braces guarantee that used to live inline in the god
        class now lives on the model that defines the shape.
        """
        raw = await self._get_name_raw()
        return SessionTitle.clean(raw)

    async def _get_name_raw(self) -> str:
        """Read the persisted session name verbatim (no cleanup).

        Split out of :meth:`get_name` so :meth:`auto_name` can
        compare the freshly-written raw string against the cleaned
        form and re-persist only when the two differ.
        """
        if self._db is None:
            return ""
        try:
            session = await self._db.get_session(
                session_id=self._session_id,
                session_type=SessionType.AGENT,
                deserialize=True,
            )
        except Exception as exc:
            logger.debug("Failed to get session name: %s", exc)
            return ""
        if session and session.session_data:
            return session.session_data.get("session_name", "") or ""
        return ""
