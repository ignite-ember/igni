"""Todo execution state must survive BE restart.

Same architectural smell as the plan-decisions bug: before this
fix, ``todo_write`` only mutated the in-memory ``TodoStore`` and
broadcast ``todos_updated``. The status flips (pending →
in_progress → completed) reached the FE for the live session
but were lost on restart — ``_rehydrate_plan_store`` re-seeded
from the plan's original ``exit_plan_mode(tasks=...)`` args
(everything pending), erasing all execution progress.

The fix persists the live snapshot to ``session_data["todos"]``
on every ``todo_write`` and prefers that on rehydration. These
tests pin the contract end to end:

* ``TodoTools.todo_write`` calls ``SessionPersistence.save_todos``
  with the post-set snapshot.
* ``_rehydrate_todos`` overwrites the plan-args seed when a
  snapshot exists.
* Headline regression: agent flips task A → in_progress, BE
  restarts, FE sees in_progress (NOT pending) on the restored
  card.
"""

from __future__ import annotations

from types import SimpleNamespace

from ember_code.backend.server import BackendServer
from ember_code.core.tools.todo import TodoStore, TodoTools


class _CapturingPersistence:
    """Captures save_todos / load_todos without touching disk."""

    def __init__(self, prefill: list[dict] | None = None) -> None:
        self.saved: list[list[dict]] = []
        self._prefill = list(prefill or [])
        self.fail_save = False
        self.fail_load = False

    async def save_todos(self, todos: list[dict]) -> None:
        if self.fail_save:
            raise RuntimeError("simulated DB outage")
        # Snapshot each call so the test can see history.
        self.saved.append([dict(t) for t in todos])

    async def load_todos(self) -> list[dict]:
        if self.fail_load:
            raise RuntimeError("simulated DB outage")
        return list(self._prefill)


def _build_session_with_todo_tools() -> tuple[object, TodoTools, _CapturingPersistence]:
    """Construct a session with just enough surface for the
    TodoTools toolkit to call ``broadcast`` + ``persistence``."""
    session = SimpleNamespace(
        todo_store=TodoStore(),
        persistence=_CapturingPersistence(),
        broadcast=lambda channel, payload: None,
    )
    tools = TodoTools.__new__(TodoTools)
    tools._session = session  # type: ignore[attr-defined]
    return session, tools, session.persistence


class TestTodoWritePersists:
    async def test_set_writes_snapshot_to_persistence(self) -> None:
        # Headline contract: every successful ``todo_write`` flushes
        # the current snapshot to the persistence layer. Without
        # this, restart wipes execution state.
        session, tools, persistence = _build_session_with_todo_tools()
        await tools.todo_write(
            [
                {"content": "Task A", "status": "pending"},
                {"content": "Task B", "status": "in_progress"},
            ]
        )
        assert persistence.saved == [
            [
                {"content": "Task A", "status": "pending", "activeForm": ""},
                {"content": "Task B", "status": "in_progress", "activeForm": ""},
            ]
        ]

    async def test_clear_persists_empty_list(self) -> None:
        # Clearing the list is a legitimate state change — must
        # persist so the next restart doesn't resurrect the
        # cleared items from the plan's original args.
        session, tools, persistence = _build_session_with_todo_tools()
        await tools.todo_write([{"content": "Task A", "status": "pending"}])
        persistence.saved.clear()
        await tools.todo_write([])
        assert persistence.saved == [[]]

    async def test_persist_failure_does_NOT_block_in_memory_update(self) -> None:
        # DB blip mid-execution shouldn't break the live FE
        # state — the user keeps seeing checkboxes flip. The
        # only cost is restart-recovery, which we accept since
        # the next ``todo_write`` will rewrite the snapshot.
        session, tools, persistence = _build_session_with_todo_tools()
        persistence.fail_save = True
        reply = await tools.todo_write([{"content": "Task A", "status": "completed"}])
        # In-memory state is correct.
        assert session.todo_store.snapshot() == [
            {"content": "Task A", "status": "completed", "activeForm": ""}
        ]
        # Reply still confirms (no exception propagated).
        assert "1 todos" in reply

    async def test_each_todo_write_overwrites_previous_snapshot(self) -> None:
        # Atomic-replace semantic: persisted blob always reflects
        # the LAST write. No accidental merge across calls.
        session, tools, persistence = _build_session_with_todo_tools()
        await tools.todo_write([{"content": "A", "status": "pending"}])
        await tools.todo_write(
            [
                {"content": "A", "status": "in_progress"},
                {"content": "B", "status": "pending"},
            ]
        )
        # Last persisted call has both items, A flipped.
        assert persistence.saved[-1] == [
            {"content": "A", "status": "in_progress", "activeForm": ""},
            {"content": "B", "status": "pending", "activeForm": ""},
        ]


# ── _rehydrate_todos ─────────────────────────────────────────


