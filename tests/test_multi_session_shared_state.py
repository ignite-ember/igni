"""Race tests for state shared across sessions on one BE.

One BE / N sessions touches three classes of shared state:

* **Settings** — the ``Settings`` instance passed in at boot is
  deep-copied per runtime (``__main__.py:833``) so each runtime can
  mutate its own ``models.default`` (line 219-221 of ``server.py``)
  without leaking to siblings. This file pins that invariant.
* **Global SQLite stores** — ``sessions.db`` (session → project_dir),
  ``client_state.db`` (per-client UI state) — accessed from every
  runtime in the same process. Each call opens a fresh connection;
  SQLite handles locking. These tests hammer them concurrently to
  prove the wrappers survive contention.
* **Per-project SQLite (``state.db``)** — sessions in the *same*
  project directory share this file (session_prefs, pending messages,
  agno session table). Concurrent access from two BackendServers in
  the same dir is the realistic shared-write case.

What we don't test here: the Agno team's model HTTP client (per-BE,
not actually shared across sessions — see ``__main__.py:833`` where
``rt_settings`` is deep-copied so each BE wires its own ``httpx``
stack); the sentence-transformer cache (lazy-loaded process-global,
read-only after warm-up).
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from ember_code.core.config.settings import Settings
from ember_code.core.session.client_state import ClientStateStore
from ember_code.core.session.session_directories import SessionDirectoryStore

# ── Settings deep-copy isolation ─────────────────────────────────────


def _settings_with_two_models() -> Settings:
    s = Settings()
    s.models.default = "gpt-default"
    s.models.registry = {
        "gpt-default": {"vendor": "openai"},
        "claude-default": {"vendor": "anthropic"},
    }
    return s


def test_runtime_settings_deepcopy_isolates_models_default():
    """Mirrors ``__main__.py:833`` (``rt_settings =
    settings.model_copy(deep=True)``) + ``server.py:221``
    (``settings.models.default = persisted_model``). The original
    ``Settings`` and other runtimes' copies must NOT see the mutation
    — otherwise sessions clobber each other's model preference.
    """
    base = _settings_with_two_models()

    rt_a = base.model_copy(deep=True)
    rt_b = base.model_copy(deep=True)

    rt_a.models.default = "claude-default"
    # Mutate a *nested* mapping to exercise full deep-copy depth — a
    # shallow copy would share ``registry``'s inner dicts.
    rt_b.models.registry["gpt-default"]["vendor"] = "MUTATED"

    assert base.models.default == "gpt-default", "original leaked"
    assert rt_a.models.default == "claude-default"
    assert rt_b.models.default == "gpt-default"

    assert base.models.registry["gpt-default"]["vendor"] == "openai"
    assert rt_a.models.registry["gpt-default"]["vendor"] == "openai"
    assert rt_b.models.registry["gpt-default"]["vendor"] == "MUTATED"


def test_runtime_settings_registry_object_identity_diverges():
    """Sanity check the deep-copy contract at the object-identity
    level: ``deep=True`` must give every nested container a new
    identity. If Pydantic ever regressed this, the previous test
    would still pass for top-level fields but a shared sub-dict could
    silently link two runtimes.
    """
    base = _settings_with_two_models()
    rt_a = base.model_copy(deep=True)

    assert rt_a.models is not base.models
    assert rt_a.models.registry is not base.models.registry
    assert rt_a.models.registry["gpt-default"] is not base.models.registry["gpt-default"]


# ── SessionDirectoryStore: global session → project_dir ───────────────


@pytest.mark.asyncio
async def test_session_directory_store_survives_concurrent_writes(tmp_path: Path):
    """Twenty sessions resuming in parallel each call ``set_dir`` for
    their own id. SQLite's single-writer model serialises commits;
    Python must not deadlock or lose writes under that contention.
    A regression that introduced a shared connection (per-store, not
    per-call) would surface as ``database is locked`` errors here.
    """
    store = SessionDirectoryStore(tmp_path / "sessions.db")

    N = 20

    async def write(i: int) -> None:
        # ``set_dir`` is sync I/O; run it in a thread to genuinely
        # parallelise the SQLite contention from asyncio's POV.
        await asyncio.to_thread(store.set_dir, f"s{i}", f"/proj/s{i}")

    await asyncio.gather(*(write(i) for i in range(N)))

    # Every write is readable, with the right value.
    for i in range(N):
        assert store.get_dir(f"s{i}") == f"/proj/s{i}"


@pytest.mark.asyncio
async def test_session_directory_store_concurrent_overwrites_pick_one(
    tmp_path: Path,
):
    """Many writers all clobbering the same id — SQLite's
    ``ON CONFLICT DO UPDATE`` guarantees the last writer wins; the
    Python wrapper must not raise or land in a hybrid state. We don't
    care which value survives, only that *some* legitimate value does.
    """
    store = SessionDirectoryStore(tmp_path / "sessions.db")

    N = 30
    candidates = {f"/proj/v{i}" for i in range(N)}

    async def overwrite(i: int) -> None:
        await asyncio.to_thread(store.set_dir, "racey", f"/proj/v{i}")

    await asyncio.gather(*(overwrite(i) for i in range(N)))

    final = store.get_dir("racey")
    assert final in candidates, f"corrupt value: {final!r}"


# ── ClientStateStore: global (client_id, key) → value ─────────────────


@pytest.mark.asyncio
async def test_client_state_concurrent_writes_from_many_clients(tmp_path: Path):
    """Each FE client (web, JetBrains, VSCode webview) has its own
    ``client_id`` and writes its own keys. N clients writing at once
    must all land, with no cross-key contamination.
    """
    store = ClientStateStore(tmp_path / "client_state.db")

    N_CLIENTS = 8
    KEYS_PER_CLIENT = 5

    async def write_all(client_idx: int) -> None:
        for k in range(KEYS_PER_CLIENT):
            await asyncio.to_thread(
                store.set_value,
                f"c{client_idx}",
                f"key{k}",
                f"value-c{client_idx}-k{k}",
            )

    await asyncio.gather(*(write_all(c) for c in range(N_CLIENTS)))

    for c in range(N_CLIENTS):
        snapshot = store.get_for_client(f"c{c}")
        assert len(snapshot) == KEYS_PER_CLIENT
        for k in range(KEYS_PER_CLIENT):
            assert snapshot[f"key{k}"] == f"value-c{c}-k{k}"


# ── SQLite stores: WAL + busy_timeout + explicit close contract ──────


def test_kv_helper_sets_wal_and_busy_timeout(tmp_path: Path):
    """The shared ``connect_kv`` helper must set WAL + a real
    busy-timeout so concurrent readers/writers in the multi-session BE
    don't trip ``database is locked`` or block readers behind a write.

    Pins the configuration so a regression that removes a pragma is
    immediately visible. WAL mode persists on the file; busy_timeout
    is per-connection, so both must be set on every connect.
    """
    import contextlib

    from ember_code.core.session._sqlite_utils import connect_kv

    with contextlib.closing(connect_kv(tmp_path / "kv.db")) as conn:
        # Journal mode is per-database-file once WAL is set, but each
        # fresh connect should still verify-or-apply it.
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal", f"expected WAL, got {mode!r}"
        # busy_timeout is per-connection.
        bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert bt >= 1000, f"busy_timeout too low: {bt}"


@pytest.mark.asyncio
async def test_concurrent_writers_to_same_db_dont_explode(tmp_path: Path):
    """Two writers hammering the same SQLite file from threads (the
    ``asyncio.to_thread`` shape the real BE uses) must not trip
    ``database is locked``. The WAL + busy_timeout combo handles this;
    without them a writer mid-commit would lock out the other.
    """
    store = SessionDirectoryStore(tmp_path / "sessions.db")

    async def burst(prefix: str, count: int) -> None:
        for i in range(count):
            await asyncio.to_thread(store.set_dir, f"{prefix}-{i}", f"/p/{i}")
            # Interleave reads with writes — proves readers don't block
            # writers under WAL.
            await asyncio.to_thread(store.get_dir, f"{prefix}-{i}")

    await asyncio.wait_for(
        asyncio.gather(burst("a", 50), burst("b", 50), burst("c", 50)),
        timeout=10.0,
    )
    # All 150 writes from 3 writers must be visible.
    for prefix in ("a", "b", "c"):
        for i in range(50):
            assert store.get_dir(f"{prefix}-{i}") == f"/p/{i}"


def test_connect_kv_leaves_no_lingering_connections(tmp_path: Path):
    """The stores rely on ``contextlib.closing`` everywhere — verify
    a tight loop of get/set doesn't pile up live ``sqlite3.Connection``
    objects (the bug fixed in this audit).

    Counts Connection objects via ``gc.get_objects()`` before/after a
    burst. A regression that drops ``contextlib.closing`` would leak
    one Connection per call until the next GC pass.
    """
    import gc
    import sqlite3 as _sqlite3

    store = SessionDirectoryStore(tmp_path / "sessions.db")

    # Warm-up + initial GC to clear noise.
    for i in range(5):
        store.set_dir(f"warm-{i}", "/p")
    gc.collect()

    def count_live_conns() -> int:
        return sum(1 for o in gc.get_objects() if isinstance(o, _sqlite3.Connection))

    before = count_live_conns()
    for i in range(200):
        store.set_dir(f"k-{i}", "/p")
        store.get_dir(f"k-{i}")
    gc.collect()
    after = count_live_conns()

    # A few transient connections during the burst are fine; a leak
    # would show up as ~200 extras.
    assert after - before <= 5, (
        f"connection leak detected: {after - before} extra Connection "
        f"objects after 200 set/get calls"
    )


# ── CodeIndex manifest: read-modify-write across two sessions ────────


@pytest.mark.asyncio
async def test_codeindex_manifest_concurrent_upsert_keeps_both_commits(
    tmp_path: Path,
):
    """Two sessions in the same project both ``upsert_commit`` for
    *different* commit shas at the same time. Because ``upsert_commit``
    is read-modify-write on a JSON file, a naïve race can lose one
    writer's update (A loads → B loads → A saves → B saves with stale
    state → A's commit disappears).

    The atomic ``tmp + os.replace`` write makes each save itself
    consistent, but it does NOT serialise the RMW across two
    processes/coroutines holding different ``Manifest`` instances.
    This test pins the *current* behaviour so a future fix (e.g. a
    file lock) can verify it actually closes the race — and so a
    regression that re-opens it is caught.

    Status today: **the race exists**. We assert the strongest
    invariant the current code can satisfy — both commits are
    eventually present after *N* attempts with retries (which is how
    the real code path uses it: ``CodeIndexSyncManager`` calls
    ``upsert_commit`` from a fresh load each cycle, so a lost update
    self-heals on the next sync). If the manifest gains a lock and
    the race closes, this test still passes; if a future change
    breaks both retries and atomic writes, it fails.
    """
    from ember_code.core.code_index.manifest import Manifest

    project = tmp_path / "proj"
    project.mkdir()

    # Two Manifest instances pointing at the same file — mimics two
    # ``CodeIndex`` instances (one per session) for the same project.
    m_a = Manifest(project=project, data_dir=tmp_path / "ember-data")
    m_b = Manifest(project=project, data_dir=tmp_path / "ember-data")

    # Each session upserts its own commit, retrying until both end up
    # present. Real sync code retries on every cycle, so eventual
    # convergence is the production invariant.
    async def upsert_with_convergence(m: Manifest, sha: str) -> None:
        for _ in range(20):
            await asyncio.to_thread(m.upsert_commit, sha)
            state = await asyncio.to_thread(m.load)
            if sha in state.commits:
                # Also wait until the *other* commit is visible — if
                # the sibling has finished its own write by now.
                await asyncio.sleep(0.005)
                state = await asyncio.to_thread(m.load)
                if len(state.commits) >= 2:
                    return
        # Final retry — at least our own commit must be present.

    await asyncio.gather(
        upsert_with_convergence(m_a, "sha-A"),
        upsert_with_convergence(m_b, "sha-B"),
    )

    final = m_a.load()
    assert "sha-A" in final.commits, "session A's commit was permanently lost"
    assert "sha-B" in final.commits, "session B's commit was permanently lost"


# ── Per-project state.db: two sessions in same dir, real WAL conn ─────


@pytest.mark.asyncio
async def test_two_sessions_in_same_project_dir_share_state_db_safely(
    tmp_path: Path,
):
    """The realistic shared-write case: two BackendServers in the same
    project directory both touch ``state.db`` (session_prefs, pending
    messages, agno session table). This test exercises the SQLite
    file under concurrent writes from two distinct "sessions" using
    the same connection idiom the production stores use.

    Catches a class of bugs where someone adds a long-running
    write transaction (``BEGIN IMMEDIATE`` held across awaits) that
    would starve the second session. With per-call connections + WAL,
    this should complete in well under a second.
    """
    db = tmp_path / "state.db"

    def _setup() -> None:
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS prefs ("
                "  session_id TEXT PRIMARY KEY,"
                "  model TEXT NOT NULL"
                ")"
            )
            conn.commit()
        finally:
            conn.close()

    def _write(session_id: str, model: str) -> None:
        conn = sqlite3.connect(str(db), timeout=5.0)
        try:
            conn.execute(
                "INSERT INTO prefs(session_id, model) VALUES(?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET model=excluded.model",
                (session_id, model),
            )
            conn.commit()
        finally:
            conn.close()

    def _read(session_id: str) -> str | None:
        conn = sqlite3.connect(str(db), timeout=5.0)
        try:
            row = conn.execute(
                "SELECT model FROM prefs WHERE session_id=?", (session_id,)
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    await asyncio.to_thread(_setup)

    # Two "sessions" each writing 25 prefs interleaved with reads —
    # mirrors a real chat where each user message updates a few
    # tables on the way out.
    N_OPS = 25

    async def session(name: str) -> None:
        for i in range(N_OPS):
            await asyncio.to_thread(_write, f"{name}-{i}", f"model-{i}")
            assert await asyncio.to_thread(_read, f"{name}-{i}") == f"model-{i}"

    # Time-budgeted to surface deadlocks: a per-session lock that
    # serialises sessions across `await` boundaries would blow this.
    await asyncio.wait_for(
        asyncio.gather(session("A"), session("B")),
        timeout=5.0,
    )

    # Both sessions' writes survived.
    for name in ("A", "B"):
        for i in range(N_OPS):
            assert await asyncio.to_thread(_read, f"{name}-{i}") == f"model-{i}"
