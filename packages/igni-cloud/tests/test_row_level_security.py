"""Row-level security: a producer's connection sees its own rows only.

``docs/CLOUD.md`` section 9, ``core/db/rls.py``. PostgreSQL only —
skipped, saying why, without ``IGNI_TEST_DATABASE_URL``. The isolation is
tested as a compromised igni inside a VM would probe it: with queries
that carry no ``WHERE``, by trying to change its own mapping, to become
another role, to turn row-level security off, and by reading another
producer's large object by its oid.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from agno.db.base import SessionType
from agno.session import AgentSession
from igni_cloud.db.rls import AGNO_SCHEMA, ProducerCredentials, ProducerRoles
from igni_cloud.producer import ProducerIncarnation, ProducerRef
from igni_cloud.storage.attachments import LargeObjectAttachments
from igni_cloud.storage.scoped_agno import ScopedAsyncPostgresDb
from sqlalchemy import BigInteger, Boolean, DateTime, Integer, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from ember_code.cli.migrate import SchemaMigration
from ember_code.core.approvals import journal as _journal  # noqa: F401 — its tables
from ember_code.core.db.base import Base
from ember_code.core.db.database import Database
from ember_code.core.db.engine import cache
from ember_code.core.db.locator import StorageLocator
from ember_code.core.db.scope import StorageScope, Tenant
from ember_code.core.db.urls import DatabaseUrl
from ember_code.core.memory.manager import StorageManager
from ember_code.core.scheduler.models import ScheduledTask
from ember_code.core.scheduler.store import TaskStore
from ember_code.core.session import queued_messages as _queued  # noqa: F401 — its table
from ember_code.core.session.client_state import ClientStateStore
from ember_code.core.session.pending_messages import PendingMessageStore
from ember_code.extensions import settings_values as _settings  # noqa: F401 — its table
from tests.storage_backends import ThrowawayPostgres


class Producer:
    """A producer's incarnation as the platform sets one up: a role, and a scope."""

    def __init__(
        self, admin: DatabaseUrl, run: str, name: str, *, incarnation: str = "m-1"
    ) -> None:
        self.incarnation = ProducerIncarnation.of(
            ProducerRef.parse(f"person:{name}-{run}"), incarnation
        )
        self.tenant = Tenant(org=f"org-{run}", producer=self.incarnation.stored)
        self.credentials: ProducerCredentials = ProducerRoles(admin).grant(
            self.tenant.org, self.incarnation
        )
        self.url = DatabaseUrl.parse(self.credentials.database_url)
        self.scope = StorageScope.for_tenant(self.tenant, project="p1")

    def engine(self) -> Engine:
        return create_engine(self.url.sync, isolation_level="AUTOCOMMIT")

    def db(self) -> Database:
        return Database(self.url, scope=self.scope)


@pytest.fixture(scope="module")
def admin(postgres_url: str) -> Iterator[DatabaseUrl]:
    """A database of its own, migrated by the admin role: tables, Agno's
    tables and row-level security."""
    database = ThrowawayPostgres(postgres_url)
    url = DatabaseUrl.parse(database.create())
    locator = StorageLocator(database_url=url.url, data_dir="/nonexistent")
    for target in SchemaMigration(locator, project=Path("/")).targets():
        SchemaMigration.migrate(target)
    try:
        yield url
    finally:
        database.drop()


@pytest.fixture(scope="module")
def producers(admin: DatabaseUrl) -> Iterator[tuple[Producer, Producer]]:
    run = uuid.uuid4().hex[:8]
    a, b = Producer(admin, run, "ada"), Producer(admin, run, "bob")
    try:
        yield a, b
    finally:
        for p in (a, b):
            ProducerRoles(admin).revoke(p.tenant.org, p.incarnation)


def _tenant_tables() -> list[Any]:
    return [
        t for t in Base.metadata.sorted_tables if "org" in t.columns and "producer" in t.columns
    ]


def _row(table: Any, scope: StorageScope) -> dict[str, Any]:
    """A row of ``table`` in ``scope``, every required column filled."""
    values: dict[str, Any] = scope.stamp()
    for column in table.columns:
        if column.name in values or column.nullable or column.server_default is not None:
            continue
        if column.autoincrement is True and column.primary_key:
            continue
        kind = column.type
        if isinstance(kind, Boolean):
            values[column.name] = False
        elif isinstance(kind, (Integer, BigInteger)):
            values[column.name] = uuid.uuid4().int % 2_000_000_000
        elif isinstance(kind, DateTime):
            values[column.name] = datetime.now()
        else:
            values[column.name] = uuid.uuid4().hex
    if table.name == "loop_state":
        values["id"] = 1
    return values


def test_each_producer_sees_only_its_rows_in_every_table(
    admin: DatabaseUrl, producers: tuple[Producer, Producer]
):
    a, b = producers
    owner = create_engine(admin.sync, isolation_level="AUTOCOMMIT")
    tables = _tenant_tables()
    assert len(tables) >= 15, [t.name for t in tables]
    with owner.connect() as c:
        for table in tables:
            for producer in (a, b):
                c.execute(table.insert().values(**_row(table, producer.scope)))
        for producer in (a, b):
            user = producer.scope.agno_user_id
            c.execute(
                text(
                    f"INSERT INTO {AGNO_SCHEMA}.ember_sessions (session_id, session_type, "
                    "user_id, created_at) VALUES (:s, 'agent', :u, 1)"
                ),
                {"s": f"{user}#s1", "u": user},
            )
    owner.dispose()

    for producer in (a, b):
        engine = producer.engine()
        with engine.connect() as c:
            for table in tables:
                rows = c.execute(text(f'SELECT org, producer FROM "{table.name}"')).all()
                assert rows == [(producer.tenant.org, producer.tenant.producer)], table.name
            agno = c.execute(text(f"SELECT user_id FROM {AGNO_SCHEMA}.ember_sessions")).all()
            assert agno == [(producer.scope.agno_user_id,)]
        engine.dispose()