class TestRehydrateTodos:
    async def test_snapshot_overwrites_plan_args_seeding(self) -> None:
        # The headline regression. The plan-args path seeded
        # the store with two pending tasks (the original plan
        # state). A snapshot with task A flipped to in_progress
        # is the real execution state — rehydration must prefer
        # it.
        server = BackendServer.__new__(BackendServer)
        store = TodoStore()
        # Pre-seed as if ``_rehydrate_plan_store`` already ran.
        from ember_code.core.tools.todo import _coerce_items

        seed_items, _ = _coerce_items(
            [
                {"content": "Task A", "status": "pending"},
                {"content": "Task B", "status": "pending"},
            ]
        )
        store.set(seed_items)

        server._session = SimpleNamespace(
            todo_store=store,
            persistence=_CapturingPersistence(
                prefill=[
                    {"content": "Task A", "status": "in_progress", "activeForm": ""},
                    {"content": "Task B", "status": "pending", "activeForm": ""},
                ]
            ),
        )

        await server._rehydrate_todos()

        assert store.snapshot() == [
            {"content": "Task A", "status": "in_progress", "activeForm": ""},
            {"content": "Task B", "status": "pending", "activeForm": ""},
        ], (
            "Bug regression: BE restart erased execution progress. "
            "The snapshot in session_data is the authoritative state "
            "once todo_write has fired; rehydration must prefer it "
            "over the plan's original args."
        )

    async def test_empty_snapshot_preserves_plan_args_seed(self) -> None:
        # No execution yet → no snapshot → keep whatever
        # _rehydrate_plan_store seeded. Otherwise a fresh plan
        # submission with tasks would render with an empty
        # checklist.
        server = BackendServer.__new__(BackendServer)
        store = TodoStore()
        from ember_code.core.tools.todo import _coerce_items

        seed_items, _ = _coerce_items([{"content": "Task A", "status": "pending"}])
        store.set(seed_items)

        server._session = SimpleNamespace(
            todo_store=store,
            persistence=_CapturingPersistence(prefill=[]),
        )

        await server._rehydrate_todos()

        assert store.snapshot() == [{"content": "Task A", "status": "pending", "activeForm": ""}]

    async def test_load_failure_preserves_seed(self) -> None:
        # DB blip on restart → fall back to the plan-args seed
        # rather than crash the boot.
        server = BackendServer.__new__(BackendServer)
        store = TodoStore()
        from ember_code.core.tools.todo import _coerce_items

        seed_items, _ = _coerce_items([{"content": "Task A", "status": "pending"}])
        store.set(seed_items)

        persistence = _CapturingPersistence(
            prefill=[{"content": "X", "status": "completed", "activeForm": ""}]
        )
        persistence.fail_load = True
        server._session = SimpleNamespace(todo_store=store, persistence=persistence)

        await server._rehydrate_todos()  # must not raise

        # Original seed intact — no partial overwrite.
        assert store.snapshot() == [{"content": "Task A", "status": "pending", "activeForm": ""}]

    async def test_missing_persistence_is_noop(self) -> None:
        # Test sessions / headless callers may build a session
        # without a persistence layer. Rehydrate must skip
        # cleanly, leaving any prior seed in place.
        server = BackendServer.__new__(BackendServer)
        store = TodoStore()
        server._session = SimpleNamespace(todo_store=store, persistence=None)

        await server._rehydrate_todos()
        assert store.snapshot() == []


# ── End-to-end: live flip → restart → restored state ─────────


class TestEndToEndRestartSurvives:
    """The user's actual scenario, stitched together from the
    in-memory pieces. Models what happens when:

      1. Agent submits a plan with tasks A, B (both pending).
      2. Agent flips A → in_progress via ``todo_write``.
      3. BE restarts.
      4. FE reconnects; the restored card must show A as
         in_progress, NOT back to pending.
    """

    async def test_in_progress_survives_restart(self) -> None:
        # Step 1+2: live session, agent writes the live state.
        live_session, live_tools, live_persistence = _build_session_with_todo_tools()
        await live_tools.todo_write(
            [
                {"content": "Task A", "status": "in_progress"},
                {"content": "Task B", "status": "pending"},
            ]
        )
        assert live_persistence.saved, "persistence layer never saw the write"
        persisted_blob = live_persistence.saved[-1]

        # Step 3: simulate restart. Fresh session, fresh store.
        # ``_rehydrate_plan_store`` would normally re-seed from
        # the plan's original args (everything pending) — we
        # simulate that step by pre-seeding the store the same
        # way.
        from ember_code.core.tools.todo import _coerce_items

        restarted_store = TodoStore()
        plan_args_seed, _ = _coerce_items(
            [
                {"content": "Task A", "status": "pending"},
                {"content": "Task B", "status": "pending"},
            ]
        )
        restarted_store.set(plan_args_seed)

        # ``_rehydrate_todos`` then loads the persisted snapshot.
        restarted_persistence = _CapturingPersistence(prefill=persisted_blob)
        server = BackendServer.__new__(BackendServer)
        server._session = SimpleNamespace(
            todo_store=restarted_store,
            persistence=restarted_persistence,
        )
        await server._rehydrate_todos()

        # Step 4: restored state matches the live state at restart.
        assert restarted_store.snapshot() == [
            {"content": "Task A", "status": "in_progress", "activeForm": ""},
            {"content": "Task B", "status": "pending", "activeForm": ""},
        ], (
            "End-to-end regression: live in_progress flip didn't survive "
            "restart. The whole point of persisting on every todo_write "
            "is this scenario — without it the FE shows stale plan-args "
            "state and the user has to re-do whatever progress was made."
        )
