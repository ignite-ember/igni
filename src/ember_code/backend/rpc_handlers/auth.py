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
