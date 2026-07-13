"""Unit tests for ``session/startup_ops.py``.

Extracted in iter 145. The Session-method delegates are
covered end-to-end via Session's boot path; these tests pin
the free-function contracts in isolation — most importantly:

* Every background starter is a **no-op when no event loop is
  running** (``get_running_loop`` raises ``RuntimeError`` → early
  return). Session's caller retries once the loop is up.
* Failures inside the fire-and-forget tasks are logged and
  swallowed — session boot must not gate on offline external
  deps.
* `rebuild_mcp` composes `list_connected` + `_clients` correctly.
* `ensure_mcp` respects the once-per-session gate.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.core.session.startup_ops import (
    ensure_knowledge_started,
    ensure_mcp,
    rebuild_mcp,
    start_codeindex_background,
    start_knowledge_background,
    start_marketplace_refresh_background,
)


def _bare_session():
    """Session-shaped stub carrying only what startup_ops reads."""
    session = SimpleNamespace()
    session.knowledge = SimpleNamespace()
    session.knowledge.start = AsyncMock()
    session.code_index = SimpleNamespace()
    session.code_index.sweep_stale_dirs = MagicMock(return_value=[])
    session.code_index.clean = AsyncMock(return_value=[])
    session.code_index_sync = SimpleNamespace()
    session.code_index_sync.resolver = SimpleNamespace()
    session.code_index_sync.resolver.resolve = AsyncMock()
    session.code_index_sync.sync_now = AsyncMock()
    session.code_index_sync.start_watcher = AsyncMock()
    session.refresh_codeindex_availability = MagicMock()
    session._mcp_initialized = False
    session.mcp_manager = SimpleNamespace()
    session.mcp_manager.list_servers = MagicMock(return_value=[])
    session.mcp_manager.list_connected = MagicMock(return_value=[])
    session.mcp_manager._clients = {}
    session.mcp_manager.connect = AsyncMock(return_value=None)
    session.mcp_manager.get_error = MagicMock(return_value="")
    session.pool = SimpleNamespace(build_agents=MagicMock())
    session._build_main_agent = MagicMock(return_value=SimpleNamespace())
    return session


class TestStartKnowledgeBackground:
    def test_no_knowledge_is_noop(self):
        s = _bare_session()
        s.knowledge = None
        # Must not raise, must not need a loop.
        start_knowledge_background(s)

    def test_no_loop_is_noop(self):
        # Called outside an event loop → get_running_loop raises,
        # we early-return so Session's __init__ (which runs on the
        # main thread with no loop yet) doesn't crash.
        s = _bare_session()
        start_knowledge_background(s)  # should not raise

    @pytest.mark.asyncio
    async def test_schedules_start_on_loop(self):
        s = _bare_session()
        start_knowledge_background(s)
        # Give the scheduled task a chance to run.
        import asyncio

        await asyncio.sleep(0)
        s.knowledge.start.assert_called_once()


class TestEnsureKnowledgeStarted:
    @pytest.mark.asyncio
    async def test_no_knowledge_is_noop(self):
        s = _bare_session()
        s.knowledge = None
        await ensure_knowledge_started(s)  # should not raise

    @pytest.mark.asyncio
    async def test_calls_start(self):
        s = _bare_session()
        await ensure_knowledge_started(s)
        s.knowledge.start.assert_called_once()


class TestStartCodeindexBackground:
    def test_no_loop_is_noop(self):
        s = _bare_session()
        start_codeindex_background(s)  # should not raise

    @pytest.mark.asyncio
    async def test_bootstrap_runs_sweep_sync_and_watcher(self):
        s = _bare_session()
        start_codeindex_background(s)
        # Yield to the scheduled task.
        import asyncio

        await asyncio.sleep(0)
        await asyncio.sleep(0)  # bootstrap has multiple awaits
        s.code_index.sweep_stale_dirs.assert_called_once()
        s.code_index_sync.sync_now.assert_called_once()

    @pytest.mark.asyncio
    async def test_sweep_failure_does_not_stop_sync(self):
        # Session boot must survive a filesystem failure in the
        # sweep step — the sync + watcher still need to fire.
        s = _bare_session()
        s.code_index.sweep_stale_dirs = MagicMock(side_effect=RuntimeError("boom"))
        start_codeindex_background(s)
        import asyncio

        await asyncio.sleep(0)
        await asyncio.sleep(0)
        s.code_index_sync.sync_now.assert_called_once()


class TestStartMarketplaceRefreshBackground:
    def test_no_loop_is_noop(self):
        s = _bare_session()
        # Missing settings attr — should still no-op without
        # crashing before the loop check.
        s.settings = SimpleNamespace(storage=SimpleNamespace(data_dir="/tmp/x"))
        start_marketplace_refresh_background(s)  # should not raise


class TestEnsureMcp:
    @pytest.mark.asyncio
    async def test_once_per_session_gate(self):
        # Second call is a no-op — the ``_mcp_initialized`` flag
        # is the once-per-session gate.
        s = _bare_session()
        s._mcp_initialized = True
        await ensure_mcp(s)
        s.mcp_manager.list_servers.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_configured_servers_is_noop(self):
        s = _bare_session()
        s.mcp_manager.list_servers = MagicMock(return_value=[])
        await ensure_mcp(s)
        # Flag flipped so subsequent calls also no-op.
        assert s._mcp_initialized is True
        # No connect attempts.
        s.mcp_manager.connect.assert_not_called()

    @pytest.mark.asyncio
    async def test_connect_success_rebuilds_agents(self):
        s = _bare_session()
        s.mcp_manager.list_servers = MagicMock(return_value=["srv-a"])
        s.mcp_manager.connect = AsyncMock(return_value=MagicMock(functions={"t1": lambda: None}))
        await ensure_mcp(s)
        s.pool.build_agents.assert_called_once()
        s._build_main_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_connect_failure_skips_rebuild(self):
        # Every connect returned None → no clients → no rebuild.
        s = _bare_session()
        s.mcp_manager.list_servers = MagicMock(return_value=["srv-a"])
        s.mcp_manager.connect = AsyncMock(return_value=None)
        await ensure_mcp(s)
        s.pool.build_agents.assert_not_called()
        s._build_main_agent.assert_not_called()


class TestRebuildMcp:
    def test_rebuilds_with_connected_clients(self):
        s = _bare_session()
        c1 = MagicMock()
        s.mcp_manager.list_connected = MagicMock(return_value=["srv-a"])
        s.mcp_manager._clients = {"srv-a": c1}
        rebuild_mcp(s)
        s.pool.build_agents.assert_called_once_with(mcp_clients={"srv-a": c1})
        s._build_main_agent.assert_called_once()

    def test_no_connected_clients_passes_none(self):
        # Agno's ``build_agents`` treats ``None`` and ``{}``
        # slightly differently; we pass None explicitly so the
        # tool surface degrades to the non-MCP shape.
        s = _bare_session()
        rebuild_mcp(s)
        s.pool.build_agents.assert_called_once_with(mcp_clients=None)
