"""Auth (login + cloud-credential) RPC handlers."""

from __future__ import annotations

from typing import Any

from ember_code.backend.rpc_handlers.base import RpcHandler, rpc
from ember_code.protocol.rpc import RpcMethod


class AuthRpcHandler(RpcHandler):
    """Login + credential-refresh + credential-clear."""

    @rpc(RpcMethod.LOGIN)
    async def login_start(self, args: dict) -> Any:
        return await self._ctx.login.start()

    @rpc(RpcMethod.RELOAD_CLOUD_CREDENTIALS)
    def reload_creds(self, args: dict) -> Any:
        return self._ctx.backend.reload_cloud_credentials()

    @rpc(RpcMethod.CLEAR_CLOUD_CREDENTIALS)
    def clear_creds(self, args: dict) -> Any:
        return self._ctx.backend.clear_cloud_credentials()

    @rpc(RpcMethod.DISCOVER_SERVER)
    async def discover_server(self, args: dict) -> Any:
        return await self._ctx.backend.discover_server(args.get("email", ""))

    @rpc(RpcMethod.SET_SERVER)
    def set_server(self, args: dict) -> Any:
        return self._ctx.backend.set_server(args.get("api_url", ""), args.get("portal_url", ""))
