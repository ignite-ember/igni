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
from ember_code.protocol import messages as msg


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
        rt.remember_id()
        rt.backend.session_id = "renewed-id"
        rt.remember_id()

        stale = await pool.get_or_create("old-chat")
        fresh = await pool.get_or_create("renewed-id")
        assert stale is rt
        assert fresh is rt
        assert created == ["old-chat"]  # no ghost resume

    @pytest.mark.asyncio
    async def test_shutdown_closes_every_runtime(self):
        pool = _pool("default-s")
        rt = await pool.get_or_create("other")
        await pool.shutdown()
        pool.default.backend.shutdown.assert_awaited_once()
        rt.backend.shutdown.assert_awaited_once()


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
