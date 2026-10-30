"""A person removed from an org and added again is a new incarnation.

igni server's ``service-identity.md`` §11.3, ``docs/CLOUD.md`` §9. The old
incarnation's data is the org's: kept for its retention period, exported
for an org admin if asked, then erased. The new incarnation — a fresh VM,
a new role — never sees it, and writing the same keys does not collide
with it. PostgreSQL only, except the naming; skipped without
``IGNI_TEST_DATABASE_URL``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from agno.db.base import SessionType
from agno.session import AgentSession
from click.testing import CliRunner
from igni_cloud.db.admin_cli import db
from igni_cloud.db.producer_data import AdminCannotSeeRows, ProducerData
from igni_cloud.db.rls import AGNO_SCHEMA, ProducerRoles
from igni_cloud.guest.binding import ProducerEnv
from igni_cloud.producer import ProducerIncarnation, ProducerRef, UnknownProducer
from igni_cloud.storage.attachments import LargeObjectAttachments
from igni_cloud.storage.scoped_agno import ScopedAsyncPostgresDb
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from ember_code.cli.migrate import SchemaMigration
from ember_code.core.db.database import Database
from ember_code.core.db.engine import cache
from ember_code.core.db.locator import StorageLocator
from ember_code.core.db.scope import StorageScope, Tenant
from ember_code.core.db.urls import DatabaseUrl
from ember_code.core.memory.manager import StorageManager
from ember_code.core.session.client_state import ClientStateStore
from ember_code.core.session.pending_messages import PendingMessageStore
from tests.storage_backends import ThrowawayPostgres

# ── Naming one ─────────────────────────────────────────────────────


def test_an_incarnation_is_part_of_the_producer_its_rows_hold():
    ada = ProducerIncarnation.of(ProducerRef.parse("person:u-ada"), "m-7f3")
    assert ada.stored == "person:u-ada@m-7f3"
    for bad in ("", "m/1", "m@1", "m 1"):
        with pytest.raises(UnknownProducer):
            ProducerIncarnation.of(ProducerRef.parse("person:u-ada"), bad)


def test_another_incarnation_in_producer_env_is_another_vm():
    old = ProducerEnv.parse(
        "IGNI_DATABASE_URL=x\nIGNI_ORG=o\nIGNI_PRODUCER=person:u\nIGNI_PRODUCER_INCARNATION=m-1\n"
    )
    new = ProducerEnv.parse(
        "IGNI_DATABASE_URL=x\nIGNI_ORG=o\nIGNI_PRODUCER=person:u\nIGNI_PRODUCER_INCARNATION=m-2\n"
    )
    assert old.identity() != new.identity()


def test_every_command_wants_the_incarnation():
    for command in ("grant-producer", "revoke-producer", "erase-producer"):
        result = CliRunner().invoke(
            db,
            [
                command,
                "--org",
                "o",
                "--producer",
                "person:p",
                "--database-url",
                "postgresql://a@b/c",
            ],
        )
        assert result.exit_code != 0 and "--incarnation" in result.output, command


# ── In the database ───────────────────────────────────────────────


class Incarnation:
    """One life of Ada in the org: its role, and its scope."""

    def __init__(self, admin: DatabaseUrl, org: str, person: str, membership: str) -> None:
        self.org = org
        self.producer = ProducerIncarnation.of(ProducerRef.parse(person), membership)
        self.url = DatabaseUrl.parse(ProducerRoles(admin).grant(org, self.producer).database_url)
        self.scope = StorageScope.for_tenant(
            Tenant(org=org, producer=self.producer.stored), project="p1"
        )

    def db(self) -> Database:
        return Database(self.url, scope=self.scope)

    def agno(self) -> ScopedAsyncPostgresDb:
        return ScopedAsyncPostgresDb(
            scope=self.scope,
            db_engine=cache.get_async_engine(self.url),
            session_table=StorageManager.SESSION_TABLE,
            memory_table=StorageManager.MEMORY_TABLE,
            create_schema=False,
        )

    def write(self, value: str) -> str:
        """A row in a few of igni's tables, an Agno session and an upload."""
        ClientStateStore(self.db()).set_value("window", "theme", value)
        PendingMessageStore(self.db()).record_received("s1", f"hello from {value}")

        async def session() -> None:
            await self.agno().upsert_session(
                AgentSession(session_id="s1", agent_id="igni", created_at=1)
            )

        asyncio.run(session())
        return LargeObjectAttachments(self.db()).save("s1", "notes.txt", [value.encode()]).ref

    def reads(self) -> dict[str, object]:
        async def session() -> object:
            return await self.agno().get_session("s1", SessionType.AGENT)

        return {
            "theme": ClientStateStore(self.db()).get_for_client("window").get("theme"),
            "pending": [m.text for m in PendingMessageStore(self.db()).list_pending("s1")],
            "session": asyncio.run(session()) is not None,
        }


@pytest.fixture(scope="module")
def admin(postgres_url: str) -> Iterator[DatabaseUrl]:
    database = ThrowawayPostgres(postgres_url)
    url = DatabaseUrl.parse(database.create())
    locator = StorageLocator(database_url=url.url, data_dir="/nonexistent")
    for target in SchemaMigration(locator, project=Path("/")).targets():
        SchemaMigration.migrate(target)
    try:
        yield url
    finally:
        database.drop()


