"""Typed protocol for Agno's session DB — closes the ``db: Any``
seam that used to live on :class:`SessionPersistence`.

Every persistence store composes an :class:`AgnoSessionDb`
instance instead of accepting an untyped ``Any``. The protocol
declares only the four methods the persistence layer actually
uses (``get_session`` / ``upsert_session`` / ``rename_session`` /
``get_sessions``) — the wider Agno DB surface is out of scope.

Marked ``@runtime_checkable`` so tests can smoke-check
``isinstance(real_agno_db, AgnoSessionDb)`` to catch drift
between this hand-written protocol and Agno's actual signatures.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AgnoSessionDb(Protocol):
    """Minimum surface the persistence stores require from the
    Agno session-DB layer.

    Kept intentionally narrow: any Agno DB implementation (the
    concrete SQLite / Postgres backends, plus test doubles) that
    exposes these four coroutines satisfies the contract. The
    stores never reach for other attributes on this object.
    """

    async def get_session(
        self,
        *,
        session_id: str,
        session_type: Any,
        deserialize: bool = True,
    ) -> Any:
        """Load one session row by id. Returns ``None`` on miss."""
        ...

    async def upsert_session(self, session: Any, *, deserialize: bool = True) -> Any:
        """Insert or update a session row (keyed by ``session_id``)."""
        ...

    async def rename_session(
        self,
        *,
        session_id: str,
        session_type: Any,
        session_name: str,
    ) -> Any:
        """Update the ``session_data["session_name"]`` field for one
        session row."""
        ...

    async def get_sessions(
        self,
        *,
        session_type: Any,
        limit: int | None = None,
        sort_by: str = "updated_at",
        sort_order: str = "desc",
        deserialize: bool = True,
    ) -> Any:
        """List session rows (returns a list or ``(list, count)``
        tuple depending on the concrete backend)."""
        ...
