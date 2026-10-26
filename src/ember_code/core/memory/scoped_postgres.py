"""Agno's PostgreSQL storage, kept to one scope's rows.

``docs/CLOUD.md`` section 9. In the cloud an org's people and apps share
one database, and Agno's tables — sessions, memories, learnings — have
no org, producer or project column to filter by. What they do have is
``user_id``, on every row and as a filter on every read and delete, so
:class:`ScopedAsyncPostgresDb` puts the scope there
(:attr:`StorageScope.agno_user_id`, ``<org>/<producer>/<project>``):
each write is stamped with it and each read and delete is filtered by
it, whatever the caller passed. Like :class:`StorageScope` for igni's own
tables, it is the one place the filter lives.

A session's primary key is its ``session_id`` alone, and Agno's upsert
conflicts on it. So the scope is also part of the key the database
holds: a session is stored as ``<org>/<producer>/<project>#<id>``
(:meth:`ScopedAsyncPostgresDb.stored_id`) and handed back with the
plain id. Two producers' sessions with the same id are two rows, kept
apart by the database's primary key rather than by the ids being random.
Agno's table and its columns are unchanged — a wrapper, not a fork of
its schema — so Agno's own migrations still apply.

Agno's other tables — metrics, evals, knowledge, traces, spans,
schedules, approvals — are not scoped. igni does not write them.
"""

from __future__ import annotations

import copy
import threading
from typing import Any, ClassVar

from agno.db.base import SessionType
from agno.db.postgres import AsyncPostgresDb
from agno.db.schemas.memory import UserMemory
from agno.session import Session
from sqlalchemy import Table

from ember_code.core.db.scope import StorageScope