def _count(admin: DatabaseUrl, stored: str) -> int:
    engine = create_engine(admin.sync)
    try:
        with engine.connect() as c:
            return int(
                c.scalar(
                    text("SELECT count(*) FROM ember_client_state WHERE producer = :p"),
                    {"p": stored},
                )
            )
    finally:
        engine.dispose()


def test_the_old_incarnation_is_kept_apart_exported_and_erased(admin: DatabaseUrl, tmp_path: Path):
    org = f"org-{uuid.uuid4().hex[:8]}"
    old = Incarnation(admin, org, "person:u-ada", "m-1")
    upload = old.write("old")
    assert old.reads() == {"theme": "old", "pending": ["hello from old"], "session": True}

    # Removed: the node agent drops the old role; the rows stay for retention.
    assert ProducerRoles(admin).revoke(org, old.producer)
    with pytest.raises(OperationalError):
        create_engine(old.url.sync).connect()
    assert _count(admin, old.producer.stored) == 1

    # Added again: a new incarnation, which sees nothing of the old and
    # writes the same keys without meeting it.
    new = Incarnation(admin, org, "person:u-ada", "m-2")
    assert new.reads() == {"theme": None, "pending": [], "session": False}
    with pytest.raises(FileNotFoundError):
        list(LargeObjectAttachments(new.db()).open(upload))
    new.write("new")
    assert new.reads() == {"theme": "new", "pending": ["hello from new"], "session": True}

    # During retention, an org admin's copy of the old one.
    out = tmp_path / "ada-m-1.zip"
    result = CliRunner().invoke(
        db,
        [
            "export-producer",
            *("--org", org, "--producer", "person:u-ada", "--incarnation", "m-1"),
            *("--out", str(out), "--database-url", admin.url),
        ],
    )
    assert result.exit_code == 0, result.output
    manifest = json.loads(result.output)
    assert (manifest["producer"], manifest["incarnation"], manifest["attachments"]) == (
        "person:u-ada",
        "m-1",
        1,
    )
    with zipfile.ZipFile(out) as archive:
        assert json.loads(archive.read("manifest.json")) == manifest
        rows = [json.loads(line) for line in archive.read("rows.jsonl").splitlines()]
        files = [n for n in archive.namelist() if n.startswith("attachments/")]
        assert [archive.read(n) for n in files] == [b"old"]
    tables = {r["table"] for r in rows}
    assert {'public."ember_client_state"', 'public."attachments"'} <= tables
    assert f'{AGNO_SCHEMA}."{StorageManager.SESSION_TABLE}"' in tables
    assert all(
        r["row"].get("producer", old.producer.stored) == old.producer.stored for r in rows
    ), "nothing of the new incarnation"
    assert sum(manifest["rows"].values()) == len(rows)

    # Retention over: erased, with what went printed for the audit.
    erased = CliRunner().invoke(
        db,
        [
            "erase-producer",
            *("--org", org, "--producer", "person:u-ada", "--incarnation", "m-1"),
            *("--database-url", admin.url),
        ],
    )
    assert erased.exit_code == 0, erased.output
    summary = json.loads(erased.output)
    assert summary["large_objects"] == 1 and summary["large_object_bytes"] == 3
    assert summary["rows"]['public."ember_client_state"'] == 1
    assert summary["rows"][f'{AGNO_SCHEMA}."{StorageManager.SESSION_TABLE}"'] == 1
    assert summary["rows"] == manifest["rows"], "what was exported is what was erased"
    assert _count(admin, old.producer.stored) == 0
    engine = create_engine(admin.sync)
    with engine.connect() as c:
        assert not c.scalar(
            text("SELECT count(*) FROM attachments WHERE producer = :p"),
            {"p": old.producer.stored},
        )
        orphans = c.scalar(
            text(
                "SELECT count(*) FROM pg_largeobject_metadata m "
                "WHERE NOT EXISTS (SELECT 1 FROM attachments a WHERE a.oid::oid = m.oid)"
            )
        )
        assert orphans == 0, "the old upload's large object is gone too"
    engine.dispose()

    # The new incarnation is untouched; erasing again finds nothing.
    assert new.reads() == {"theme": "new", "pending": ["hello from new"], "session": True}
    again = ProducerData(admin).erase(org, old.producer)
    assert again.nothing_left, again


def test_erasing_drops_the_role_of_an_incarnation_not_yet_revoked(admin: DatabaseUrl):
    org = f"org-{uuid.uuid4().hex[:8]}"
    app = Incarnation(admin, org, "app:reporter", "reporter")
    app.write("app")
    summary = ProducerData(admin).erase(org, app.producer)
    assert summary.role_dropped and summary.large_objects == 1
    with pytest.raises(OperationalError):
        create_engine(app.url.sync).connect()


def test_an_admin_held_to_the_policies_is_told_so(admin: DatabaseUrl):
    role = f"igni_plain_{uuid.uuid4().hex[:8]}"
    engine = create_engine(admin.sync, isolation_level="AUTOCOMMIT")
    with engine.connect() as c:
        c.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD 'pw' NOBYPASSRLS"))
    try:
        plain = make_url(admin.sync).set(username=role, password="pw")
        data = ProducerData(DatabaseUrl.parse(plain.render_as_string(hide_password=False)))
        producer = ProducerIncarnation.of(ProducerRef.parse("person:x"), "m-1")
        with pytest.raises(AdminCannotSeeRows):
            data.erase("o", producer)
    finally:
        with engine.connect() as c:
            c.execute(text(f"DROP ROLE {role}"))
        engine.dispose()
