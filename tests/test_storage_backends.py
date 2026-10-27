"""igni's stores on both databases: SQLite, and PostgreSQL when one is named.

``docs/CLOUD.md`` section 9. Every test here runs twice:

* ``sqlite`` — always: a shared SQLite file, the same code path the
  cloud takes (``IGNI_DATABASE_URL`` set, rows scoped by org, producer
  and project), on the dialect the desktop uses;
* ``postgresql`` — when ``IGNI_TEST_DATABASE_URL`` names a server the
  tests may create and drop databases on (``tests/storage_backends.py``).
  Unset, those cases skip and say so (``-rs`` prints why). Each run
  creates a database of its own and drops it afterwards.

What they hold every store to: a row written in one scope is invisible
from another, and the schema alembic builds is the one the models say.
"""

from __future__ import annotations

import base64
import os
import subprocess
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from click.testing import CliRunner
from sqlalchemy.engine import make_url

from ember_code.cli.migrate import migrate
from ember_code.core.approvals import journal as approval_journal  # noqa: F401 — its tables
from ember_code.core.db.base import Base
from ember_code.core.db.database import Database
from ember_code.core.db.engine import cache
from ember_code.core.db.locator import StorageLocator
from ember_code.core.db.schema import HEAD_REVISION, SchemaRevision
from ember_code.core.db.scope import NoTenantError, StorageScope, Tenant, tenancy
from ember_code.core.db.urls import DatabaseUrl
from ember_code.core.hooks.memory_sync import MemorySyncHook
from ember_code.core.loop.models import LoopState
from ember_code.core.loop.store import LoopProgressStore, LoopStore
from ember_code.core.scheduler.models import ScheduledTask
from ember_code.core.scheduler.store import TaskStore
from ember_code.core.session import queued_messages  # noqa: F401 — its table
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
)
from ember_code.core.utils.context_memory import ProjectMemoryBank
from ember_code.core.utils.memory_files import (
    DatabaseMemoryFiles,
    DirectoryMemoryFiles,
    MemoryFiles,
    MemoryMirror,
)
from ember_code.extensions.settings_values import (
    DatabaseSettingsValues,
    FileSettingsValues,
    SettingsValues,
)
from ember_code.notifications.local_store import LocalNotificationStore
from ember_code.notifications.schemas import ExtensionNotice, NotificationTarget
from tests.storage_backends import Scopes

# ── The schema ────────────────────────────────────────────────────


#: Tables a shared database's dialect makes when ``igni migrate`` prepares it.
PREPARED_BY_THE_DIALECT = frozenset({"producer_roles"})


def test_alembic_builds_the_schema_the_models_describe(database_url: DatabaseUrl):
    Database(database_url)
    assert SchemaRevision.of(database_url) == HEAD_REVISION
    with cache.get_engine(database_url).connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={
                "compare_type": True,
                # What a dialect's own preparation adds is not igni's schema
                # (PostgreSQL's row-level security keeps ``producer_roles``).
                "include_name": lambda name, kind, _parent: (
                    not (kind == "table" and name in PREPARED_BY_THE_DIALECT)
                ),
            },
        )
        drift = compare_metadata(context, Base.metadata)
    assert drift == [], f"the migrations and the models disagree: {drift}"


