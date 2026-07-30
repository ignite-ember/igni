"""MCP tool integration — wraps MCP servers as Agno tools for agents."""

from typing import Any

from ember_code.core.mcp.client import MCPClientManager


class MCPToolProvider:
    """Provides MCP-based tools to agents."""

    def __init__(self, mcp_manager: MCPClientManager):
        self.mcp_manager = mcp_manager

    async def get_tools_for_agent(self, mcp_server_names: list[str]) -> list[Any]:
        """Get Agno-compatible tool instances from MCP servers.

        Args:
            mcp_server_names: Names of MCP servers to connect to.

        Returns:
            List of Agno toolkit instances from connected MCP servers.
        """
        tools = []
        for name in mcp_server_names:
            result = await self.mcp_manager.connect(name)
            if result.ok and result.client is not None:
                tools.append(result.client)
        return tools
