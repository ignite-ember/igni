"""Where a service keeps people signed in to its own pages.

Server-side: the browser holds an opaque, random session id in an
HttpOnly cookie and nothing else. The person's access and refresh tokens
stay here and are never sent to the browser.

`WebSessionStore` is the protocol; `MemoryWebSessionStore` is for tests
and a single process; `SqlWebSessionStore` is for a deployment — every
replica sees every session, because igni's callback may land on any of
them. It seals each row with the service's key encryption key and keys it
by a SHA-256 of the cookie, so a copy of the table signs nobody in.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from igni_service.clock import Clock
from igni_service.keystore import KeyCipher
from igni_service.sql_tables import TableMaker
from igni_service.tables import IgniTables
from igni_service.tokens import Person


class WebSession(BaseModel):
    """One person signed in to the service's pages."""

    model_config = ConfigDict(frozen=True)

    person: Person
    access_token: str
    refresh_token: str
    #: Wall-clock second the access token stops working.
    access_expires_at: float
    #: Wall-clock second the session is dead: the refresh token's life.
    expires_at: float


class LoginAttempt(BaseModel):
    """A sign-in in flight: sent to igni, not yet back."""

    model_config = ConfigDict(frozen=True)

    state: str
    verifier: str
    return_to: str
    expires_at: float


@runtime_checkable
class WebSessionStore(Protocol):
    async def get(self, session_id: str) -> WebSession | None: ...

    async def put(self, session_id: str, session: WebSession) -> None: ...

    async def delete(self, session_id: str) -> None: ...

    async def put_login(self, attempt: LoginAttempt) -> None: ...

    async def take_login(self, state: str) -> LoginAttempt | None:
        """The attempt for `state`, once: a second take finds nothing."""
        ...


class Digest:
    @staticmethod
    def of(secret: str) -> str:
        return hashlib.sha256(secret.encode()).hexdigest()


class MemoryWebSessionStore:
    """Sessions in this process only. For tests, and for one replica."""

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = clock or Clock()
        self._sessions: dict[str, WebSession] = {}
        self._logins: dict[str, LoginAttempt] = {}
        self._lock = asyncio.Lock()

    async def get(self, session_id: str) -> WebSession | None:
        session = self._sessions.get(Digest.of(session_id))
        if session is not None and self._clock.wall() >= session.expires_at:
            self._sessions.pop(Digest.of(session_id), None)
            return None
        return session

    async def put(self, session_id: str, session: WebSession) -> None:
        self._sessions[Digest.of(session_id)] = session

    async def delete(self, session_id: str) -> None:
        self._sessions.pop(Digest.of(session_id), None)

    async def put_login(self, attempt: LoginAttempt) -> None:
        self._logins[Digest.of(attempt.state)] = attempt

    async def take_login(self, state: str) -> LoginAttempt | None:
        async with self._lock:
            attempt = self._logins.pop(Digest.of(state), None)
        if attempt is None or self._clock.wall() >= attempt.expires_at:
            return None
        return attempt


class SqlWebSessionStore:
    """Sessions in two of the service's tables, sealed. See `igni_service.tables`."""

    def __init__(
        self,
        engine: AsyncEngine,
        cipher: KeyCipher,
        *,
        create_table: bool = True,
        clock: Clock | None = None,
    ) -> None:
        self._engine = engine
        self._cipher = cipher
        self._clock = clock or Clock()
        self._tables = TableMaker(
            engine, [IgniTables.web_session, IgniTables.web_login], create=create_table
        )

    async def get(self, session_id: str) -> WebSession | None:
        await self._tables.ensure()
        table = IgniTables.web_session
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(table.c.sealed, table.c.expires_at).where(
                        table.c.id_hash == Digest.of(session_id)
                    )
                )
            ).first()
        if row is None or self._clock.wall() >= float(row.expires_at):
            return None
        return WebSession.model_validate_json(self._cipher.open(str(row.sealed)))

    async def put(self, session_id: str, session: WebSession) -> None:
        await self._tables.ensure()
        table = IgniTables.web_session
        values = {
            "sealed": self._cipher.seal(session.model_dump_json()),
            "expires_at": session.expires_at,
        }
        async with self._engine.begin() as connection:
            changed = await connection.execute(
                update(table).where(table.c.id_hash == Digest.of(session_id)).values(**values)
            )
            if changed.rowcount == 0:
                await connection.execute(
                    insert(table).values(id_hash=Digest.of(session_id), **values)
                )

    async def delete(self, session_id: str) -> None:
        await self._tables.ensure()
        table = IgniTables.web_session
        async with self._engine.begin() as connection:
            await connection.execute(delete(table).where(table.c.id_hash == Digest.of(session_id)))

    async def put_login(self, attempt: LoginAttempt) -> None:
        await self._tables.ensure()
        table = IgniTables.web_login
        now = self._clock.wall()
        async with self._engine.begin() as connection:
            # Sign-ins nobody finished, and dead sessions, go as new ones arrive.
            await connection.execute(delete(table).where(table.c.expires_at <= now))
            await connection.execute(
                delete(IgniTables.web_session).where(IgniTables.web_session.c.expires_at <= now)
            )
            await connection.execute(
                insert(table).values(
                    state_hash=Digest.of(attempt.state),
                    sealed=self._cipher.seal(attempt.model_dump_json()),
                    expires_at=attempt.expires_at,
                )
            )

    async def take_login(self, state: str) -> LoginAttempt | None:
        await self._tables.ensure()
        table = IgniTables.web_login
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    select(table.c.sealed, table.c.expires_at).where(
                        table.c.state_hash == Digest.of(state)
                    )
                )
            ).first()
            if row is None:
                return None
            taken = await connection.execute(
                delete(table).where(table.c.state_hash == Digest.of(state))
            )
        # Whoever deleted the row took the attempt; a racing second take
        # deletes nothing and gets nothing.
        if taken.rowcount != 1 or self._clock.wall() >= float(row.expires_at):
            return None
        return LoginAttempt.model_validate_json(self._cipher.open(str(row.sealed)))
