"""Shared read/write chokepoint over Agno's ``session_data`` blob.

Every persistence store (plan-decisions, todos, event-log)
composes one :class:`SessionDataService` instance. The service
owns the three properties that were duplicated across four
methods in the pre-refactor god class:

* the ``asyncio.Lock`` that serialises load-modify-write against
  the session row (without which two writes racing on the merge
  step both start from the same pre-image and the second to
  ``upsert_session`` silently drops the first write's key),
* the "create if missing" branch that mints a minimal
  :class:`AgentSession` when the session has never been through
  Agno's run path yet (fresh boot, zero turns),
* the "merge with existing" logic that preserves the sibling
  ``session_data`` keys so writing todos doesn't clobber a
  parallel plan_decisions write and vice versa.

Public API:

* :meth:`read_key` — dispatch ``db.get_session`` → hand
  ``session_data[key]`` to ``parser`` → return a
  :class:`LoadResult` envelope.
* :meth:`write_key` — hold the lock → create-or-merge → upsert →
  return a :class:`PersistResult` envelope.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TypeVar

from agno.db.base import SessionType
from agno.session.agent import AgentSession

from ember_code.core.session.persistence.db_protocol import AgnoSessionDb
from ember_code.core.session.schemas import LoadResult, PersistResult

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class SessionDataService:
    """Read/write chokepoint over the Agno session row's
    ``session_data`` blob.

    Held (via composition) by every persistence store; the
    :class:`SessionPersistence` facade builds one instance and
    threads it into each store's constructor.
    """

    def __init__(self, db: AgnoSessionDb | None, session_id: str) -> None:
        self._db = db
        self._session_id = session_id
        # Serializes ``session_data`` writes so concurrent callers
        # (event-log append, todo save, plan-decisions save) can't
        # clobber each other. Every write is a load-modify-write
        # against Agno's session row; without the lock, two writes
        # racing on the merge step would both start from the same
        # pre-image and the second to ``upsert_session`` would win —
        # silently dropping the first write's key.
        self._lock = asyncio.Lock()

    @property
    def db(self) -> AgnoSessionDb | None:
        """The underlying Agno DB (``None`` when persistence is
        disabled — headless CLI, tests without a DB)."""
        return self._db

    @property
    def session_id(self) -> str:
        """The active session id. Mutable via the setter so
        :meth:`Session.rotate_id` can retarget every store at
        once."""
        return self._session_id

    @session_id.setter
    def session_id(self, new_id: str) -> None:
        self._session_id = new_id

    async def read_key(
        self,
        key: str,
        parser: Callable[[object], _T | None],
    ) -> LoadResult[_T]:
        """Load ``session_data[key]``, hand the raw value to
        ``parser``, and wrap the outcome in a :class:`LoadResult`.

        ``parser`` returns ``None`` on miss / shape mismatch;
        the service maps that to ``LoadResult(ok=True, value=None)``.
        A DB-layer exception maps to ``LoadResult(ok=False, error=...)``
        — callers that want the historic "swallow-and-default"
        behaviour can check ``result.value is None`` (which is
        true on both miss and error paths).
        """
        if self._db is None:
            return LoadResult(ok=True, value=None)
        try:
            session = await self._db.get_session(
                session_id=self._session_id,
                session_type=SessionType.AGENT,
                deserialize=True,
            )
        except Exception as exc:
            logger.debug("session_data read failed on key=%r: %s", key, exc)
            return LoadResult(ok=False, value=None, error=str(exc))
        if session is None or not session.session_data:
            return LoadResult(ok=True, value=None)
        raw = session.session_data.get(key)
        if raw is None:
            return LoadResult(ok=True, value=None)
        try:
            parsed = parser(raw)
        except Exception as exc:
            logger.debug("session_data parse failed on key=%r: %s", key, exc)
            return LoadResult(ok=False, value=None, error=str(exc))
        return LoadResult(ok=True, value=parsed)

    async def write_key(self, key: str, value: object) -> PersistResult:
        """Merge ``value`` into ``session_data[key]`` and upsert.

        Creates a minimal :class:`AgentSession` if the session row
        doesn't yet exist (fresh boot, zero turns) — without this
        fallback the write silently no-ops and the user loses
        state on the next launch.

        Serialised via :attr:`_lock` so two callers writing
        different keys concurrently can't clobber each other on
        the merge step.
        """
        if self._db is None:
            return PersistResult(ok=True)
        try:
            async with self._lock:
                session = await self._db.get_session(
                    session_id=self._session_id,
                    session_type=SessionType.AGENT,
                    deserialize=True,
                )
                if session is None:
                    # Fresh session that's never been through Agno's
                    # run path yet. Create a minimal AgentSession so
                    # the upsert lands as an INSERT — every other
                    # field is Optional in Agno's dataclass, and the
                    # run path will fill them in on the first
                    # ``run_message``.
                    now = int(time.time())
                    session = AgentSession(
                        session_id=self._session_id,
                        session_data={key: value},
                        created_at=now,
                        updated_at=now,
                    )
                else:
                    sd = dict(session.session_data or {})
                    sd[key] = value
                    session.session_data = sd
                await self._db.upsert_session(session, deserialize=True)
        except Exception as exc:
            logger.debug("session_data write failed on key=%r: %s", key, exc)
            return PersistResult(ok=False, error=str(exc))
        return PersistResult(ok=True)
