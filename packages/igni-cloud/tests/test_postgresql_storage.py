"""What only PostgreSQL has: uploads as large objects, and Agno's rows kept to a scope.

``igni_cloud.storage.postgres`` — the ``igni.storage`` provider for a
``postgresql://`` URL. Each test needs ``IGNI_TEST_DATABASE_URL``.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from agno.db.base import SessionType
from agno.db.schemas.memory import UserMemory
from agno.session import AgentSession
from igni_cloud.storage.attachments import LargeObjectAttachments
from igni_cloud.storage.scoped_agno import ScopedAsyncPostgresDb
from sqlalchemy import text

from ember_code.backend.server_files import FilesController
from ember_code.core.db.database import Database
from ember_code.core.db.engine import cache
from ember_code.core.db.locator import StorageLocator
from ember_code.core.db.scope import StorageScope, Tenant, tenancy
from ember_code.core.db.urls import DatabaseUrl
from ember_code.core.utils.attachment_store import (
    AttachmentReferences,
    Attachments,
    Base64Chunks,
)
from tests.storage_backends import Scopes


def test_attachments_are_large_objects_written_and_read_in_chunks(
    postgres_url: str, tmp_path: Path
):
    url = DatabaseUrl.parse(postgres_url)
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

    other = LargeObjectAttachments(Database(url, scope=Scopes.fresh()))
    with pytest.raises(FileNotFoundError):
        list(other.open(stored.ref))
    assert other.delete(stored.ref) is False
    assert store.delete(stored.ref) is True  # type: ignore[attr-defined]


def test_a_failed_upload_leaves_no_large_object(postgres_url: str):
    url = DatabaseUrl.parse(postgres_url)
    store = LargeObjectAttachments(Database(url, scope=Scopes.fresh()))
    with cache.get_engine(url).connect() as connection:
        before = connection.scalar(text("SELECT count(*) FROM pg_largeobject_metadata"))
    with pytest.raises(ValueError):
        store.save("s1", "x.bin", Base64Chunks("not base64!"))
    with cache.get_engine(url).connect() as connection:
        after = connection.scalar(text("SELECT count(*) FROM pg_largeobject_metadata"))
    assert after == before


async def test_agno_rows_are_kept_to_their_scope(postgres_url: str):
    url = DatabaseUrl.parse(postgres_url)
    first, second = Scopes.fresh(), Scopes.fresh()
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
    # The same id in another scope is another row: the scope is part of
    # the key the database holds, not only a filter.
    await theirs.upsert_session(
        AgentSession(session_id=session_id, agent_id="other", created_at=now)
    )
    kept = await mine.get_session(session_id, SessionType.AGENT)
    assert kept is not None and kept.agent_id == "igni"  # type: ignore[union-attr]
    other = await theirs.get_session(session_id, SessionType.AGENT)
    assert other is not None and other.agent_id == "other"  # type: ignore[union-attr]
    assert other.session_id == session_id  # type: ignore[union-attr]
    with cache.get_engine(url).connect() as connection:
        stored = connection.scalars(
            text("SELECT session_id FROM ai.ember_sessions WHERE session_id LIKE :id"),
            {"id": f"%#{session_id}"},
        ).all()
    assert sorted(stored) == sorted([mine.stored_id(session_id), theirs.stored_id(session_id)])
    assert await mine.delete_session(session_id) is True
    assert await theirs.get_session(session_id, SessionType.AGENT) is not None

    await mine.upsert_user_memory(UserMemory(memory="likes tea", user_id="someone-else"))
    assert [m.memory for m in await mine.get_user_memories()] == ["likes tea"]  # type: ignore[union-attr]
    assert await theirs.get_user_memories() == []


def test_a_message_names_a_cloud_upload_and_it_is_read_through_the_store(
    postgres_url: str, tmp_path: Path
):
    url = DatabaseUrl.parse(postgres_url)
    tenancy.use(lambda: Tenant(org="acme", producer="person:ada"))
    locator = StorageLocator(database_url=url.url, data_dir=tmp_path)
    store = Attachments.for_project(tmp_path, locator)
    payload = os.urandom(2 * LargeObjectAttachments.CHUNK + 5)
    stored = store.save("s1", "notes.txt", [payload[:1000], payload[1000:]])

    text = f"summarise @{stored.ref} please"
    assert AttachmentReferences.present(text)
    localized = AttachmentReferences.localize(text, store)
    [path] = [token[1:] for token in localized.split() if token.startswith("@")]
    assert Path(path).read_bytes() == payload
    assert Path(path).is_relative_to(tmp_path / ".igni" / "attachments" / "s1")
    assert AttachmentReferences.localize(text, store) == localized, "the copy is reused"

    # The same store reads a desktop path, and an unknown reference stays as written.
    plain = tmp_path / "plain.txt"
    plain.write_text("on disk")
    assert b"".join(store.open(str(plain))) == b"on disk"
    unknown = "attachment:" + "0" * 32
    assert AttachmentReferences.localize(unknown, store) == unknown

    preview = FilesController(
        SimpleNamespace(
            project_dir=tmp_path,
            session_id="s1",
            settings=SimpleNamespace(
                storage=SimpleNamespace(data_dir=str(tmp_path), database_url=url.url)
            ),
        )
    ).read_file(stored.ref)
    assert preview.error == "" and preview.size == len(payload)
