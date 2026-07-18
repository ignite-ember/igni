"""Tests for mcp/client.py — MCP client connection management."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ember_code.core.mcp.client import MCPClientManager
from ember_code.core.mcp.config import MCPPolicy


class TestMCPClientManager:
    def _make_manager(self, configs=None):
        with (
            patch("ember_code.core.mcp.client.MCPConfigLoader") as MockLoader,
            patch("ember_code.core.mcp.client.MCPApprovalManager") as MockApproval,
            patch("ember_code.core.mcp.config.MCPPolicy.from_managed_settings") as mock_from,
        ):
            MockLoader.return_value.load.return_value = configs or {}
            # Auto-approve everything in existing tests
            MockApproval.return_value.check_approval.return_value = True
            mock_from.return_value = MCPPolicy()
            return MCPClientManager(project_dir="/tmp/test")

    def test_list_servers_empty(self):
        mgr = self._make_manager()
        assert mgr.list_servers() == []

    def test_list_servers_returns_names(self):
        configs = {"server1": MagicMock(), "server2": MagicMock()}
        mgr = self._make_manager(configs)
        assert set(mgr.list_servers()) == {"server1", "server2"}

    def test_list_connected_initially_empty(self):
        mgr = self._make_manager({"s1": MagicMock()})
        assert mgr.list_connected() == []

    @pytest.mark.asyncio
    async def test_connect_missing_config(self):
        mgr = self._make_manager()
        result = await mgr.connect("nonexistent")
        assert result.ok is False
        assert result.client is None
        assert "No config" in result.reason

    @pytest.mark.asyncio
    async def test_connect_unsupported_type(self):
        config = MagicMock()
        config.type = "grpc"
        mgr = self._make_manager({"test": config})
        result = await mgr.connect("test")
        assert result.ok is False
        assert "Unsupported" in result.reason

    @pytest.mark.asyncio
    async def test_connect_sse_missing_url(self):
        config = MagicMock()
        config.type = "sse"
        config.url = ""
        mgr = self._make_manager({"sse-server": config})
        result = await mgr.connect("sse-server")
        assert result.ok is False
        assert "url" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_connect_stdio_success(self):
        config = MagicMock()
        config.type = "stdio"
        config.command = "node"
        config.args = ["server.js"]
        config.env = {}

        mock_mcp_tools = MagicMock()
        mock_mcp_tools.functions = {"tool1": MagicMock()}

        mgr = self._make_manager({"my-server": config})
        with patch.object(
            mgr._stdio_binding, "open", new_callable=AsyncMock, return_value=mock_mcp_tools
        ):
            result = await mgr.connect("my-server")
            assert result.ok is True
            assert result.client is mock_mcp_tools
            assert "my-server" in mgr.list_connected()

    @pytest.mark.asyncio
    async def test_connect_returns_cached(self):
        config = MagicMock()
        config.type = "stdio"
        config.command = "node"
        config.args = []
        config.env = {}

        mock_mcp_tools = MagicMock()
        mock_mcp_tools.functions = {"tool1": MagicMock()}

        mgr = self._make_manager({"cached": config})
        with patch.object(
            mgr._stdio_binding, "open", new_callable=AsyncMock, return_value=mock_mcp_tools
        ):
            first = await mgr.connect("cached")
            second = await mgr.connect("cached")
            assert first.ok is True
            assert second.ok is True
            assert first.client is second.client

    @pytest.mark.asyncio
    async def test_connect_no_tools_closes(self):
        config = MagicMock()
        config.type = "stdio"
        config.command = "node"
        config.args = []
        config.env = {}

        mock_mcp_tools = MagicMock()
        mock_mcp_tools.__aexit__ = AsyncMock()
        mock_mcp_tools.functions = {}  # no tools

        mgr = self._make_manager({"empty": config})
        with patch.object(
            mgr._stdio_binding, "open", new_callable=AsyncMock, return_value=mock_mcp_tools
        ):
            result = await mgr.connect("empty")
            assert result.ok is False
            assert "no tools" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_connect_import_error(self):
        config = MagicMock()
        config.type = "stdio"
        config.command = "node"
        config.args = []
        config.env = {}

        mgr = self._make_manager({"broken": config})
        # Post-refactor the client imports MCPTools at module top with a
        # try/except ImportError guard (mirrors the ``pwd`` pattern in
        # ``frontend/tui/app.py``). Simulate the missing-dep case by
        # patching the module-local name to None.
        with patch("ember_code.core.mcp.client._MCPTools", None):
            result = await mgr.connect("broken")
            assert result.ok is False
            assert "not installed" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_disconnect_all(self):
        mgr = self._make_manager()
        mock_client = MagicMock()
        mock_client.__aexit__ = AsyncMock()
        mgr._clients = {"s1": mock_client}
        mgr.configs = {"s1": MagicMock(type="stdio")}

        await mgr.disconnect_all()
        assert mgr._clients == {}
        mock_client.__aexit__.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_skips_sse(self):
        mgr = self._make_manager()
        mock_client = MagicMock()
        mock_client.__aexit__ = AsyncMock()
        mgr._clients = {"sse": mock_client}
        mgr.configs = {"sse": MagicMock(type="sse")}

        await mgr.disconnect_all()
        assert mgr._clients == {}
        mock_client.__aexit__.assert_not_called()
