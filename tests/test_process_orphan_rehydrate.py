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
* :meth:`OrphanRehydrator.run` (and the thin
  ``rehydrate_orphan_processes`` back-compat wrapper) injects
  alive pids into the registry as :class:`OrphanProcess` entries;
  dead pids get pruned from the DB and the typed
  :class:`RehydrateResult` populates ``surfaced`` / ``pruned`` /
  ``reason`` fields so failure modes are observable.
* :class:`OrphanProcess` quacks like ``ManagedProcess``: the
  registry's ``all_running`` reports it, ``read`` returns a
  placeholder, ``kill`` sends SIGTERM via the saved pgid,
  ``is_orphan`` is ``True`` (paired with
  :attr:`ManagedProcess.is_orphan` = ``False``).
* ``stop_background_process`` correctly cleans up an orphan
  (registry + DB rows go away; no reader to wait on).
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

import pytest

from ember_code.backend.server import BackendServer
from ember_code.core.tools import process_store as ps_mod
from ember_code.core.tools.orphan_process import OrphanProcess
from ember_code.core.tools.orphan_rehydrator import (
    OrphanRehydrator,
    build_rehydrator,
)
from ember_code.core.tools.process_registry import ProcessRegistry
from ember_code.core.tools.process_store import (
    BackgroundProcessRow,
    BackgroundProcessStore,
)
from ember_code.core.tools.process_supervisor_locator import supervisors
from ember_code.core.tools.shell_orphan import (
    rehydrate_orphan_processes,
)
from ember_code.core.tools.shell_orphan_schemas import RehydrateResult

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


# ── OrphanProcess duck-typing ───────────────────────────────


