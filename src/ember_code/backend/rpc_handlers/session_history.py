"""Chat history + search + truncation + context-token RPCs.

Split out of :class:`SessionRpcHandler` because the combined cluster
went past the 15-method-per-class ceiling. History concerns are a
natural sub-cluster: query the past + measure it + trim it.
"""

from __future__ import annotations

from typing import Any

from ember_code.backend.rpc_handlers.base import RpcHandler, rpc
from ember_code.protocol.rpc import RpcMethod


class SessionHistoryRpcHandler(RpcHandler):
    """Chat log query + search + truncate + context-token count."""

    @rpc(RpcMethod.GET_CHAT_HISTORY)
    def get_chat_history(self, args: dict) -> Any:
        return self._ctx.backend.get_chat_history(args["session_id"])

    @rpc(RpcMethod.SEARCH_CHAT)
    def search_chat(self, args: dict) -> Any:
        return self._ctx.backend.search_chat(
            args["session_id"], args["query"], int(args.get("limit", 50))
        )

    @rpc(RpcMethod.TRUNCATE_HISTORY)
    def truncate_history(self, args: dict) -> Any:
        return self._ctx.backend.truncate_history(args["session_id"], args["run_id"])

    @rpc(RpcMethod.COUNT_CONTEXT_TOKENS)
    def count_context_tokens(self, args: dict) -> Any:
        return self._ctx.backend.count_context_tokens()