def test_a_producer_cannot_write_as_another(producers: tuple[Producer, Producer]):
    a, b = producers
    engine = a.engine()
    with engine.connect() as c, pytest.raises(DBAPIError, match="row-level security"):
        c.execute(
            text(
                "INSERT INTO ember_client_state (org, producer, project, client_id, key, "
                "value, updated_at) VALUES (:o, :p, '', 'w', 'k', 'v', 1)"
            ),
            {"o": b.tenant.org, "p": b.tenant.producer},
        )
    engine.dispose()


@pytest.mark.parametrize(
    ("statement", "refused"),
    [
        ("SELECT * FROM producer_roles", "permission denied"),
        ("UPDATE producer_roles SET producer = 'x'", "permission denied"),
        ("SET ROLE igni_runtime", "permission denied"),
        ("ALTER TABLE ember_client_state DISABLE ROW LEVEL SECURITY", "must be owner"),
        ("ALTER TABLE ember_client_state NO FORCE ROW LEVEL SECURITY", "must be owner"),
        ("DROP POLICY igni_tenant ON ember_client_state", "must be owner"),
        ("CREATE TABLE sneaky (x int)", "permission denied"),
    ],
)
def test_a_producer_cannot_get_around_the_policy(
    producers: tuple[Producer, Producer], statement: str, refused: str
):
    a, _ = producers
    engine = a.engine()
    with engine.connect() as c, pytest.raises(DBAPIError, match=refused):
        c.execute(text(statement))
    engine.dispose()


def test_a_producer_cannot_become_another(producers: tuple[Producer, Producer]):
    a, b = producers
    engine = a.engine()
    with engine.connect() as c:
        with pytest.raises(DBAPIError, match="permission denied"):
            c.execute(text(f"SET ROLE {b.credentials.role}"))
        c.execute(text("SET row_security = off"))
        with pytest.raises(DBAPIError, match="row-level security"):
            c.execute(text("SELECT * FROM ember_client_state")).all()
    engine.dispose()


def test_igni_runs_as_a_producer_and_migrations_still_run(
    admin: DatabaseUrl, producers: tuple[Producer, Producer]
):
    a, b = producers
    store = TaskStore(db=a.db())

    async def work() -> None:
        before = len(await store.get_all(include_done=True))
        await store.add(
            ScheduledTask(id=uuid.uuid4().hex[:8], description="x", scheduled_at=datetime.now())
        )
        assert len(await store.get_all(include_done=True)) == before + 1

    asyncio.run(work())
    message_id = PendingMessageStore(a.db()).record_received("s1", "hello")
    assert [m.message_id for m in PendingMessageStore(a.db()).list_pending("s1")] == [message_id]
    assert PendingMessageStore(b.db()).list_pending("s1") == []
    ClientStateStore(a.db()).set_value("w", "k", "v")
    assert ClientStateStore(a.db()).get_for_client("w") == {"k": "v"}

    locator = StorageLocator(database_url=admin.url, data_dir="/nonexistent")
    assert "already at" in SchemaMigration.migrate(locator.global_url())
    assert ClientStateStore(a.db()).get_for_client("w") == {"k": "v"}


def test_agno_works_as_a_producer_and_keeps_to_its_rows(producers: tuple[Producer, Producer]):
    a, b = producers

    def agno(p: Producer) -> ScopedAsyncPostgresDb:
        return ScopedAsyncPostgresDb(
            scope=p.scope,
            db_engine=cache.get_async_engine(p.url),
            session_table=StorageManager.SESSION_TABLE,
            memory_table=StorageManager.MEMORY_TABLE,
            create_schema=False,
        )

    async def work() -> None:
        await agno(a).upsert_session(AgentSession(session_id="s9", agent_id="igni", created_at=1))
        assert await agno(a).get_session("s9", SessionType.AGENT) is not None
        assert await agno(b).get_session("s9", SessionType.AGENT) is None

    asyncio.run(work())


def test_another_producers_large_object_cannot_be_read_by_its_oid(
    producers: tuple[Producer, Producer],
):
    a, b = producers
    stored = LargeObjectAttachments(a.db()).save("s1", "secret.txt", [b"top secret"])
    assert b"".join(LargeObjectAttachments(a.db()).open(stored.ref)) == b"top secret"
    with pytest.raises(FileNotFoundError):
        list(LargeObjectAttachments(b.db()).open(stored.ref))

    reader = a.engine()
    with reader.connect() as c:
        oid = c.scalar(
            text("SELECT oid FROM attachments WHERE id = :id"),
            {"id": stored.ref.removeprefix(LargeObjectAttachments.REF_PREFIX)},
        )
        assert c.scalar(text("SHOW lo_compat_privileges")) == "off"
    reader.dispose()
    engine = b.engine()
    with engine.connect() as c, pytest.raises(DBAPIError, match="permission denied"):
        c.execute(text("SELECT lo_get(:oid)"), {"oid": oid})
    engine.dispose()
