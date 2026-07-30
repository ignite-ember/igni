"""Plan-mode RPC handlers (latest / approve / dismiss)."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from ember_code.backend.rpc_handlers.base import RpcHandler, rpc
from ember_code.protocol.rpc import RpcMethod


class PlanRpcHandler(RpcHandler):
    """Latest snapshot + approve + dismiss. ``approve_plan`` /
    ``dismiss_plan`` are async on ``Session`` — the handler returns
    the coroutine and the dispatcher awaits whatever handlers return.
    """

    @rpc(RpcMethod.GET_LATEST_PLAN)
    def get_latest_plan(self, args: dict) -> Any:
        return self._ctx.backend.get_latest_plan()

    @rpc(RpcMethod.APPROVE_PLAN)
    def approve_plan(self, args: dict) -> Awaitable[Any]:
        return self._ctx.backend.approve_plan(run_id=str(args.get("run_id", "")))

    @rpc(RpcMethod.DISMISS_PLAN)
    def dismiss_plan(self, args: dict) -> Awaitable[Any]:
        return self._ctx.backend.dismiss_plan(run_id=str(args.get("run_id", "")))
