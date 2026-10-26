"""igni's stores on both databases: SQLite, and PostgreSQL when one is named.

``docs/CLOUD.md`` section 9. Every test here runs twice:

* ``sqlite`` — always: a shared SQLite file, the same code path the
  cloud takes (``IGNI_DATABASE_URL`` set, rows scoped by org, producer
  and project), on the dialect the desktop uses;
* ``postgresql`` — when ``IGNI_TEST_DATABASE_URL`` names a server the
  tests may create and drop databases on, e.g.::

      docker run --rm -d --name igni-pg-test -e POSTGRES_PASSWORD=pw \\
          -p 55432:5432 postgres:17
      IGNI_TEST_DATABASE_URL=postgresql://postgres:pw@localhost:55432/postgres \\
          pytest tests/test_storage_backends.py -rs

  Unset, those cases skip and say so (``-rs`` prints why). Each run
  creates a database of its own and drops it afterwards.

What they hold every store to: a row written in one scope is invisible
from another, and the schema alembic builds is the one the models say.
"""

from __future__ import annotations

import base64
import os
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from agno.db.base import SessionType
from agno.db.schemas.memory import UserMemory
from agno.session import AgentSession
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from click.testing import CliRunner
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from ember_code.cli.migrate import migrate
from ember_code.core.db.base import Base
from ember_code.core.db.database import Database
from ember_code.core.db.engine import cache
from ember_code.core.db.locator import StorageLocator
from ember_code.core.db.schema import HEAD_REVISION, SchemaRevision
from ember_code.core.db.scope import NoTenantError, StorageScope, Tenant, tenancy
from ember_code.core.db.urls import DatabaseUrl, Dialect
from ember_code.core.loop.models import LoopState
from ember_code.core.loop.store import LoopProgressStore, LoopStore
from ember_code.core.memory.scoped_postgres import ScopedAsyncPostgresDb
from ember_code.core.scheduler.models import ScheduledTask
from ember_code.core.scheduler.store import TaskStore
from ember_code.core.session.client_state import ClientStateStore
from ember_code.core.session.pending_messages import PendingMessageStore
from ember_code.core.session.session_directories import SessionDirectoryStore
from ember_code.core.session.session_preferences import SessionPreferencesStore
from ember_code.core.tools.process_store import BackgroundProcessStore
from ember_code.core.tools.process_store_schemas import BackgroundProcessRow
from ember_code.core.utils.attachment_store import (
    Attachments,
    Base64Chunks,
    DirectoryAttachments,
    LargeObjectAttachments,
)
from ember_code.core.utils.context_memory import ProjectMemoryBank
from ember_code.core.utils.memory_files import DatabaseMemoryFiles, MemoryFiles
from ember_code.notifications.local_store import LocalNotificationStore
from ember_code.notifications.schemas import ExtensionNotice, NotificationTarget

#: Names a PostgreSQL server the tests may create databases on.
POSTGRES_ENV = "IGNI_TEST_DATABASE_URL"

_POSTGRES_SKIP = (
    f"{POSTGRES_ENV} is unset: the PostgreSQL storage tests need a server they may "
    "create and drop databases on (see this module's docstring for a docker one-liner)"
)


class ThrowawayPostgres:
    """A database of the run's own on the server ``IGNI_TEST_DATABASE_URL`` names."""

    def __init__(self, server_url: str) -> None:
        self._server = make_url(server_url).set(drivername="postgresql+psycopg")
        self._name = f"igni_test_{uuid.uuid4().hex[:12]}"

    def _admin(self):  # noqa: ANN202 — a short-lived engine
        return create_engine(self._server, isolation_level="AUTOCOMMIT")

    def create(self) -> str:
        admin = self._admin()
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{self._name}"'))
        admin.dispose()
        return self._server.set(database=self._name).render_as_string(hide_password=False)

    def drop(self) -> None:
        cache.dispose_all()
        admin = self._admin()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{self._name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="module")