class ScopedAsyncPostgresDb(AsyncPostgresDb):
    """:class:`AsyncPostgresDb` that reads and writes one scope's rows."""

    def __init__(self, *, scope: StorageScope, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._scoped_user_id = scope.agno_user_id

    # ── Tables ────────────────────────────────────────────────────

    #: Tables found, per database, schema, kind and name — for the process.
    _tables: ClassVar[dict[tuple[str, str, str, str], Table]] = {}
    _tables_lock: ClassVar[threading.Lock] = threading.Lock()
    #: Agno's name attribute for each kind of table igni uses.
    _NAME_ATTRIBUTES: ClassVar[dict[str, str]] = {
        "sessions": "session_table_name",
        "memories": "memory_table_name",
        "versions": "versions_table_name",
        "learnings": "learnings_table_name",
    }

    async def _get_table(
        self, table_type: str, create_table_if_not_found: bool | None = False
    ) -> Any:
        """Agno's table, looked up once per process instead of on every query.

        Agno checks that a table exists, that its columns are right and
        reflects it on *every* call — three round trips before each
        query, which after a restore are three on a fresh connection.
        The schema is fixed while igni runs (``igni migrate`` made it,
        as the admin role), so a table once found is kept. One not found
        is asked again: it may be made later.
        """
        attribute = self._NAME_ATTRIBUTES.get(table_type)
        if attribute is None:
            return await super()._get_table(table_type, create_table_if_not_found)
        key = (
            self.db_engine.url.render_as_string(hide_password=True),
            self.db_schema or "",
            table_type,
            str(getattr(self, attribute, "")),
        )
        with self._tables_lock:
            known = self._tables.get(key)
        if known is not None:
            return known
        table = await super()._get_table(table_type, create_table_if_not_found)
        if table is not None:
            with self._tables_lock:
                self._tables[key] = table
        return table

    @property
    def scoped_user_id(self) -> str:
        return self._scoped_user_id

    # ── Sessions ──────────────────────────────────────────────────

    @property
    def key_prefix(self) -> str:
        return f"{self._scoped_user_id}#"

    def stored_id(self, session_id: str) -> str:
        """The key a session is stored under: the scope, then the id."""
        return f"{self.key_prefix}{session_id}"

    def _plain(self, found: Any) -> Any:
        """What Agno read back, with the scope taken off every session id."""
        if found is None:
            return None
        if isinstance(found, tuple):
            return tuple(self._plain(item) for item in found)
        if isinstance(found, list):
            return [self._plain(item) for item in found]
        if isinstance(found, dict):
            session_id = found.get("session_id")
            if isinstance(session_id, str):
                return {**found, "session_id": session_id.removeprefix(self.key_prefix)}
            return found
        session_id = getattr(found, "session_id", None)
        if isinstance(session_id, str):
            found.session_id = session_id.removeprefix(self.key_prefix)
        return found

    async def delete_session(self, session_id: str, user_id: str | None = None) -> bool:
        return await super().delete_session(
            self.stored_id(session_id), user_id=self._scoped_user_id
        )

    async def delete_sessions(self, session_ids: list[str], user_id: str | None = None) -> None:
        await super().delete_sessions(
            [self.stored_id(s) for s in session_ids], user_id=self._scoped_user_id
        )

    async def get_session(
        self,
        session_id: str,
        session_type: SessionType | None = None,
        user_id: str | None = None,
        deserialize: bool | None = True,
    ) -> Any:
        return self._plain(
            await super().get_session(
                self.stored_id(session_id),
                session_type=session_type,
                user_id=self._scoped_user_id,
                deserialize=deserialize,
            )
        )

    async def get_sessions(
        self,
        session_type: SessionType | None = None,
        user_id: str | None = None,
        component_id: str | None = None,
        session_name: str | None = None,
        start_timestamp: int | None = None,
        end_timestamp: int | None = None,
        limit: int | None = None,
        page: int | None = None,
        sort_by: str | None = None,
        sort_order: str | None = None,
        deserialize: bool | None = True,
    ) -> Any:
        return self._plain(
            await super().get_sessions(
                session_type=session_type,
                user_id=self._scoped_user_id,
                component_id=component_id,
                session_name=session_name,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                limit=limit,
                page=page,
                sort_by=sort_by,
                sort_order=sort_order,
                deserialize=deserialize,
            )
        )

    async def rename_session(
        self,
        session_id: str,
        session_type: SessionType | None,
        session_name: str,
        user_id: str | None = None,
        deserialize: bool | None = True,
    ) -> Any:
        return self._plain(
            await super().rename_session(
                self.stored_id(session_id),
                session_type,
                session_name,
                user_id=self._scoped_user_id,
                deserialize=deserialize,
            )
        )

    async def upsert_session(self, session: Session, deserialize: bool | None = True) -> Any:
        # A copy: the caller's session keeps its plain id.
        stored = copy.copy(session)
        stored.session_id = self.stored_id(session.session_id)
        stored.user_id = self._scoped_user_id
        return self._plain(await super().upsert_session(stored, deserialize=deserialize))

    # ── Memories ──────────────────────────────────────────────────

    async def delete_user_memory(self, memory_id: str, user_id: str | None = None) -> Any:
        return await super().delete_user_memory(memory_id, user_id=self._scoped_user_id)

    async def delete_user_memories(self, memory_ids: list[str], user_id: str | None = None) -> None:
        await super().delete_user_memories(memory_ids, user_id=self._scoped_user_id)

    async def get_all_memory_topics(self, user_id: str | None = None) -> list[str]:
        return await super().get_all_memory_topics(user_id=self._scoped_user_id)

    async def get_user_memory(
        self,
        memory_id: str,
        deserialize: bool | None = True,
        user_id: str | None = None,
    ) -> Any:
        return await super().get_user_memory(
            memory_id, deserialize=deserialize, user_id=self._scoped_user_id
        )

    async def get_user_memories(
        self,
        user_id: str | None = None,
        agent_id: str | None = None,
        team_id: str | None = None,
        topics: list[str] | None = None,
        search_content: str | None = None,
        limit: int | None = None,
        page: int | None = None,
        sort_by: str | None = None,
        sort_order: str | None = None,
        deserialize: bool | None = True,
    ) -> Any:
        return await super().get_user_memories(
            user_id=self._scoped_user_id,
            agent_id=agent_id,
            team_id=team_id,
            topics=topics,
            search_content=search_content,
            limit=limit,
            page=page,
            sort_by=sort_by,
            sort_order=sort_order,
            deserialize=deserialize,
        )

    async def get_user_memory_stats(
        self, limit: int | None = None, page: int | None = None, user_id: str | None = None
    ) -> Any:
        return await super().get_user_memory_stats(
            limit=limit, page=page, user_id=self._scoped_user_id
        )

    async def upsert_user_memory(self, memory: UserMemory, deserialize: bool | None = True) -> Any:
        memory.user_id = self._scoped_user_id
        return await super().upsert_user_memory(memory, deserialize=deserialize)

    async def clear_memories(self) -> None:
        """Only this scope's: Agno's clears the whole table."""
        memories = await super().get_user_memories(user_id=self._scoped_user_id)
        ids = [m.memory_id for m in memories if getattr(m, "memory_id", None)]  # type: ignore[union-attr]
        if ids:
            await super().delete_user_memories(ids, user_id=self._scoped_user_id)

    # ── Learnings ─────────────────────────────────────────────────

    async def get_learning(self, learning_type: str, **kwargs: Any) -> Any:
        kwargs["user_id"] = self._scoped_user_id
        return await super().get_learning(learning_type, **kwargs)

    async def upsert_learning(
        self, id: str, learning_type: str, content: dict[str, Any], **kwargs: Any
    ) -> None:
        kwargs["user_id"] = self._scoped_user_id
        await super().upsert_learning(id, learning_type, content, **kwargs)

    async def get_learnings(self, learning_type: str | None = None, **kwargs: Any) -> Any:
        kwargs["user_id"] = self._scoped_user_id
        return await super().get_learnings(learning_type, **kwargs)

    async def list_learnings(self, learning_type: str | None = None, **kwargs: Any) -> Any:
        kwargs["user_id"] = self._scoped_user_id
        kwargs["include_global"] = False
        return await super().list_learnings(learning_type, **kwargs)


__all__ = ["ScopedAsyncPostgresDb"]
