"""MCP integration — Model Context Protocol client."""

from ember_code.core.mcp.approval import MCPApprovalManager
from ember_code.core.mcp.client import MCPClientManager
from ember_code.core.mcp.config import MCPConfigLoader, MCPPolicy, MCPServerConfig
from ember_code.core.mcp.schemas import (
    MCPConnectResult,
    MCPPrompt,
    MCPResource,
    MCPToolInfo,
)
from ember_code.core.mcp.stdio_binding import StdioMCPBinding
from ember_code.core.mcp.tool_filter import MCPToolFilter
from ember_code.core.mcp.tools import MCPToolProvider

__all__ = [
    "MCPApprovalManager",
    "MCPClientManager",
    "MCPConfigLoader",
    "MCPConnectResult",
    "MCPPolicy",
    "MCPPrompt",
    "MCPResource",
    "MCPServerConfig",
    "MCPToolFilter",
    "MCPToolInfo",
    "MCPToolProvider",
    "StdioMCPBinding",
]
