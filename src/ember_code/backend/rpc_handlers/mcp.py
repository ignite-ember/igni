"""MCP-server RPC handlers."""

from __future__ import annotations

from typing import Any

from ember_code.backend.rpc_handlers.base import RpcHandler, rpc
from ember_code.protocol.rpc import RpcMethod


class McpRpcHandler(RpcHandler):
    """Ensure/connect/disconnect/status/details for MCP servers +
    per-tool enable toggling."""

    @rpc(RpcMethod.ENSURE_MCP)
    def ensure_mcp(self, args: dict) -> Any:
        return self._ctx.backend.ensure_mcp()

    @rpc(RpcMethod.MCP_CONNECT)
    def mcp_connect(self, args: dict) -> Any:
        return self._ctx.backend.mcp_connect(args["server_name"])

    @rpc(RpcMethod.MCP_DISCONNECT)
    def mcp_disconnect(self, args: dict) -> Any:
        return self._ctx.backend.mcp_disconnect(args["server_name"])

    @rpc(RpcMethod.GET_MCP_STATUS)
    def mcp_status(self, args: dict) -> Any:
        return self._ctx.backend.get_mcp_status()

    @rpc(RpcMethod.GET_MCP_SERVER_DETAILS)
    def mcp_server_details(self, args: dict) -> Any:
        return self._ctx.backend.get_mcp_server_details()

    @rpc(RpcMethod.GET_MCP_SERVERS)
    def mcp_servers(self, args: dict) -> Any:
        return self._ctx.backend.get_mcp_servers()

    @rpc(RpcMethod.SET_MCP_TOOL_ENABLED)
    def mcp_set_tool_enabled(self, args: dict) -> Any:
        return self._ctx.backend.set_mcp_tool_enabled(
            server=args["server"],
            tool=args["tool"],
            enabled=args["enabled"],
        )
