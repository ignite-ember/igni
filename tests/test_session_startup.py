"""Unit tests for ``session/startup`` sub-package.

Rewritten in iter 146 when the ``startup_ops.py`` monolith split
into a phase-based sub-package. The Session-method delegates are
covered end-to-end via Session's boot path; these tests pin the
phase-class contracts in isolation — most importantly:

* Every background starter is a **no-op when no event loop is
  running** (``get_running_loop`` raises ``RuntimeError`` → early
  return). Session's caller retries once the loop is up.
* Failures inside the fire-and-forget tasks are logged and
  swallowed — session boot must not gate on offline external
  deps.
* :meth:`McpInitPhase.rebuild_current` composes ``list_connected``
  + ``_clients`` correctly.
* :meth:`McpInitPhase.ensure` respects the once-per-session gate
  and returns the typed :class:`McpInitResult` envelope.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.core.session.schemas import McpInitResult
from ember_code.core.session.startup import SessionStartupCoordinator


def _bare_session():
    """Session-shaped stub carrying only what the startup phases read."""
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
    from ember_code.core.mcp.schemas import MCPConnectResult

    session.mcp_manager = SimpleNamespace()
    session.mcp_manager.list_servers = MagicMock(return_value=[])
    session.mcp_manager.list_connected = MagicMock(return_value=[])
    session.mcp_manager._clients = {}
    session.mcp_manager.connect = AsyncMock(return_value=MCPConnectResult.failure(""))
    # Session-scoped failure cache — populated by
    # ``record_mcp_result`` in production. Tests that assert on
    # the failure path also write to this dict directly.
    session.mcp_failures = {}
    session.record_mcp_result = MagicMock()
    session.pool = SimpleNamespace(build_agents=MagicMock())
    session._build_main_agent = MagicMock(return_value=SimpleNamespace())
    # The startup phases route through the public rebuild seam so
    # the private ``_build_main_agent`` name doesn't leak into the
    # coordinator. Track it separately for the assertions below.
    session.rebuild_main_team = MagicMock()
    # ``ensure_mcp`` calls ``session.display.print_info(...)`` on the
    # connect-failure branch — stub the display surface so the failure
    # path renders instead of crashing.
    session.display = MagicMock()
    return session


class TestStartKnowledgeBackground:
    def test_no_knowledge_is_noop(self):
        s = _bare_session()
        s.knowledge = None
        # Must not raise, must not need a loop.
        SessionStartupCoordinator(s).start_knowledge_background()

    def test_no_loop_is_noop(self):
        # Called outside an event loop → get_running_loop raises,
        # we early-return so Session's __init__ (which runs on the
        # main thread with no loop yet) doesn't crash.
        s = _bare_session()
        SessionStartupCoordinator(s).start_knowledge_background()  # should not raise

    @pytest.mark.asyncio
    async def test_schedules_start_on_loop(self):
        s = _bare_session()
        SessionStartupCoordinator(s).start_knowledge_background()
        # Give the scheduled task a chance to run.
        import asyncio

        await asyncio.sleep(0)
        s.knowledge.start.assert_called_once()


class TestEnsureKnowledgeStarted:
    @pytest.mark.asyncio
    async def test_no_knowledge_is_noop(self):
        s = _bare_session()
        s.knowledge = None
        await SessionStartupCoordinator(s).ensure_knowledge_started()  # should not raise

    @pytest.mark.asyncio
    async def test_calls_start(self):
        s = _bare_session()
        await SessionStartupCoordinator(s).ensure_knowledge_started()
        s.knowledge.start.assert_called_once()


class TestStartCodeindexBackground:
    def test_no_loop_is_noop(self):
        s = _bare_session()
        SessionStartupCoordinator(s).start_codeindex_background()  # should not raise

    @pytest.mark.asyncio
    async def test_bootstrap_runs_sweep_sync_and_watcher(self):
        s = _bare_session()
        SessionStartupCoordinator(s).start_codeindex_background()
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
        SessionStartupCoordinator(s).start_codeindex_background()
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
        SessionStartupCoordinator(s).start_marketplace_refresh_background()  # should not raise


class TestEnsureMcp:
    @pytest.mark.asyncio
    async def test_once_per_session_gate(self):
        # Second call is a no-op — the ``mcp_initialized`` flag
        # is the once-per-session gate.
        s = _bare_session()
        coord = SessionStartupCoordinator(s)
        coord.mcp_initialized = True
        result = await coord.ensure_mcp()
        s.mcp_manager.list_servers.assert_not_called()
        assert isinstance(result, McpInitResult)
        assert result.skipped_reason == "already_initialized"
        assert result.connected == []
        assert result.failed == {}
        assert result.rebuilt is False

    @pytest.mark.asyncio
    async def test_no_configured_servers_is_noop(self):
        s = _bare_session()
        s.mcp_manager.list_servers = MagicMock(return_value=[])
        coord = SessionStartupCoordinator(s)
        result = await coord.ensure_mcp()
        # Flag flipped so subsequent calls also no-op.
        assert coord.mcp_initialized is True
        # No connect attempts.
        s.mcp_manager.connect.assert_not_called()
        assert result.skipped_reason == "no_configured_servers"
        assert result.connected == []
        assert result.rebuilt is False

    @pytest.mark.asyncio
    async def test_connect_success_rebuilds_agents(self):
        from ember_code.core.mcp.schemas import MCPConnectResult

        s = _bare_session()
        s.mcp_manager.list_servers = MagicMock(return_value=["srv-a"])
        s.mcp_manager.connect = AsyncMock(
            return_value=MCPConnectResult.success(MagicMock(functions={"t1": lambda: None}))
        )
        result = await SessionStartupCoordinator(s).ensure_mcp()
        s.pool.build_agents.assert_called_once()
        s.rebuild_main_team.assert_called_once()
        # Pattern-3 envelope reflects the success path.
        assert result.rebuilt is True
        assert result.connected == ["srv-a"]
        assert result.failed == {}
        assert result.skipped_reason is None

    @pytest.mark.asyncio
    async def test_connect_failure_skips_rebuild(self):
        # Every connect returned an ``ok=False`` Result → no
        # clients → no rebuild.
        from ember_code.core.mcp.schemas import MCPConnectResult

        s = _bare_session()
        s.mcp_manager.list_servers = MagicMock(return_value=["srv-a"])
        s.mcp_manager.connect = AsyncMock(
            return_value=MCPConnectResult.failure("handshake refused")
        )
        result = await SessionStartupCoordinator(s).ensure_mcp()
        s.pool.build_agents.assert_not_called()
        s.rebuild_main_team.assert_not_called()
        # Envelope surfaces the failed set so callers don't
        # grep-parse logs.
        assert result.rebuilt is False
        assert result.connected == []
        assert result.failed == {"srv-a": "handshake refused"}
        assert result.skipped_reason == "no_clients_connected"


class TestRebuildMcp:
    def test_rebuilds_with_connected_clients(self):
        s = _bare_session()
        c1 = MagicMock()
        s.mcp_manager.list_connected = MagicMock(return_value=["srv-a"])
        s.mcp_manager._clients = {"srv-a": c1}
        SessionStartupCoordinator(s).rebuild_mcp()
        s.pool.build_agents.assert_called_once_with(mcp_clients={"srv-a": c1})
        s.rebuild_main_team.assert_called_once()

    def test_no_connected_clients_passes_none(self):
        # Agno's ``build_agents`` treats ``None`` and ``{}``
        # slightly differently; we pass None explicitly so the
        # tool surface degrades to the non-MCP shape.
        s = _bare_session()
        SessionStartupCoordinator(s).rebuild_mcp()
        s.pool.build_agents.assert_called_once_with(mcp_clients=None)
