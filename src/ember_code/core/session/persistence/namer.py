"""Session-naming coordinator.

Owns the ``auto_name`` / ``rename`` / ``get_name`` flow that
used to sit inline in the pre-refactor
:class:`SessionPersistence`. Composes:

* :class:`SessionDataService` for reads (so a corrupt row on
  disk drops silently rather than sinking the whole read),
* Agno's ``db.rename_session`` directly for the write path (it's
  a dedicated column-level update, not a ``session_data`` merge),
* :class:`TitleAsker` for the model call that makes a title — not
  Agno's ``aset_session_name``, which blocks the event loop (see
  that module).

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
from ember_code.core.session.persistence.title_asker import TitleAsker
from ember_code.core.session.schemas import PersistResult, SessionTitle

logger = logging.getLogger(__name__)


class SessionNamer:
    """Coordinator for the session name orbit."""

    # A title is made from the first message, and the second when the
    # first is too short to say what the session is about.
    _OPENING_RUNS = 2

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

    async def auto_name(self, model: Any, *, model_busy: bool = False) -> str:
        """Title a session that has none, from its first message(s).

        Returns the title, or ``""`` when the session has nothing to
        title yet. The model is asked through :class:`TitleAsker` —
        async, bounded, with the opening only — and when it is busy
        with the person's work (``model_busy``) it is not asked at
        all: the title is the opening's first words, so naming never
        queues behind a run on the only GPU slot.
        """
        # Fast path: if the session already has a name, return the
        # cleaned form without asking the model. Pre-cleanup DB
        # rows surface clean this way too — :meth:`get_name`
        # routes through :class:`SessionTitle` on every read.
        existing = await self.get_name()
        if existing:
            return existing
        opening = TitleAsker.opening(await self._opening_messages())
        if not opening:
            return ""
        if model_busy:
            title = TitleAsker.fallback(opening)
        else:
            title = await TitleAsker(model).ask(opening)
        if not title:
            return ""
        written = await self.title(title)
        return title if written.ok else ""

    async def _opening_messages(self) -> list[str]:
        """What the person sent in the session's first runs."""
        if self._db is None:
            return []
        try:
            session = await self._db.get_session(
                session_id=self._session_id,
                session_type=SessionType.AGENT,
                deserialize=True,
            )
        except Exception as exc:
            logger.debug("Failed to read the session to title it: %s", exc)
            return []
        texts: list[str] = []
        for run in (getattr(session, "runs", None) or [])[: self._OPENING_RUNS]:
            content = getattr(getattr(run, "input", None), "input_content", None)
            if isinstance(content, str) and content.strip():
                texts.append(content)
        return texts

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

    async def title(self, name: str) -> PersistResult:
        """Name the session, whether or not it has a row yet.

        :meth:`rename` updates a row, and a session has none until its
        first run completes. A chat opened for a service's thread
        (``docs/CLOUD.md`` §6.1) is titled before anything has run in it,
        so this writes the name into the row, creating it if it is not
        there — the run that comes fills the rest in.
        """
        renamed = await self.rename(name)
        if not renamed.ok or await self.get_name():
            return renamed
        return await self._data.write_key("session_name", name)

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
