"""``/loop`` + compaction + learning RPC handlers."""

from __future__ import annotations

from typing import Any

from ember_code.backend.rpc_handlers.base import RpcHandler, rpc
from ember_code.protocol.rpc import RpcMethod


class LoopRpcHandler(RpcHandler):
    """Loop iteration control (pop/cancel/status/resume/pause) plus
    the compaction + learning extraction RPCs that ride on the same
    turn boundary."""

    @rpc(RpcMethod.POP_PENDING_LOOP_ITERATION)
    def loop_pop(self, args: dict) -> Any:
        return self._ctx.backend.pop_pending_loop_iteration()

    @rpc(RpcMethod.CANCEL_PENDING_LOOP)
    def loop_cancel(self, args: dict) -> Any:
        return self._ctx.backend.cancel_pending_loop()

    @rpc(RpcMethod.LOOP_STATUS)
    def loop_status(self, args: dict) -> Any:
        return self._ctx.backend.loop_status()

    @rpc(RpcMethod.LOOP_RESUME)
    def loop_resume(self, args: dict) -> Any:
        return self._ctx.backend.loop_resume()

    @rpc(RpcMethod.LOOP_PAUSE)
    def loop_pause(self, args: dict) -> Any:
        return self._ctx.backend.loop_pause()

    @rpc(RpcMethod.COMPACT_IF_NEEDED)
    def compact_if_needed(self, args: dict) -> Any:
        return self._ctx.backend.compact_if_needed(args["ctx_tokens"], args["max_ctx"])

    @rpc(RpcMethod.EXTRACT_LEARNINGS)
    def extract_learnings(self, args: dict) -> Any:
        return self._ctx.backend.extract_learnings(args["user_msg"], args["assistant_msg"])
