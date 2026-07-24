"""Knowledge base + read-file + search-code RPC handlers."""

from __future__ import annotations

from typing import Any

from ember_code.backend.rpc_handlers.base import RpcHandler, rpc
from ember_code.protocol.rpc import RpcMethod


class KnowledgeRpcHandler(RpcHandler):
    """Status, semantic-search, CRUD, plus the ``read_file`` +
    ``search_code`` tool-like RPCs that share the KB's access model."""

    @rpc(RpcMethod.GET_KNOWLEDGE_STATUS)
    def get_knowledge_status(self, args: dict) -> Any:
        return self._ctx.backend.get_knowledge_status()

    @rpc(RpcMethod.KNOWLEDGE_SEARCH)
    def knowledge_search(self, args: dict) -> Any:
        return self._ctx.backend.knowledge_search(args["query"])

    @rpc(RpcMethod.KNOWLEDGE_ADD)
    def knowledge_add(self, args: dict) -> Any:
        return self._ctx.backend.knowledge_add(args["source"])

    @rpc(RpcMethod.KNOWLEDGE_LIST)
    def knowledge_list(self, args: dict) -> Any:
        return self._ctx.backend.knowledge_list()

    @rpc(RpcMethod.KNOWLEDGE_GET)
    def knowledge_get(self, args: dict) -> Any:
        return self._ctx.backend.knowledge_get(args["id"])

    @rpc(RpcMethod.KNOWLEDGE_REMOVE)
    def knowledge_remove(self, args: dict) -> Any:
        return self._ctx.backend.knowledge_remove(args["id"])

    @rpc(RpcMethod.READ_FILE)
    def read_file(self, args: dict) -> Any:
        return self._ctx.backend.read_file(args["path"])

    @rpc(RpcMethod.SEARCH_CODE)
    def search_code(self, args: dict) -> Any:
        return self._ctx.backend.search_code(args["snippet"], args.get("max_results", 20))
