"""What a bound backend does before its first answer, and what it no longer repeats.

``docs/CLOUD.md`` §9. A warm base's igni boots its backend after binding,
so every repeated step there is paid on each producer's first request:
the git subprocess behind a project's id (asked by every store that
opens a scope) and Agno's table checks (three round trips before every
query) are now each done once.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from agno.db.base import SessionType
from sqlalchemy import event

from ember_code.backend.guest.binding import ProducerBinding
from ember_code.backend.guest.watch import DirectoryWatch
from ember_code.core.data_paths import ProjectIds
from ember_code.core.db.agno_tables import AgnoTables
from ember_code.core.db.engine import cache
from ember_code.core.db.scope import StorageScope, Tenant
from ember_code.core.db.urls import DatabaseUrl
from ember_code.core.memory.manager import StorageManager
from ember_code.core.memory.scoped_postgres import ScopedAsyncPostgresDb
from ember_code.core.profile import PROFILE_ENV


class GitCalls:
    def __init__(self, monkeypatch) -> None:
        self.count = 0
        ask = ProjectIds._ask_git

        def counted(project: Path) -> str:
            self.count += 1
            return ask(project)

        monkeypatch.setattr(ProjectIds, "_ask_git", staticmethod(counted))


def _repo(path: Path, remote: str | None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if remote:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote], check=True)
    return path


def test_a_project_id_asks_git_once(tmp_path: Path, monkeypatch):
    calls = GitCalls(monkeypatch)
    ids = ProjectIds()
    project = _repo(tmp_path / "repo", "https://example.test/a.git")
    first = ids.of(project)
    assert all(ids.of(project) == first for _ in range(10))
    assert ids.of(project / "sub" / "..") == first
    assert calls.count == 1


def test_a_changed_remote_is_seen(tmp_path: Path, monkeypatch):
    ids = ProjectIds()
    project = _repo(tmp_path / "repo", None)
    by_path = ids.of(project)
    subprocess.run(
        ["git", "-C", str(project), "remote", "add", "origin", "https://example.test/b.git"],
        check=True,
    )
    by_remote = ids.of(project)
    assert by_remote != by_path
    other_clone = _repo(tmp_path / "clone", "https://example.test/b.git")
    assert ids.of(other_clone) == by_remote, "two clones, one id"


def test_a_folder_inside_a_repository_follows_its_config(tmp_path: Path):
    ids = ProjectIds()
    project = _repo(tmp_path / "repo", None)
    inside = project / "packages" / "app"
    inside.mkdir(parents=True)
    before = ids.of(inside)
    subprocess.run(
        ["git", "-C", str(project), "remote", "add", "origin", "https://example.test/c.git"],
        check=True,
    )
    assert ids.of(inside) != before


def test_outside_any_repository_the_path_is_the_id(tmp_path: Path, monkeypatch):
    calls = GitCalls(monkeypatch)
    ids = ProjectIds()
    plain = tmp_path / "plain"
    plain.mkdir()
    assert ids.of(plain) == ids.of(plain)
    assert calls.count == 1


def test_agnos_tables_are_looked_up_once_per_process(postgres_url: str):
    url = DatabaseUrl.parse(postgres_url)
    AgnoTables(
        url, session_table=StorageManager.SESSION_TABLE, memory_table=StorageManager.MEMORY_TABLE
    ).prepare()
    scope = StorageScope.for_tenant(Tenant(org="acme", producer="person:ada"), project="p")
    statements: list[str] = []

    def heard(_conn, _cursor, statement, *_args) -> None:
        statements.append(statement)

    async def read_three_times() -> int:
        engine = cache.get_async_engine(url)

        def database() -> ScopedAsyncPostgresDb:
            return ScopedAsyncPostgresDb(
                scope=scope,
                db_engine=engine,
                create_schema=False,
                session_table=StorageManager.SESSION_TABLE,
                memory_table=StorageManager.MEMORY_TABLE,
            )

        event.listen(engine.sync_engine, "before_cursor_execute", heard)
        try:
            await database().get_session("s1", SessionType.AGENT)
            first = len(statements)
            statements.clear()
            await database().get_session("s1", SessionType.AGENT)
            await database().get_session("s2", SessionType.AGENT)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", heard)
            await cache.reset_pools()
        return first

    first = asyncio.run(read_three_times())
    assert first > 2, "the first lookup checks and reflects the table"
    assert len(statements) == 2, "afterwards: one query per read, nothing else"


@pytest.fixture(autouse=True)
def _fresh_table_cache():
    ScopedAsyncPostgresDb._tables.clear()
    yield
    ScopedAsyncPostgresDb._tables.clear()


# ── Binding as quick as the rename ────────────────────────────────


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="inotify is Linux's")
async def test_a_rename_into_the_run_directory_wakes_the_watch(tmp_path: Path):
    watch = DirectoryWatch(tmp_path)
    assert watch.start()
    try:
        (tmp_path / ".producer.env.tmp").write_text("x")
        await watch.wait(0.05)
        os.replace(tmp_path / ".producer.env.tmp", tmp_path / "producer.env")
        assert await watch.wait(1.0)
        assert not await watch.wait(0.05)
    finally:
        watch.close()


async def test_without_inotify_it_says_so_once(tmp_path: Path):
    watch = DirectoryWatch(tmp_path / "missing")
    assert not watch.start()
    assert not watch.start()
    assert not watch.watching


async def test_binding_wakes_on_the_rename_and_falls_back_to_polling(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(PROFILE_ENV, "cloud")
    for name in ("IGNI_DATABASE_URL", "IGNI_ACCESS_TOKEN_FILE", "IGNI_ORG", "IGNI_PRODUCER"):
        monkeypatch.setenv(name, "")  # recorded, so put back as it was
        monkeypatch.delenv(name, raising=False)
    binding = ProducerBinding(tmp_path / "producer.env")
    waiting = asyncio.create_task(binding.bind())
    await asyncio.sleep(0.05)
    (tmp_path / "token").write_text(_token())
    (tmp_path / ".p.tmp").write_text(
        f"IGNI_ACCESS_TOKEN_FILE={tmp_path / 'token'}\nIGNI_DATABASE_URL=sqlite:///x.db\n"
    )
    handed = time.perf_counter()
    os.replace(tmp_path / ".p.tmp", tmp_path / "producer.env")
    await asyncio.wait_for(waiting, timeout=5)
    latency = time.perf_counter() - handed
    await binding.stop()
    if binding.watching or sys.platform.startswith("linux"):
        assert latency < 0.05, "woken by the rename"
    else:
        assert latency < ProducerBinding.POLL_SECONDS * 3, "polled where inotify is missing"


def _token() -> str:
    def part(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    claims = {"sub": "u-ada", "org": "org-acme", "exp": int(time.time()) + 3600}
    return f"{part(b'{}')}.{part(json.dumps(claims).encode())}.sig"