def _postgres_url() -> Iterator[str]:
    server = os.environ.get(POSTGRES_ENV, "").strip()
    if not server:
        pytest.skip(_POSTGRES_SKIP)
    database = ThrowawayPostgres(server)
    url = database.create()
    try:
        yield url
    finally:
        database.drop()


@pytest.fixture(params=[Dialect.SQLITE, Dialect.POSTGRESQL], ids=lambda d: d.value)
def database_url(request, tmp_path: Path) -> DatabaseUrl:
    """The shared database under test, on each dialect."""
    if request.param is Dialect.POSTGRESQL:
        return DatabaseUrl.parse(request.getfixturevalue("_postgres_url"))
    return DatabaseUrl.for_file(tmp_path / "shared.db")


def _scope(project: str = "project") -> StorageScope:
    """A scope nobody else in the run uses."""
    return StorageScope.for_tenant(
        Tenant(org=f"org-{uuid.uuid4().hex[:8]}", producer=f"person:{uuid.uuid4().hex[:8]}"),
        project=project,
    )


def _pair(url: DatabaseUrl) -> tuple[Database, Database]:
    """Two handles on one database, for two producers."""
    first = Database(url, scope=_scope())
    return first, first.in_scope(_scope())


# ── The schema ────────────────────────────────────────────────────


def test_alembic_builds_the_schema_the_models_describe(database_url: DatabaseUrl):
    Database(database_url)
    assert SchemaRevision.of(database_url) == HEAD_REVISION
    with cache.get_engine(database_url).connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        drift = compare_metadata(context, Base.metadata)
    assert drift == [], f"the migrations and the models disagree: {drift}"