def test_igni_migrate_prepares_the_named_database(database_url: DatabaseUrl, tmp_path: Path):
    result = CliRunner().invoke(
        migrate,
        ["--database-url", database_url.url, "--project-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert HEAD_REVISION in result.output
    reports = [line for line in result.output.splitlines() if HEAD_REVISION in line]
    assert len(reports) == 1, "one database, migrated once"
    password = make_url(database_url.url).password
    # migrate prints the redacted URL (password masked to ***); assert the real
    # secret never appears in its ``:password@`` credential position. A plain
    # ``password not in output`` false-fails when the password equals a common
    # substring — e.g. "postgres", which is also the scheme and default username.
    assert not password or f":{password}@" not in result.output


# ── Every store keeps its scope's rows ───────────────────────────


async def test_scheduled_tasks_are_scoped(database_url: DatabaseUrl):
    mine, theirs = Scopes.pair(database_url)
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
    mine, theirs = Scopes.pair(database_url)
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
    mine, theirs = Scopes.pair(database_url)
    await LoopProgressStore(db=mine).set("run", "step", "done")
    await LoopProgressStore(db=theirs).set("run", "step", "other")
    assert await LoopProgressStore(db=mine).list("run") == [("step", "done")]
    assert await LoopProgressStore(db=theirs).get("run", "step") == "other"
    assert await LoopProgressStore(db=theirs).clear("run") == 1
    assert await LoopProgressStore(db=mine).get("run", "step") == "done"


async def test_background_processes_upsert_per_scope(database_url: DatabaseUrl):
    """A pid is per machine: two producers' VMs may both have pid 42."""
    mine, theirs = Scopes.pair(database_url)
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
    mine, theirs = Scopes.pair(database_url)
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
    mine, theirs = Scopes.pair(database_url)
    SessionPreferencesStore(mine).set_model("s1", "model-a")
    SessionPreferencesStore(mine).set_model("s1", "model-b")
    assert SessionPreferencesStore(mine).get_model("s1") == "model-b"
    assert SessionPreferencesStore(theirs).get_model("s1") is None


def test_session_directories_are_found_without_knowing_the_project(
    database_url: DatabaseUrl, tmp_path: Path
):
    """Which folder a session is in is asked before the project is known,
    so the lookup filters by org and producer — and only by them."""
    mine = Database(database_url, scope=Scopes.fresh().without_project())
    theirs = mine.in_scope(Scopes.fresh().without_project())
    SessionDirectoryStore(mine).set_dir("s1", tmp_path / "a")
    SessionDirectoryStore(mine).set_dir("s1", tmp_path / "b")
    assert SessionDirectoryStore(mine).get_dir("s1") == str(tmp_path / "b")
    assert SessionDirectoryStore(theirs).get_dir("s1") is None


def test_client_state_is_scoped(database_url: DatabaseUrl):
    mine, theirs = Scopes.pair(database_url)
    ClientStateStore(mine).set_value("window", "draft", "one")
    ClientStateStore(mine).set_value("window", "draft", "two")
    ClientStateStore(theirs).set_value("window", "draft", "theirs")
    assert ClientStateStore(mine).get_for_client("window") == {"draft": "two"}
    ClientStateStore(theirs).delete_value("window", "draft")
    assert ClientStateStore(mine).get_for_client("window") == {"draft": "two"}


def test_local_notifications_are_scoped_and_once_per_event(database_url: DatabaseUrl):
    mine, theirs = Scopes.pair(database_url)
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
    db = Database(database_url, scope=Scopes.fresh())
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
    mine, theirs = Scopes.pair(database_url)
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


def test_base64_chunks_decode_like_the_whole(tmp_path: Path):
    payload = os.urandom(200_000)
    encoded = base64.b64encode(payload).decode()
    wrapped = "\n".join(encoded[i : i + 76] for i in range(0, len(encoded), 76))
    assert b"".join(Base64Chunks(wrapped)) == payload
    stored = DirectoryAttachments(tmp_path).save("s1", "a b.bin", Base64Chunks(encoded))
    assert Path(stored.ref).name == "a_b.bin"
    assert Path(stored.ref).read_bytes() == payload


# ── Memory files the agent writes ─────────────────────────────────


async def test_whatever_writes_a_memory_file_writes_the_row(
    database_url: DatabaseUrl, tmp_path: Path
):
    """In the cloud the memory directory is a mirror of the rows: a file
    written by any tool — ``save_file``, a shell ``echo`` — is stored,
    and nothing reaches past it."""
    db = Database(database_url, scope=Scopes.fresh())
    files = DatabaseMemoryFiles(db)
    files.write("old.md", "from an earlier session")
    directory = tmp_path / "memory"
    mirror = MemoryMirror(files, directory)
    assert mirror.fill() == 1
    assert (directory / "old.md").read_text() == "from an earlier session"

    def save_file(file_name: str, contents: str) -> str:
        (directory / file_name).write_text(contents)
        return "saved"

    hook = MemorySyncHook(mirror)
    assert (
        await hook(
            name="save_file",
            func=save_file,
            args={"file_name": "MEMORY.md", "contents": "- [Tea](tea.md)\n"},
        )
        == "saved"
    )
    assert files.read("MEMORY.md") == "- [Tea](tea.md)\n"

    subprocess.run(
        [
            "sh",
            "-c",
            f"echo 'likes green tea' > '{directory / 'tea.md'}'; rm '{directory / 'old.md'}'",
        ],
        check=True,
    )
    assert mirror.sync() == 2
    assert files.names() == ["MEMORY.md", "tea.md"]
    assert files.read("tea.md") == "likes green tea\n"
    assert ProjectMemoryBank(tmp_path, files=files).load_index() == "- [Tea](tea.md)\n"
    assert mirror.sync() == 0, "nothing changed, nothing written"

    # A new VM: the directory comes back from the rows.
    fresh = tmp_path / "fresh"
    assert MemoryMirror(files, fresh).fill() == 2
    assert (fresh / "MEMORY.md").read_text() == "- [Tea](tea.md)\n"


def test_the_desktop_has_no_mirror(tmp_path: Path):
    bank = ProjectMemoryBank(tmp_path, files=DirectoryMemoryFiles(tmp_path / "memory"))
    assert bank.mirror() is None


# ── Reading an attachment ────────────────────────────────────────


def test_the_desktop_store_reads_paths_and_knows_no_cloud_references(tmp_path: Path):
    store = DirectoryAttachments(tmp_path)
    stored = store.save("s1", "a.txt", [b"hello"])
    assert store.local_path(stored.ref) == Path(stored.ref)
    assert b"".join(store.open(stored.ref)) == b"hello"
    assert store.local_path("attachment:" + "1" * 32) is None


def test_extension_settings_are_per_producer_in_the_cloud(
    database_url: DatabaseUrl, tmp_path: Path
):
    mine, theirs = Scopes.pair(database_url)
    DatabaseSettingsValues(mine.in_scope(mine.scope.without_project())).write(
        "acme.reviewer", {"strict": True}
    )
    assert DatabaseSettingsValues(mine.in_scope(mine.scope.without_project())).read(
        "acme.reviewer"
    ) == {"strict": True}
    assert DatabaseSettingsValues(theirs).read("acme.reviewer") == {}

    tenancy.use(lambda: Tenant(org="acme", producer="person:ada"))
    locator = StorageLocator(database_url=database_url.url, data_dir=tmp_path)
    values = SettingsValues.for_store(tmp_path / "extensions", locator)
    assert isinstance(values, DatabaseSettingsValues)
    values.write("acme.reviewer", {"level": "high"})
    assert values.read("acme.reviewer") == {"level": "high"}
    assert not list(tmp_path.glob("**/*.settings.json")), "no file in the cloud"
    values.delete("acme.reviewer")
    assert values.read("acme.reviewer") == {}


def test_the_desktop_keeps_extension_settings_beside_the_bundles(tmp_path: Path):
    values = SettingsValues.for_store(
        tmp_path, StorageLocator(database_url=None, data_dir=tmp_path)
    )
    assert isinstance(values, FileSettingsValues)
    values.write("acme.reviewer", {"strict": True})
    assert (tmp_path / "acme.reviewer.settings.json").is_file()
