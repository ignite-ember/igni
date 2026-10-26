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

What ``user_id`` cannot do (the gaps, until row-level security):

* A session's primary key is its ``session_id`` alone. Agno's upsert
  only updates a row whose ``user_id`` is the writer's, so a session
  whose id collides with another scope's (igni's ids are eight hex
  characters) is not written over — but neither is it saved.
* Agno's other tables — metrics, evals, knowledge, traces, spans,
  schedules, approvals — are not scoped. igni does not write them.
"""

from __future__ import annotations

from typing import Any

from agno.db.base import SessionType
from agno.db.postgres import AsyncPostgresDb
from agno.db.schemas.memory import UserMemory
from agno.session import Session

from ember_code.core.db.scope import StorageScope


class ScopedAsyncPostgresDb(AsyncPostgresDb):
    """:class:`AsyncPostgresDb` that reads and writes one scope's rows."""

    def __init__(self, *, scope: StorageScope, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._scoped_user_id = scope.agno_user_id

    @property
    def scoped_user_id(self) -> str:
        return self._scoped_user_id

    # ── Sessions ──────────────────────────────────────────────────

    async def delete_session(self, session_id: str, user_id: str | None = None) -> bool:
        return await super().delete_session(session_id, user_id=self._scoped_user_id)

    async def delete_sessions(self, session_ids: list[str], user_id: str | None = None) -> None:
        await super().delete_sessions(session_ids, user_id=self._scoped_user_id)

    async def get_session(
        self,
        session_id: str,
        session_type: SessionType | None = None,
        user_id: str | None = None,
        deserialize: bool | None = True,
    ) -> Any:
        return await super().get_session(
            session_id,
            session_type=session_type,
            user_id=self._scoped_user_id,
            deserialize=deserialize,
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
        return await super().get_sessions(
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

    async def rename_session(
        self,
        session_id: str,
        session_type: SessionType | None,
        session_name: str,
        user_id: str | None = None,
        deserialize: bool | None = True,
    ) -> Any:
        return await super().rename_session(
            session_id,
            session_type,
            session_name,
            user_id=self._scoped_user_id,
            deserialize=deserialize,
        )

    async def upsert_session(self, session: Session, deserialize: bool | None = True) -> Any:
        session.user_id = self._scoped_user_id
        return await super().upsert_session(session, deserialize=deserialize)

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
