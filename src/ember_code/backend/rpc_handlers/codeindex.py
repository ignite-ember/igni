"""CodeIndex (status/sync/resync/clean/install/breakdown/activity) RPCs."""

from __future__ import annotations

from typing import Any

from ember_code.backend.rpc_handlers.base import RpcHandler, rpc
from ember_code.protocol.rpc import RpcMethod


class CodeIndexRpcHandler(RpcHandler):
    """Status + sync/resync + clean + install + head-breakdown +
    activity for the CodeIndex sidecar."""

    @rpc(RpcMethod.CODEINDEX_STATUS)
    def codeindex_status(self, args: dict) -> Any:
        return self._ctx.backend.codeindex_status()

    @rpc(RpcMethod.CODEINDEX_SYNC)
    def codeindex_sync(self, args: dict) -> Any:
        return self._ctx.backend.codeindex_sync(args.get("sha"))

    @rpc(RpcMethod.CODEINDEX_RESYNC)
    def codeindex_resync(self, args: dict) -> Any:
        return self._ctx.backend.codeindex_resync(args.get("sha"))

    @rpc(RpcMethod.CODEINDEX_CLEAN)
    def codeindex_clean(self, args: dict) -> Any:
        return self._ctx.backend.codeindex_clean()

    @rpc(RpcMethod.CODEINDEX_INSTALL)
    def codeindex_install(self, args: dict) -> Any:
        return self._ctx.backend.codeindex_install()

    @rpc(RpcMethod.CODEINDEX_HEAD_BREAKDOWN)
    def codeindex_head_breakdown(self, args: dict) -> Any:
        return self._ctx.backend.codeindex_head_breakdown()

    @rpc(RpcMethod.CODEINDEX_ACTIVITY)
    def codeindex_activity(self, args: dict) -> Any:
        return self._ctx.backend.codeindex_activity()
