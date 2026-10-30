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


class TestBothTransportsNamespaceTheirTools:
    """An MCP server's tools are prefixed whichever transport carried them.

    stdio servers got ``mcp_<server>_``; SSE servers got nothing. So an SSE
    server exposing ``read_file`` or ``run_shell_command`` collided head-on with
    the built-in of that name, and which one won depended on registration order.

    The asymmetry is the part worth pinning. Nothing in a server's config says
    the transport decides whether its tools are namespaced, so the collision
    appears when someone moves a working server from stdio to SSE and the tools
    change names underneath them.

    This used to compare the two ``f"mcp_{name}"`` literals by reading the
    source of both methods. They are one function now — which is the better
    fix, and also why reading for a literal stopped finding anything. What is
    pinned instead: neither site builds a prefix by hand, and the prefix can
    be taken back off, because something has to put a registered name back
    into the server's own vocabulary and for a long time nothing did.
    """

    def test_neither_transport_builds_its_own_prefix(self):
        """Two spellings would be as bad as one missing: the same server
        would answer to different names depending on how it was connected."""
        import inspect
        import re

        from ember_code.core.mcp.client import MCPClientManager
        from ember_code.core.mcp.stdio_binding import StdioMCPBinding

        for method in (MCPClientManager._open_transport, StdioMCPBinding.open):
            source = inspect.getsource(method)
            assert re.search(r'tool_name_prefix=f?"', source) is None, (
                f"{method.__qualname__} spells the prefix itself instead of "
                "calling tool_name_prefix()"
            )

    def test_both_transports_ask_for_the_prefix(self):
        import inspect

        from ember_code.core.mcp.client import MCPClientManager
        from ember_code.core.mcp.stdio_binding import StdioMCPBinding

        for method in (MCPClientManager._open_transport, StdioMCPBinding.open):
            source = inspect.getsource(method)
            assert "tool_name_prefix(name)" in source, (
                f"{method.__qualname__} does not namespace its tools — they "
                "will collide with built-ins of the same name"
            )

    def test_a_registered_name_converts_back(self):
        """The half that was missing. ``get_tools`` returns registered names
        and a manifest declares the server's own, so anything comparing the
        two has to convert — and ``permissions.tools`` did not, which is why
        it refused every tool it was ever asked about."""
        from ember_code.core.mcp.naming import bare_tool_name, tool_name_prefix

        server = "ext:acme.scanner"
        registered = f"{tool_name_prefix(server)}_list"
        assert registered == "mcp_ext:acme.scanner_list"
        assert bare_tool_name(server, registered) == "list"

    def test_a_name_that_is_not_prefixed_is_left_alone(self):
        """Callers pass lists from mixed sources, and a bare name is
        already bare."""
        from ember_code.core.mcp.naming import bare_tool_name

        assert bare_tool_name("ext:acme.scanner", "list") == "list"
        assert bare_tool_name("ext:acme.scanner", "mcp_other_list") == "mcp_other_list"
