"""``INSERT … ON CONFLICT`` on either database igni runs on.

SQLite and PostgreSQL both have it, and SQLAlchemy spells it the same
for both — ``on_conflict_do_update(index_elements=…, set_=…)`` and
``on_conflict_do_nothing(…)`` — but each from its own dialect module.
:class:`ConflictInsert` picks the construct for the session's database,
so a store writes one statement for both and keeps the upsert atomic
instead of a read-then-write that two writers can interleave.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from ember_code.core.db.urls import Dialect


class ConflictInsert:
    """The dialect's ``insert`` for a session's database."""

    _BY_DIALECT: dict[str, Any] = {
        Dialect.SQLITE.value: sqlite.insert,
        Dialect.POSTGRESQL.value: postgresql.insert,
    }

    @classmethod
    def into(cls, session: Session | AsyncSession, model: Any) -> Any:
        """``insert(model)`` with ``on_conflict_do_*`` available."""
        bind = session.get_bind() if isinstance(session, Session) else session.bind
        name = bind.dialect.name
        try:
            return cls._BY_DIALECT[name](model)
        except KeyError:
            raise ValueError(f"no INSERT … ON CONFLICT for the {name!r} dialect") from None


__all__ = ["ConflictInsert"]
