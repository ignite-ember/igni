"""Hooks (session-lifecycle + tool-gate) RPC handlers."""

from __future__ import annotations

from typing import Any

from ember_code.backend.rpc_handlers.base import RpcHandler, rpc
from ember_code.backend.schemas_panels import HookEntryView
from ember_code.protocol.rpc import RpcMethod


class HooksRpcHandler(RpcHandler):
    """Details view + reload for the currently-registered hooks."""

    @rpc(RpcMethod.GET_HOOKS_DETAILS)
    def get_hooks_details(self, args: dict) -> list[HookEntryView]:
        return self._ctx.backend.get_hooks_details()

    @rpc(RpcMethod.RELOAD_HOOKS)
    def reload_hooks(self, args: dict) -> Any:
        return self._ctx.backend.reload_hooks_rpc()