def test_igni_migrate_prepares_the_named_database(database_url: DatabaseUrl, tmp_path: Path):
    result = CliRunner().invoke(
        migrate,
        ["--database-url", database_url.url, "--project-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert HEAD_REVISION in result.output
    assert len(result.output.strip().splitlines()) == 1, "one database, migrated once"
    password = make_url(database_url.url).password
    assert not password or password not in result.output


# ── Every store keeps its scope's rows ───────────────────────────


async def test_scheduled_tasks_are_scoped(database_url: DatabaseUrl):
    mine, theirs = _pair(database_url)
    task = ScheduledTask(
        id=uuid.uuid4().hex[:8],
        description="nightly",
        scheduled_at=datetime.now() - timedelta(minutes=1),
    )
    await TaskStore(db=mine).add(task)
    assert [t.id for t in await TaskStore(db=mine).get_due_tasks()] == [task.id]
    assert await TaskStore(db=theirs).get_all(include_done=True) == []
    assert await TaskStore(db=theirs).get(task.id) is None
    assert await TaskStore(db=theirs).remove(task.id) is False
    assert await TaskStore(db=mine).get(task.id) is not None


async def test_the_active_loop_is_one_row_per_scope(database_url: DatabaseUrl):
    mine, theirs = _pair(database_url)
    await LoopStore(db=mine).save(
        LoopState(run_id="a", prompt="mine", iteration_index=0, iterations_remaining=3)
    )
    await LoopStore(db=theirs).save(
        LoopState(run_id="b", prompt="theirs", iteration_index=1, iterations_remaining=2)
    )
    assert (await LoopStore(db=mine).load()).prompt == "mine"  # type: ignore[union-attr]
    assert (await LoopStore(db=theirs).load()).prompt == "theirs"  # type: ignore[union-attr]
    assert await LoopStore(db=theirs).clear() is True
    assert await LoopStore(db=mine).load() is not None


async def test_loop_progress_is_scoped(database_url: DatabaseUrl):
    mine, theirs = _pair(database_url)
    await LoopProgressStore(db=mine).set("run", "step", "done")
    await LoopProgressStore(db=theirs).set("run", "step", "other")
    assert await LoopProgressStore(db=mine).list("run") == [("step", "done")]
    assert await LoopProgressStore(db=theirs).get("run", "step") == "other"
    assert await LoopProgressStore(db=theirs).clear("run") == 1
    assert await LoopProgressStore(db=mine).get("run", "step") == "done"


async def test_background_processes_upsert_per_scope(database_url: DatabaseUrl):
    """A pid is per machine: two producers' VMs may both have pid 42."""
    mine, theirs = _pair(database_url)
    for db, cmd in ((mine, "npm run dev"), (theirs, "make serve")):
        result = await BackgroundProcessStore(db=db).upsert(
            BackgroundProcessRow(pid=42, cmd=cmd, pgid=42, started_at=1)
        )
        assert result.ok, result.reason
    replaced = await BackgroundProcessStore(db=mine).upsert(
        BackgroundProcessRow(pid=42, cmd="npm run dev -- --port 4000", pgid=42, started_at=2)
    )
    assert replaced.ok, replaced.reason
    assert [r.cmd for r in await BackgroundProcessStore(db=mine).list_all()] == [
        "npm run dev -- --port 4000"
    ]
    assert [r.cmd for r in await BackgroundProcessStore(db=theirs).list_all()] == ["make serve"]
    assert (await BackgroundProcessStore(db=theirs).finish(42, 0)).ok
    assert (await BackgroundProcessStore(db=mine).list_all())[0].finished_at is None


def test_received_messages_are_scoped(database_url: DatabaseUrl):
    mine, theirs = _pair(database_url)
    message_id = PendingMessageStore(mine).record_received("s1", "hello")
    PendingMessageStore(theirs).record_received("s1", "theirs")
    PendingMessageStore(theirs).mark_completed(message_id)
    PendingMessageStore(theirs).discard(message_id)
    assert [m.text for m in PendingMessageStore(mine).list_pending("s1")] == ["hello"]
    PendingMessageStore(mine).mark_interrupted(message_id, "errored", "boom")
    interrupted = PendingMessageStore(mine).list_interrupted("s1")
    assert [(m.interrupted_reason, m.last_error) for m in interrupted] == [("errored", "boom")]
    assert PendingMessageStore(theirs).list_interrupted("s1") == []
    assert PendingMessageStore(theirs).discard_all_for_session("s1") == 1
    assert PendingMessageStore(mine).discard_all_for_session("s1") == 1


def test_session_preferences_are_scoped(database_url: DatabaseUrl):
    mine, theirs = _pair(database_url)
    SessionPreferencesStore(mine).set_model("s1", "model-a")
    SessionPreferencesStore(mine).set_model("s1", "model-b")
    assert SessionPreferencesStore(mine).get_model("s1") == "model-b"
    assert SessionPreferencesStore(theirs).get_model("s1") is None


def test_session_directories_are_found_without_knowing_the_project(
    database_url: DatabaseUrl, tmp_path: Path
):
    """Which folder a session is in is asked before the project is known,
    so the lookup filters by org and producer — and only by them."""
    mine = Database(database_url, scope=_scope().without_project())
    theirs = mine.in_scope(_scope().without_project())
    SessionDirectoryStore(mine).set_dir("s1", tmp_path / "a")
    SessionDirectoryStore(mine).set_dir("s1", tmp_path / "b")
    assert SessionDirectoryStore(mine).get_dir("s1") == str(tmp_path / "b")
    assert SessionDirectoryStore(theirs).get_dir("s1") is None


def test_client_state_is_scoped(database_url: DatabaseUrl):
    mine, theirs = _pair(database_url)
    ClientStateStore(mine).set_value("window", "draft", "one")
    ClientStateStore(mine).set_value("window", "draft", "two")
    ClientStateStore(theirs).set_value("window", "draft", "theirs")
    assert ClientStateStore(mine).get_for_client("window") == {"draft": "two"}
    ClientStateStore(theirs).delete_value("window", "draft")
    assert ClientStateStore(mine).get_for_client("window") == {"draft": "two"}


def test_local_notifications_are_scoped_and_once_per_event(database_url: DatabaseUrl):
    mine, theirs = _pair(database_url)
    notice = ExtensionNotice(
        kind="export",
        title="Export ready",
        body="It is done.",
        event_key="export-1",
        target=NotificationTarget(page="exports"),
    )
    assert LocalNotificationStore(mine).add("ext", notice) is not None
    assert LocalNotificationStore(mine).add("ext", notice) is None, "an event is kept once"
    assert LocalNotificationStore(theirs).add("ext", notice) is not None, "per producer"
    assert LocalNotificationStore(mine).counts().unread == 1
    assert LocalNotificationStore(theirs).mark_read() == 1
    assert LocalNotificationStore(mine).counts().unread == 1
    assert [n.title for n in LocalNotificationStore(mine).listing()] == ["Export ready"]


def test_local_notifications_keep_the_newest(database_url: DatabaseUrl, monkeypatch):
    db = Database(database_url, scope=_scope())
    store = LocalNotificationStore(db)
    monkeypatch.setattr(LocalNotificationStore, "KEEP", 3)
    for n in range(5):
        store.add(
            "ext",
            ExtensionNotice(
                kind="k",
                title=f"n{n}",
                body="",
                event_key=f"e{n}",
                target=NotificationTarget(page="exports"),
            ),
        )
    assert [n.title for n in store.listing()] == ["n4", "n3", "n2"]


def test_memory_files_are_scoped(database_url: DatabaseUrl, tmp_path: Path):
    mine, theirs = _pair(database_url)
    DatabaseMemoryFiles(mine).write("MEMORY.md", "- [a](a.md)\n")
    DatabaseMemoryFiles(mine).write("MEMORY.md", "- [b](b.md)\n")
    DatabaseMemoryFiles(mine).write("b.md", "remember b")
    assert DatabaseMemoryFiles(mine).names() == ["MEMORY.md", "b.md"]
    assert DatabaseMemoryFiles(theirs).read("MEMORY.md") is None
    bank = ProjectMemoryBank(tmp_path, files=DatabaseMemoryFiles(mine))
    assert bank.load_index() == "- [b](b.md)\n"
    assert DatabaseMemoryFiles(theirs).delete("b.md") is False
    assert DatabaseMemoryFiles(mine).delete("b.md") is True


# ── The locator and the tenant ────────────────────────────────────


def test_a_shared_database_needs_somebody_signed_in(database_url: DatabaseUrl, tmp_path: Path):
    locator = StorageLocator(database_url=database_url.url, data_dir=tmp_path)
    assert locator.shared
    tenancy.use(lambda: None)
    with pytest.raises(NoTenantError):
        locator.project_database(tmp_path)


def test_the_locator_scopes_to_the_signed_in_producer_and_the_folder(
    database_url: DatabaseUrl, tmp_path: Path
):
    tenancy.use(lambda: Tenant(org="acme", producer="person:ada"))
    locator = StorageLocator(database_url=database_url.url, data_dir=tmp_path)
    project = locator.project_database(tmp_path)
    assert project.url == database_url == locator.global_url()
    assert (project.scope.org, project.scope.producer) == ("acme", "person:ada")
    assert project.scope.project and project.scope.shared
    assert locator.global_database().scope.project == ""
    files = MemoryFiles.for_project(tmp_path, tmp_path / "memory", locator)
    assert isinstance(files, DatabaseMemoryFiles)


def test_the_desktop_keeps_its_files(tmp_path: Path):
    locator = StorageLocator(database_url=None, data_dir=tmp_path)
    assert not locator.shared
    assert locator.project_url(tmp_path).file is not None
    assert locator.project_url(tmp_path).file.name == "state.db"  # type: ignore[union-attr]
    assert locator.global_url().file == (tmp_path / "ember.db").resolve()
    assert locator.scope(tmp_path) == StorageScope.local()
    assert isinstance(Attachments.for_project(tmp_path, locator), DirectoryAttachments)


# ── What only PostgreSQL has ─────────────────────────────────────


def test_attachments_are_large_objects_written_and_read_in_chunks(
    _postgres_url: str, tmp_path: Path
):
    url = DatabaseUrl.parse(_postgres_url)
    tenancy.use(lambda: Tenant(org="acme", producer="person:ada"))
    locator = StorageLocator(database_url=url.url, data_dir=tmp_path)
    store = Attachments.for_project(tmp_path, locator)
    assert isinstance(store, LargeObjectAttachments)

    payload = os.urandom(3 * LargeObjectAttachments.CHUNK + 17)
    written: list[int] = []

    def chunks() -> Iterator[bytes]:
        for start in range(0, len(payload), 10_000):
            written.append(start)
            yield payload[start : start + 10_000]

    stored = store.save("s1", "report.pdf", chunks())
    assert stored.size == len(payload)
    assert stored.ref.startswith(LargeObjectAttachments.REF_PREFIX)
    pieces = list(store.open(stored.ref))
    assert b"".join(pieces) == payload
    assert max(len(p) for p in pieces) <= LargeObjectAttachments.CHUNK
    assert len(written) > 1

    other = LargeObjectAttachments(Database(url, scope=_scope()))
    with pytest.raises(FileNotFoundError):
        list(other.open(stored.ref))
    assert other.delete(stored.ref) is False
    assert store.delete(stored.ref) is True  # type: ignore[attr-defined]


def test_a_failed_upload_leaves_no_large_object(_postgres_url: str):
    url = DatabaseUrl.parse(_postgres_url)
    store = LargeObjectAttachments(Database(url, scope=_scope()))
    with cache.get_engine(url).connect() as connection:
        before = connection.scalar(text("SELECT count(*) FROM pg_largeobject_metadata"))
    with pytest.raises(ValueError):
        store.save("s1", "x.bin", Base64Chunks("not base64!"))
    with cache.get_engine(url).connect() as connection:
        after = connection.scalar(text("SELECT count(*) FROM pg_largeobject_metadata"))
    assert after == before


def test_base64_chunks_decode_like_the_whole(tmp_path: Path):
    payload = os.urandom(200_000)
    encoded = base64.b64encode(payload).decode()
    wrapped = "\n".join(encoded[i : i + 76] for i in range(0, len(encoded), 76))
    assert b"".join(Base64Chunks(wrapped)) == payload
    stored = DirectoryAttachments(tmp_path).save("s1", "a b.bin", Base64Chunks(encoded))
    assert Path(stored.ref).name == "a_b.bin"
    assert Path(stored.ref).read_bytes() == payload


async def test_agno_rows_are_kept_to_their_scope(_postgres_url: str):
    url = DatabaseUrl.parse(_postgres_url)
    first, second = _scope(), _scope()
    engine = cache.get_async_engine(url)

    def agno_db(scope: StorageScope) -> ScopedAsyncPostgresDb:
        return ScopedAsyncPostgresDb(
            scope=scope,
            db_engine=engine,
            session_table="ember_sessions",
            memory_table="ember_memories",
        )

    mine, theirs = agno_db(first), agno_db(second)
    session_id = uuid.uuid4().hex[:8]
    now = int(time.time())
    await mine.upsert_session(
        AgentSession(session_id=session_id, agent_id="igni", created_at=now, updated_at=now)
    )
    await theirs.upsert_session(
        AgentSession(session_id=uuid.uuid4().hex[:8], agent_id="igni", created_at=now)
    )
    assert await mine.get_session(session_id, SessionType.AGENT) is not None
    assert await theirs.get_session(session_id, SessionType.AGENT) is None
    listed = await mine.get_sessions(session_type=SessionType.AGENT)
    assert [s.session_id for s in listed] == [session_id]  # type: ignore[union-attr]
    assert await theirs.delete_session(session_id) is False
    # The same id from another scope is not written over: Agno's upsert
    # only updates a row whose ``user_id`` is the writer's.
    await theirs.upsert_session(
        AgentSession(session_id=session_id, agent_id="other", created_at=now)
    )
    kept = await mine.get_session(session_id, SessionType.AGENT)
    assert kept is not None and kept.agent_id == "igni"  # type: ignore[union-attr]

    await mine.upsert_user_memory(UserMemory(memory="likes tea", user_id="someone-else"))
    assert [m.memory for m in await mine.get_user_memories()] == ["likes tea"]  # type: ignore[union-attr]
    assert await theirs.get_user_memories() == []