class TestOrphanProcess:
    def setup_method(self) -> None:
        # Each test gets a fresh registry — orphan entries from a
        # prior test would survive otherwise (module-global).
        supervisors.default().registry.clear()

    def test_is_running_true_for_alive_pid(self) -> None:
        # Use our own pid as the "alive" sample — guaranteed to
        # exist for the duration of the test.
        orphan = OrphanProcess(
            pid=os.getpid(), cmd="self", started_epoch=int(time.time()), pgid=None
        )
        assert orphan.is_running() is True

    def test_is_running_false_for_dead_pid(self) -> None:
        # ``-1`` is never a valid pid; ``os.kill(-1, 0)`` raises
        # PermissionError on Linux/macOS in practice. Use a
        # provably-dead pid by picking something well outside
        # PID_MAX. ``os.kill(0x7FFFFFFF, 0)`` raises ProcessLookupError.
        orphan = OrphanProcess(pid=0x7FFFFFFE, cmd="dead", started_epoch=0, pgid=None)
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
        with tempfile.TemporaryDirectory() as tmp:
            supervisors.default().configure_log_store(tmp)
            try:
                orphan = OrphanProcess(
                    pid=os.getpid(), cmd="x", started_epoch=int(time.time()), pgid=None
                )
                text = orphan.read()
                assert "no buffered output" in text.lower()
                assert "kill button" in text.lower()
            finally:
                supervisors.default().configure_log_store(None)

    def test_kill_no_pgid_does_not_raise(self) -> None:
        # PGid missing (e.g. row from a different platform / OS
        # error during ``getpgid``) — kill must still attempt the
        # bare pid call and not crash.
        orphan = OrphanProcess(pid=0x7FFFFFFE, cmd="x", started_epoch=0, pgid=None)
        orphan.kill()  # must not raise

    def test_registry_all_running_handles_orphan(self) -> None:
        # ``ProcessRegistry.all_running`` previously assumed
        # ``mp.started_at`` (monotonic). Orphans carry epoch
        # instead — the branch must produce a sensible elapsed.
        reg = ProcessRegistry()
        orphan = OrphanProcess(
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
        supervisor = supervisors.default()
        supervisor.registry.clear()
        supervisor.registry.attach_persistence(None)

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
        original_resolver = ps_mod._resolve_db_path
        ps_mod._resolve_db_path = lambda *_, **__: tmp_path / "state.db"
        try:
            count = await rehydrate_orphan_processes(project_dir=tmp_path)
        finally:
            ps_mod._resolve_db_path = original_resolver

        assert count == 1
        mp = supervisors.default().registry.get(os.getpid())
        assert isinstance(mp, OrphanProcess)
        assert mp.cmd == "pretend dev server"

    async def test_prunes_dead_pid(self, tmp_path: Path) -> None:
        # Seed with a dead pid. Rehydrate should NOT add it AND
        # should remove the stale DB row.
        dead_pid = 0x7FFFFFFE
        store = BackgroundProcessStore(db_path=tmp_path / "state.db")
        await store.upsert(
            BackgroundProcessRow(pid=dead_pid, cmd="ghost", pgid=dead_pid, started_at=0)
        )

        original_resolver = ps_mod._resolve_db_path
        ps_mod._resolve_db_path = lambda *_, **__: tmp_path / "state.db"
        try:
            count = await rehydrate_orphan_processes(project_dir=tmp_path)
        finally:
            ps_mod._resolve_db_path = original_resolver

        assert count == 0
        assert supervisors.default().registry.get(dead_pid) is None
        # Dead row was pruned from disk.
        rows = await BackgroundProcessStore(db_path=tmp_path / "state.db").list_all()
        assert rows == []

    async def test_empty_db_is_noop(self, tmp_path: Path) -> None:
        original_resolver = ps_mod._resolve_db_path
        ps_mod._resolve_db_path = lambda *_, **__: tmp_path / "state.db"
        try:
            count = await rehydrate_orphan_processes(project_dir=tmp_path)
        finally:
            ps_mod._resolve_db_path = original_resolver

        assert count == 0


# ── OrphanRehydrator (typed-result direct entry point) ───────


class TestOrphanRehydratorRun:
    """The typed :meth:`OrphanRehydrator.run` entry point returns
    a :class:`RehydrateResult` with populated ``surfaced`` /
    ``pruned`` / ``reason`` fields. The three failure branches
    (store init, ``list_all``, per-row ``remove``) each surface a
    distinct ``reason`` so :class:`RehydrateController` can plumb
    it through instead of collapsing to a bare ``int``.
    """

    def setup_method(self) -> None:
        supervisor = supervisors.default()
        supervisor.registry.clear()
        supervisor.registry.attach_persistence(None)

    async def test_run_returns_typed_result_with_surfaced_and_pruned_counts(
        self, tmp_path: Path
    ) -> None:
        # Two rows: one alive (our own pid), one dead — the pass
        # should surface the alive one and prune the dead one, and
        # report BOTH counts in the typed result.
        dead_pid = 0x7FFFFFFE
        store = BackgroundProcessStore(db_path=tmp_path / "state.db")
        await store.upsert(
            BackgroundProcessRow(
                pid=os.getpid(),
                cmd="alive one",
                pgid=os.getpid(),
                started_at=int(time.time()),
            )
        )
        await store.upsert(
            BackgroundProcessRow(pid=dead_pid, cmd="ghost", pgid=dead_pid, started_at=0)
        )

        rehydrator = OrphanRehydrator(supervisors.default(), store)
        result = await rehydrator.run()

        assert isinstance(result, RehydrateResult)
        assert result.ok is True
        assert result.surfaced == 1
        assert result.pruned == 1
        assert result.reason == ""

    async def test_run_reports_reason_when_list_all_fails(self, tmp_path: Path) -> None:
        # A store whose ``list_all`` raises should NOT crash the
        # pass — the failure surfaces via the typed reason instead.
        class _FailingStore:
            async def list_all(self) -> list[BackgroundProcessRow]:
                raise RuntimeError("db locked")

            async def remove(self, pid: int) -> None:  # pragma: no cover
                return None

            async def upsert(self, row: BackgroundProcessRow) -> None:  # pragma: no cover
                return None

        rehydrator = OrphanRehydrator(
            supervisors.default(),
            _FailingStore(),  # type: ignore[arg-type]
        )
        result = await rehydrator.run()

        assert result.ok is False
        assert result.surfaced == 0
        assert "list_all" in result.reason
        assert "db locked" in result.reason

    async def test_run_reports_reason_when_remove_fails(self, tmp_path: Path) -> None:
        # A dead row whose ``remove`` raises should mark the
        # result as ``ok=False`` with the pid encoded in the
        # reason, without swallowing at DEBUG.
        dead_pid = 0x7FFFFFFE
        real_store = BackgroundProcessStore(db_path=tmp_path / "state.db")
        await real_store.upsert(
            BackgroundProcessRow(pid=dead_pid, cmd="ghost", pgid=dead_pid, started_at=0)
        )

        class _FailingRemoveStore:
            def __init__(self, inner: BackgroundProcessStore) -> None:
                self._inner = inner

            async def list_all(self) -> list[BackgroundProcessRow]:
                return await self._inner.list_all()

            async def remove(self, pid: int) -> None:
                raise RuntimeError("cannot delete")

            async def upsert(self, row: BackgroundProcessRow) -> None:  # pragma: no cover
                return None

        rehydrator = OrphanRehydrator(
            supervisors.default(),
            _FailingRemoveStore(real_store),  # type: ignore[arg-type]
        )
        result = await rehydrator.run()

        assert result.ok is False
        assert result.surfaced == 0
        assert result.pruned == 0
        assert f"pid={dead_pid}" in result.reason
        assert "cannot delete" in result.reason

    async def test_build_rehydrator_reports_reason_on_store_init_failure(
        self, tmp_path: Path
    ) -> None:
        # If the store constructor itself raises, ``build_rehydrator``
        # returns ``(None, RehydrateResult(ok=False, reason=...))`` so
        # the caller can surface the store-init failure instead of
        # silently falling back to "return 0".
        original_resolver = ps_mod._resolve_db_path

        def _broken_resolver(*_: object, **__: object) -> Path:
            raise RuntimeError("resolver broke")

        ps_mod._resolve_db_path = _broken_resolver  # type: ignore[assignment]
        try:
            rehydrator, failure = build_rehydrator(supervisors.default(), tmp_path)
        finally:
            ps_mod._resolve_db_path = original_resolver

        assert rehydrator is None
        assert failure is not None
        assert failure.ok is False
        assert "store_init" in failure.reason


# ── BackendServer.stop_background_process for orphans ────────


class TestStopOrphanProcess:
    def setup_method(self) -> None:
        supervisors.default().registry.clear()

    async def test_killing_orphan_drops_registry_and_db_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Use a doubly-stubbed orphan so we don't actually kill
        # anything. Track that ``kill`` was called and that
        # cleanup landed.
        supervisor = supervisors.default()
        store = BackgroundProcessStore(db_path=tmp_path / "state.db")
        await store.upsert(BackgroundProcessRow(pid=12345, cmd="fake", pgid=12345, started_at=0))
        supervisor.registry.attach_persistence(store)

        kill_calls: list[int] = []

        class _Fake(OrphanProcess):
            def is_running(self) -> bool:  # type: ignore[override]
                # Behaves alive until kill is called.
                return self.pid not in kill_calls

            def kill(self) -> None:  # type: ignore[override]
                kill_calls.append(self.pid)

        fake = _Fake(pid=12345, cmd="fake", started_epoch=int(time.time()), pgid=12345)
        supervisor.registry.add(fake)  # type: ignore[arg-type]

        server = BackendServer.__new__(BackendServer)
        result = await server.stop_background_process(pid=12345)

        assert result["killed"] is True
        assert kill_calls == [12345]
        # Orphan path explicitly removed the registry row.
        assert supervisor.registry.get(12345) is None
        # And scheduled the DB delete. The fire-and-forget task
        # runs on the loop; give it a tick to flush.
        await asyncio.sleep(0.05)
        rows = await store.list_all()
        assert rows == []

        # Clean up supervisor state for test isolation.
        supervisor.registry.attach_persistence(None)


# ── Persist add/remove fire-and-forget guards ────────────────


class TestPersistGuards:
    """The persistence hooks in the registry's hot path must NOT
    raise when no store is wired (test/headless contexts) or
    when there's no running loop. These guards exist so the
    shell tool stays usable in the standalone pytest harness."""

    def test_persist_add_noop_when_store_unset(self) -> None:
        registry = supervisors.default().registry
        registry.attach_persistence(None)
        # No exception even though there's no store + no loop.
        registry._persist_add(12345, "x")

    def test_persist_remove_noop_when_store_unset(self) -> None:
        registry = supervisors.default().registry
        registry.attach_persistence(None)
        registry._persist_remove(12345)


# Silence unused-import warnings — kept so the test runner can
# resolve module attributes on test discovery.
_ = signal
_ = sys
