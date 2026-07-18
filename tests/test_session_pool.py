"""Tests for the BE session pool — per-view session routing."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.backend.session_pool import (
    SessionPool,
    SessionRuntime,
    SessionStampingTransport,
)
from ember_code.backend.session_stamping_transport import (
    SessionStampingTransport as SessionStampingTransportDirect,
)
from ember_code.protocol import messages as msg

# The re-export from session_pool must resolve to the same class as
# the direct import from session_stamping_transport — this keeps the
# one-release-cycle shim honest.
assert SessionStampingTransport is SessionStampingTransportDirect


def _runtime(session_id: str) -> SessionRuntime:
    backend = MagicMock()
    backend.session_id = session_id
    backend.shutdown = AsyncMock()
    return SessionRuntime(
        backend=backend,
        rpc_table={},
        queue=[],
        transport=MagicMock(),
    )


def _pool(default_id: str = "default-s", created: list[str] | None = None) -> SessionPool:
    sink = created if created is not None else []

    async def factory(session_id: str) -> SessionRuntime:
        sink.append(session_id)
        return _runtime(session_id)

    return SessionPool(_runtime(default_id), factory)


class TestRouting:
    @pytest.mark.asyncio
    async def test_empty_session_id_routes_to_default(self):
        """TUI behaviour: messages without a stamp hit the boot session."""
        pool = _pool("default-s")
        rt = await pool.get_or_create("")
        assert rt is pool.default

    @pytest.mark.asyncio
    async def test_default_session_own_id_routes_to_default(self):
        pool = _pool("default-s")
        rt = await pool.get_or_create("default-s")
        assert rt is pool.default

    @pytest.mark.asyncio
    async def test_unknown_id_creates_runtime(self):
        created: list[str] = []
        pool = _pool("default-s", created)
        rt = await pool.get_or_create("old-chat")
        assert created == ["old-chat"]
        assert rt is not pool.default
        assert rt.backend.session_id == "old-chat"

    @pytest.mark.asyncio
    async def test_second_message_reuses_runtime(self):
        created: list[str] = []
        pool = _pool("default-s", created)
        rt1 = await pool.get_or_create("old-chat")
        rt2 = await pool.get_or_create("old-chat")
        assert rt1 is rt2
        assert created == ["old-chat"]

    @pytest.mark.asyncio
    async def test_concurrent_creates_resolve_to_one_runtime(self):
        """Two messages racing for the same unloaded session must not
        resume it twice (double Session = double Agno team)."""
        created: list[str] = []
        pool = _pool("default-s", created)
        rt1, rt2 = await asyncio.gather(
            pool.get_or_create("old-chat"),
            pool.get_or_create("old-chat"),
        )
        assert rt1 is rt2
        assert created == ["old-chat"]

    @pytest.mark.asyncio
    async def test_id_rename_keeps_routing_to_same_runtime(self):
        """/clear renews a runtime's internal id; views still stamping
        the OLD id must not trigger a ghost resume of it."""
        created: list[str] = []
        pool = _pool("default-s", created)
        rt = await pool.get_or_create("old-chat")
        # Simulate /clear: internal id changes, alias retained.
        rt.register_id()
        rt.backend.session_id = "renewed-id"
        rt.register_id()

        stale = await pool.get_or_create("old-chat")
        fresh = await pool.get_or_create("renewed-id")
        assert stale is rt
        assert fresh is rt
        assert created == ["old-chat"]  # no ghost resume

    @pytest.mark.asyncio
    async def test_shutdown_closes_every_runtime(self):
        pool = _pool("default-s")
        rt = await pool.get_or_create("other")
        reports = await pool.shutdown()
        pool.default.backend.shutdown.assert_awaited_once()
        rt.backend.shutdown.assert_awaited_once()
        # Every runtime yields a typed ShutdownReport; both are OK
        # in this test since the mock ``shutdown`` doesn't raise.
        assert len(reports) == 2
        assert all(r.ok for r in reports)


class TestIdleEviction:
    @pytest.fixture
    def fake_clock(self):
        """Injectable monotonic clock — tests advance it explicitly so
        eviction logic can be exercised without real sleeps."""

        class _Clock:
            def __init__(self) -> None:
                self.t = 1000.0

            def __call__(self) -> float:
                return self.t

            def advance(self, seconds: float) -> None:
                self.t += seconds

        return _Clock()

    def _build_pool(self, clock, *, idle_timeout=60.0, default_id="default-s"):
        created: list[str] = []

        async def factory(session_id: str) -> SessionRuntime:
            created.append(session_id)
            backend = MagicMock()
            backend.session_id = session_id
            backend.processing = False
            backend.shutdown = AsyncMock()
            return SessionRuntime(
                backend=backend,
                rpc_table={},
                queue=[],
                transport=MagicMock(),
            )

        default = _runtime(default_id)
        default.backend.processing = False
        pool = SessionPool(
            default,
            factory,
            idle_timeout_seconds=idle_timeout,
            clock=clock,
        )
        return pool, created

    @pytest.mark.asyncio
    async def test_default_runtime_is_never_evicted(self, fake_clock):
        """The boot runtime serves empty-``session_id`` traffic — if it
        disappeared, the TUI's next message would silently spawn a new
        one, breaking session continuity."""
        pool, _ = self._build_pool(fake_clock, idle_timeout=10.0)
        fake_clock.advance(10_000)  # way past timeout
        report = await pool.evict_idle()
        assert report.evicted_ids == []
        assert pool.default is pool.runtimes[0]

    @pytest.mark.asyncio
    async def test_idle_runtime_evicted_and_backend_shut_down(self, fake_clock):
        pool, created = self._build_pool(fake_clock, idle_timeout=60.0)
        rt = await pool.get_or_create("alpha")
        # rt was just created → last_used_at = clock now. Advance past
        # the timeout.
        fake_clock.advance(61.0)
        report = await pool.evict_idle()
        assert report.evicted_ids == ["alpha"]
        rt.backend.shutdown.assert_awaited_once()
        # Pool no longer holds it.
        assert all(r is not rt for r in pool.runtimes)

    @pytest.mark.asyncio
    async def test_recently_used_runtime_is_kept(self, fake_clock):
        pool, _ = self._build_pool(fake_clock, idle_timeout=60.0)
        rt = await pool.get_or_create("alpha")
        fake_clock.advance(30.0)
        # A find() call updates last_used_at — keeps the runtime live.
        pool.find("alpha")
        fake_clock.advance(40.0)  # 70s since creation, but 40s since use
        report = await pool.evict_idle()
        assert report.evicted_ids == []
        assert rt in pool.runtimes

    @pytest.mark.asyncio
    async def test_processing_runtime_is_skipped(self, fake_clock):
        """Mid-stream runs must not be torn down by the evictor — the
        FE would see the stream die without a clean error. Wait for
        the next sweep instead."""
        pool, _ = self._build_pool(fake_clock, idle_timeout=60.0)
        rt = await pool.get_or_create("alpha")
        rt.backend.processing = True
        fake_clock.advance(120.0)
        report = await pool.evict_idle()
        assert report.evicted_ids == []
        assert rt in pool.runtimes
        rt.backend.shutdown.assert_not_called()

        # Once it finishes processing, the next sweep evicts it.
        rt.backend.processing = False
        report = await pool.evict_idle()
        assert report.evicted_ids == ["alpha"]

    @pytest.mark.asyncio
    async def test_evicted_session_respawns_via_factory_on_next_message(self, fake_clock):
        """Eviction clears in-memory state; the session is still on
        disk (Agno persists it). The next inbound message for that id
        must trigger a fresh resume, not silently route to the default.
        """
        pool, created = self._build_pool(fake_clock, idle_timeout=60.0)
        await pool.get_or_create("alpha")
        fake_clock.advance(61.0)
        await pool.evict_idle()
        # Fresh resume on next access.
        rt2 = await pool.get_or_create("alpha")
        assert rt2.backend.session_id == "alpha"
        assert created == ["alpha", "alpha"]  # factory invoked twice

    @pytest.mark.asyncio
    async def test_default_session_traffic_keeps_default_alive(self, fake_clock):
        """Empty-``session_id`` find() should refresh the default's
        ``last_used_at`` — otherwise a busy default runtime could look
        idle to the evictor purely because all its traffic was empty-
        stamped."""
        pool, _ = self._build_pool(fake_clock, idle_timeout=60.0)
        # Advance past the timeout, then issue empty-id traffic.
        fake_clock.advance(120.0)
        pool.find("")  # refreshes default
        # Default's last_used_at is now > cutoff for the next sweep.
        report = await pool.evict_idle()
        assert report.evicted_ids == []
        # Sanity: default is still the first runtime.
        assert pool.default is pool.runtimes[0]


class TestSessionStamping:
    @pytest.mark.asyncio
    async def test_outbound_events_get_runtime_session_id(self):
        inner = MagicMock()
        inner.send = AsyncMock()
        backend = MagicMock()
        backend.session_id = "sess-A"
        stamped = SessionStampingTransport(inner, backend)

        await stamped.send(msg.Info(text="hello"))

        sent = inner.send.call_args[0][0]
        assert sent.session_id == "sess-A"
        assert sent.text == "hello"

    @pytest.mark.asyncio
    async def test_existing_stamp_is_preserved(self):
        """Relays (e.g. Typing) already carry the sender's session —
        the wrapper must not overwrite it."""
        inner = MagicMock()
        inner.send = AsyncMock()
        backend = MagicMock()
        backend.session_id = "sess-A"
        stamped = SessionStampingTransport(inner, backend)

        await stamped.send(msg.Typing(text="draft", session_id="sess-B"))

        sent = inner.send.call_args[0][0]
        assert sent.session_id == "sess-B"

    def test_attribute_passthrough(self):
        inner = MagicMock()
        inner.port = 1234
        stamped = SessionStampingTransport(inner, MagicMock())
        assert stamped.port == 1234
