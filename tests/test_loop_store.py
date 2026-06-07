"""Tests for :class:`LoopStore` and :class:`LoopProgressStore`.

Each test gets its own SQLite tmp file (mirroring ``test_scheduler``),
so the in-DB state is isolated and the singleton-row invariant on
``loop_state`` can be exercised end-to-end.
"""

from __future__ import annotations

import pytest

from ember_code.core.loop.models import LoopState
from ember_code.core.loop.store import LoopProgressStore, LoopStore

# ── LoopStore ──────────────────────────────────────────────────────


class TestLoopStore:
    @pytest.fixture
    def store(self, tmp_path):
        return LoopStore(db_path=tmp_path / "state.db")

    @pytest.mark.asyncio
    async def test_load_empty(self, store):
        """Fresh DB → no row → ``load`` returns ``None`` (not a
        default ``LoopState`` — None is the unambiguous "no loop"
        signal)."""
        assert await store.load() is None

    @pytest.mark.asyncio
    async def test_save_then_load_roundtrip(self, store):
        await store.save(
            LoopState(
                run_id="run-1",
                prompt="check each section",
                iteration_index=3,
                iterations_remaining=7,
            )
        )
        loaded = await store.load()
        assert loaded is not None
        assert loaded.run_id == "run-1"
        assert loaded.prompt == "check each section"
        assert loaded.iteration_index == 3
        assert loaded.iterations_remaining == 7

    @pytest.mark.asyncio
    async def test_save_twice_updates_in_place(self, store):
        """Second ``save`` targets the same row (singleton). No
        duplicate-row growth, ``updated_at`` ticks forward, and
        the loaded state reflects the second write."""
        await store.save(
            LoopState(run_id="run-1", prompt="p", iteration_index=1, iterations_remaining=9)
        )
        await store.save(
            LoopState(run_id="run-1", prompt="p", iteration_index=2, iterations_remaining=8)
        )
        loaded = await store.load()
        assert loaded.iteration_index == 2
        assert loaded.iterations_remaining == 8

    @pytest.mark.asyncio
    async def test_save_with_different_run_id_replaces_row(self, store):
        """Starting a new loop run mid-life (rare but possible)
        replaces the row — singleton invariant is upheld."""
        await store.save(
            LoopState(run_id="run-1", prompt="p", iteration_index=1, iterations_remaining=5)
        )
        await store.save(
            LoopState(run_id="run-2", prompt="p2", iteration_index=1, iterations_remaining=9)
        )
        loaded = await store.load()
        assert loaded.run_id == "run-2"
        assert loaded.prompt == "p2"

    @pytest.mark.asyncio
    async def test_clear_removes_row(self, store):
        await store.save(
            LoopState(run_id="run-1", prompt="p", iteration_index=1, iterations_remaining=5)
        )
        ok = await store.clear()
        assert ok is True
        assert await store.load() is None

    @pytest.mark.asyncio
    async def test_clear_when_empty_returns_false(self, store):
        """Calling ``clear`` on an empty table returns False rather
        than raising — the cancel_loop helper relies on this to
        decide whether to surface a "loop cancelled" message."""
        assert await store.clear() is False

    @pytest.mark.asyncio
    async def test_persistence_across_store_instances(self, tmp_path):
        """The whole point of the store: a fresh instance backed
        by the same file sees what the previous instance wrote.
        Simulates a CLI restart."""
        db_path = tmp_path / "state.db"
        first = LoopStore(db_path=db_path)
        await first.save(
            LoopState(
                run_id="survives",
                prompt="resume me",
                iteration_index=4,
                iterations_remaining=6,
            )
        )

        second = LoopStore(db_path=db_path)
        loaded = await second.load()
        assert loaded is not None
        assert loaded.run_id == "survives"
        assert loaded.iteration_index == 4


# ── LoopProgressStore ─────────────────────────────────────────────


class TestLoopProgressStore:
    @pytest.fixture
    def store(self, tmp_path):
        return LoopProgressStore(db_path=tmp_path / "state.db")

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self, store):
        assert await store.get("run-1", "any") is None

    @pytest.mark.asyncio
    async def test_set_then_get(self, store):
        await store.set("run-1", "section_1", "verified ok")
        assert await store.get("run-1", "section_1") == "verified ok"

    @pytest.mark.asyncio
    async def test_set_twice_updates_in_place(self, store):
        """The model uses ``set`` to append notes across iterations
        — calling it twice on the same key must replace, not throw
        on the unique constraint."""
        await store.set("run-1", "k", "v1")
        await store.set("run-1", "k", "v2")
        assert await store.get("run-1", "k") == "v2"

    @pytest.mark.asyncio
    async def test_list_orders_by_creation(self, store):
        await store.set("run-1", "a", "first")
        await store.set("run-1", "b", "second")
        await store.set("run-1", "c", "third")
        rows = await store.list("run-1")
        assert [k for k, _ in rows] == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_list_scopes_by_run_id(self, store):
        """The whole point of ``run_id``: progress from a previous
        run must NOT leak into the current run. Without scoping,
        iteration 1 of a fresh loop would see stale rows from a
        long-since-completed loop."""
        await store.set("old-run", "section_1", "from old loop")
        await store.set("new-run", "section_1", "from new loop")
        await store.set("new-run", "section_2", "new only")

        new_rows = dict(await store.list("new-run"))
        assert new_rows == {"section_1": "from new loop", "section_2": "new only"}
        # Old rows are still there (clear is explicit), just not
        # surfaced through the new-run scope.
        old_rows = dict(await store.list("old-run"))
        assert old_rows == {"section_1": "from old loop"}

    @pytest.mark.asyncio
    async def test_delete_returns_true_when_existed(self, store):
        await store.set("run-1", "k", "v")
        assert await store.delete("run-1", "k") is True
        assert await store.get("run-1", "k") is None

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_missing(self, store):
        assert await store.delete("run-1", "nope") is False

    @pytest.mark.asyncio
    async def test_clear_returns_deleted_count(self, store):
        await store.set("run-1", "a", "v")
        await store.set("run-1", "b", "v")
        await store.set("run-1", "c", "v")
        # Different run — must survive the clear.
        await store.set("run-2", "x", "v")

        n = await store.clear("run-1")
        assert n == 3
        assert await store.list("run-1") == []
        # run-2 untouched.
        assert await store.list("run-2") == [("x", "v")]

    @pytest.mark.asyncio
    async def test_clear_empty_returns_zero(self, store):
        assert await store.clear("never-existed") == 0
