"""Cross-restart orphan-process rehydration.

When the BE restarts, its in-memory ``_ProcessRegistry`` is wiped
— but processes spawned via ``run_shell_command(background=True)``
keep running (``start_new_session=True`` detaches them from the
parent's process group). Without DB persistence the watcher
can't see them, the user can't kill them via the UI, and the
process holds ports / file locks until the user finds it with
``lsof`` and kills it manually.

These tests pin the fix:

* :class:`BackgroundProcessStore` round-trips rows through real
  SQLite (in-memory file). Same shape as the matching
  ``test_session_data_real_db.py`` for plan decisions.
* ``rehydrate_orphan_processes`` injects alive pids into the
  registry as :class:`_OrphanProcess` entries; dead pids get
  pruned from the DB.
* :class:`_OrphanProcess` quacks like ``_ManagedProcess``: the
  registry's ``all_running`` reports it, ``read`` returns a
  placeholder, ``kill`` sends SIGTERM via the saved pgid.
* ``stop_background_process`` correctly cleans up an orphan
  (registry + DB rows go away; no reader to wait on).
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import pytest

from ember_code.backend.server import BackendServer
from ember_code.core.tools import shell as shell_mod
from ember_code.core.tools.process_store import (
    BackgroundProcessRow,
    BackgroundProcessStore,
)
from ember_code.core.tools.shell import (
    _OrphanProcess,
    _persist_add,
    _persist_remove,
    _ProcessRegistry,
    rehydrate_orphan_processes,
    set_process_store,
)

# ── BackgroundProcessStore round-trip ────────────────────────


class TestBackgroundProcessStore:
    async def test_upsert_then_list(self, tmp_path: Path) -> None:
        store = BackgroundProcessStore(db_path=tmp_path / "state.db")
        await store.upsert(
            BackgroundProcessRow(pid=1234, cmd="npm run dev", pgid=1234, started_at=1700)
        )
        await store.upsert(
            BackgroundProcessRow(pid=5678, cmd="tail -f x", pgid=5678, started_at=1800)
        )
        rows = await store.list_all()
        assert len(rows) == 2
        pids = {r.pid for r in rows}
        assert pids == {1234, 5678}

    async def test_upsert_replaces_on_pid_collision(self, tmp_path: Path) -> None:
        # OS pid reuse is rare but real — the latest spawn wins,
        # we don't accumulate stale rows.
        store = BackgroundProcessStore(db_path=tmp_path / "state.db")
        await store.upsert(BackgroundProcessRow(pid=9, cmd="old cmd", pgid=9, started_at=100))
        await store.upsert(BackgroundProcessRow(pid=9, cmd="new cmd", pgid=9, started_at=200))
        rows = await store.list_all()
        assert len(rows) == 1
        assert rows[0].cmd == "new cmd"
        assert rows[0].started_at == 200

    async def test_remove_is_idempotent(self, tmp_path: Path) -> None:
        # Hot-path callers (``_emit_completion``) fire-and-forget
        # delete — a duplicate call must not error.
        store = BackgroundProcessStore(db_path=tmp_path / "state.db")
        await store.upsert(BackgroundProcessRow(pid=42, cmd="x", pgid=42, started_at=0))
        await store.remove(42)
        await store.remove(42)  # already gone — no error
        assert await store.list_all() == []

    async def test_fresh_store_instance_reads_prior_write(self, tmp_path: Path) -> None:
        # The actual cross-restart scenario: one store writes,
        # gets discarded (BE shutdown), another instance reads
        # the rows back from the same file.
        db_path = tmp_path / "state.db"
        writer = BackgroundProcessStore(db_path=db_path)
        await writer.upsert(
            BackgroundProcessRow(pid=7, cmd="orphaned dev server", pgid=7, started_at=42)
        )

        reader = BackgroundProcessStore(db_path=db_path)
        rows = await reader.list_all()
        assert len(rows) == 1
        assert rows[0].pid == 7
        assert rows[0].cmd == "orphaned dev server"


# ── _OrphanProcess duck-typing ───────────────────────────────


class TestOrphanProcess:
    def setup_method(self) -> None:
        # Each test gets a fresh registry — orphan entries from a
        # prior test would survive otherwise (module-global).
        shell_mod._registry._processes.clear()  # type: ignore[attr-defined]

    def test_is_running_true_for_alive_pid(self) -> None:
        # Use our own pid as the "alive" sample — guaranteed to
        # exist for the duration of the test.
        orphan = _OrphanProcess(
            pid=os.getpid(), cmd="self", started_epoch=int(time.time()), pgid=None
        )
        assert orphan.is_running() is True

    def test_is_running_false_for_dead_pid(self) -> None:
        # ``-1`` is never a valid pid; ``os.kill(-1, 0)`` raises
        # PermissionError on Linux/macOS in practice. Use a
        # provably-dead pid by picking something well outside
        # PID_MAX. ``os.kill(0x7FFFFFFF, 0)`` raises ProcessLookupError.
        orphan = _OrphanProcess(pid=0x7FFFFFFE, cmd="dead", started_epoch=0, pgid=None)
        assert orphan.is_running() is False
        # State is sticky — once observed dead it stays dead.
        assert orphan.is_running() is False

    def test_read_returns_placeholder_not_empty(self) -> None:
        # The watcher's tail pane needs SOMETHING to render, not
        # an empty string. When the log file is missing/empty
        # (no buffered output yet), the orphan returns the
        # explanatory placeholder. The exact wording changed
        # when per-pid log files landed — match the durable
        # part ("no buffered output" + "Kill button").
        # Force the lookup into a tmp dir so we don't read from
        # a real project's log file by accident.
        import tempfile

        from ember_code.core.tools import process_log

        with tempfile.TemporaryDirectory() as tmp:
            process_log.set_default_project_dir(tmp)
            try:
                orphan = _OrphanProcess(
                    pid=os.getpid(), cmd="x", started_epoch=int(time.time()), pgid=None
                )
                text = orphan.read()
                assert "no buffered output" in text.lower()
                assert "kill button" in text.lower()
            finally:
                process_log.set_default_project_dir(None)

    def test_kill_no_pgid_does_not_raise(self) -> None:
        # PGid missing (e.g. row from a different platform / OS
        # error during ``getpgid``) — kill must still attempt the
        # bare pid call and not crash.
        orphan = _OrphanProcess(pid=0x7FFFFFFE, cmd="x", started_epoch=0, pgid=None)
        orphan.kill()  # must not raise

    def test_registry_all_running_handles_orphan(self) -> None:
        # ``_ProcessRegistry.all_running`` previously assumed
        # ``mp.started_at`` (monotonic). Orphans carry epoch
        # instead — the branch must produce a sensible elapsed.
        reg = _ProcessRegistry()
        orphan = _OrphanProcess(
            pid=os.getpid(),
            cmd="self",
            started_epoch=int(time.time()) - 30,
            pgid=None,
        )
        reg.add(orphan)  # type: ignore[arg-type]
        rows = reg.all_running()
        assert len(rows) == 1
        pid, cmd, elapsed = rows[0]
        assert pid == os.getpid()
        assert cmd == "self"
        # ~30s ago, allow a few seconds slop.
        assert 25 <= elapsed <= 60


# ── rehydrate_orphan_processes ───────────────────────────────


class TestRehydrateOrphanProcesses:
    def setup_method(self) -> None:
        shell_mod._registry._processes.clear()  # type: ignore[attr-defined]
        set_process_store(None)

    async def test_surfaces_alive_pid_as_orphan(self, tmp_path: Path) -> None:
        # Seed the DB with our own pid (guaranteed alive), then
        # rehydrate. The registry should now have the orphan.
        store = BackgroundProcessStore(db_path=tmp_path / "state.db")
        await store.upsert(
            BackgroundProcessRow(
                pid=os.getpid(),
                cmd="pretend dev server",
                pgid=os.getpid(),
                started_at=int(time.time()) - 10,
            )
        )

        # Patch the resolver so rehydrate uses our tmp DB.
        from ember_code.core.tools import process_store as ps_mod

        original_resolver = ps_mod._resolve_db_path
        ps_mod._resolve_db_path = lambda *_, **__: tmp_path / "state.db"
        try:
            count = await rehydrate_orphan_processes(project_dir=tmp_path)
        finally:
            ps_mod._resolve_db_path = original_resolver

        assert count == 1
        mp = shell_mod._registry.get(os.getpid())
        assert isinstance(mp, _OrphanProcess)
        assert mp.cmd == "pretend dev server"

    async def test_prunes_dead_pid(self, tmp_path: Path) -> None:
        # Seed with a dead pid. Rehydrate should NOT add it AND
        # should remove the stale DB row.
        dead_pid = 0x7FFFFFFE
        store = BackgroundProcessStore(db_path=tmp_path / "state.db")
        await store.upsert(
            BackgroundProcessRow(pid=dead_pid, cmd="ghost", pgid=dead_pid, started_at=0)
        )

        from ember_code.core.tools import process_store as ps_mod

        original_resolver = ps_mod._resolve_db_path
        ps_mod._resolve_db_path = lambda *_, **__: tmp_path / "state.db"
        try:
            count = await rehydrate_orphan_processes(project_dir=tmp_path)
        finally:
            ps_mod._resolve_db_path = original_resolver

        assert count == 0
        assert shell_mod._registry.get(dead_pid) is None
        # Dead row was pruned from disk.
        rows = await BackgroundProcessStore(db_path=tmp_path / "state.db").list_all()
        assert rows == []

    async def test_empty_db_is_noop(self, tmp_path: Path) -> None:
        from ember_code.core.tools import process_store as ps_mod

        original_resolver = ps_mod._resolve_db_path
        ps_mod._resolve_db_path = lambda *_, **__: tmp_path / "state.db"
        try:
            count = await rehydrate_orphan_processes(project_dir=tmp_path)
        finally:
            ps_mod._resolve_db_path = original_resolver

        assert count == 0


# ── BackendServer.stop_background_process for orphans ────────


class TestStopOrphanProcess:
    def setup_method(self) -> None:
        shell_mod._registry._processes.clear()  # type: ignore[attr-defined]

    async def test_killing_orphan_drops_registry_and_db_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Use a doubly-stubbed orphan so we don't actually kill
        # anything. Track that ``kill`` was called and that
        # cleanup landed.
        store = BackgroundProcessStore(db_path=tmp_path / "state.db")
        await store.upsert(BackgroundProcessRow(pid=12345, cmd="fake", pgid=12345, started_at=0))
        set_process_store(store)

        kill_calls: list[int] = []

        class _Fake(_OrphanProcess):
            def is_running(self) -> bool:  # type: ignore[override]
                # Behaves alive until kill is called.
                return self.pid not in kill_calls

            def kill(self) -> None:  # type: ignore[override]
                kill_calls.append(self.pid)

        fake = _Fake(pid=12345, cmd="fake", started_epoch=int(time.time()), pgid=12345)
        shell_mod._registry.add(fake)  # type: ignore[arg-type]

        server = BackendServer.__new__(BackendServer)
        result = await server.stop_background_process(pid=12345)

        assert result["killed"] is True
        assert kill_calls == [12345]
        # Orphan path explicitly removed the registry row.
        assert shell_mod._registry.get(12345) is None
        # And scheduled the DB delete. The fire-and-forget task
        # runs on the loop; give it a tick to flush.
        import asyncio

        await asyncio.sleep(0.05)
        rows = await store.list_all()
        assert rows == []

        # Clean up module-global state for test isolation.
        set_process_store(None)


# ── Persist add/remove fire-and-forget guards ────────────────


class TestPersistGuards:
    """The persistence hooks in the registry's hot path must NOT
    raise when no store is wired (test/headless contexts) or
    when there's no running loop. These guards exist so the
    shell tool stays usable in the standalone pytest harness."""

    def test_persist_add_noop_when_store_unset(self) -> None:
        set_process_store(None)
        # No exception even though there's no store + no loop.
        _persist_add(12345, "x")

    def test_persist_remove_noop_when_store_unset(self) -> None:
        set_process_store(None)
        _persist_remove(12345)


# Silence unused-import warnings — kept so the test runner can
# resolve module attributes on test discovery.
_ = signal
_ = sys
