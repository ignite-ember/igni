"""Artifact store + read-file + search-code RPC handlers."""

from __future__ import annotations

from typing import Any

from ember_code.backend.rpc_handlers.base import RpcHandler, rpc
from ember_code.protocol.rpc import RpcMethod


class ArtifactRpcHandler(RpcHandler):
    """Status, semantic search, CRUD, version and lineage reads, the
    reconcile surface, export/share, plus the ``read_file`` +
    ``search_code`` tool-like RPCs that share the same access model."""

    @rpc(RpcMethod.GET_ARTIFACT_STATUS)
    def get_artifact_status(self, args: dict) -> Any:
        return self._ctx.backend.get_artifact_status()

    @rpc(RpcMethod.ARTIFACT_SEARCH)
    def artifact_search(self, args: dict) -> Any:
        return self._ctx.backend.artifact_search(args["query"])

    @rpc(RpcMethod.ARTIFACT_ADD)
    def artifact_add(self, args: dict) -> Any:
        return self._ctx.backend.artifact_add(args["source"])

    @rpc(RpcMethod.ARTIFACT_LIST)
    def artifact_list(self, args: dict) -> Any:
        return self._ctx.backend.artifact_list()

    @rpc(RpcMethod.ARTIFACT_GET)
    def artifact_get(self, args: dict) -> Any:
        return self._ctx.backend.artifact_get(args["id"], int(args.get("version", 0)))

    @rpc(RpcMethod.ARTIFACT_VERSIONS)
    def artifact_versions(self, args: dict) -> Any:
        return self._ctx.backend.artifact_versions(args["id"])

    @rpc(RpcMethod.ARTIFACT_LINEAGE)
    def artifact_lineage(self, args: dict) -> Any:
        return self._ctx.backend.artifact_lineage(args["id"], int(args.get("version", 0)))

    @rpc(RpcMethod.ARTIFACT_STALE)
    def artifact_stale(self, args: dict) -> Any:
        return self._ctx.backend.artifact_stale()

    @rpc(RpcMethod.ARTIFACT_CONFIRM_SOURCE)
    def artifact_confirm_source(self, args: dict) -> Any:
        return self._ctx.backend.artifact_confirm_source(args["id"], args["source_id"])

    @rpc(RpcMethod.ARTIFACT_DISMISS_SOURCE)
    def artifact_dismiss_source(self, args: dict) -> Any:
        return self._ctx.backend.artifact_dismiss_source(args["id"], args["source_id"])

    @rpc(RpcMethod.ARTIFACT_RECONCILE_DIFF)
    def artifact_reconcile_diff(self, args: dict) -> Any:
        return self._ctx.backend.artifact_reconcile_diff(args["id"], args["source_id"])

    @rpc(RpcMethod.ARTIFACT_PREVIEW)
    def artifact_preview(self, args: dict) -> Any:
        return self._ctx.backend.artifact_preview(args["id"], int(args.get("version", 0)))

    @rpc(RpcMethod.ARTIFACT_EXPORT)
    def artifact_export(self, args: dict) -> Any:
        return self._ctx.backend.artifact_export(
            args["id"], int(args.get("version", 0)), args.get("destination", "")
        )

    @rpc(RpcMethod.ARTIFACT_SHARE)
    def artifact_share(self, args: dict) -> Any:
        return self._ctx.backend.artifact_share(
            args["id"],
            int(args.get("version", 0)),
            args.get("visibility", "private"),
            args.get("audience") or [],
            args.get("together_with") or [],
        )

    @rpc(RpcMethod.ARTIFACT_REMOVE)
    def artifact_remove(self, args: dict) -> Any:
        return self._ctx.backend.artifact_remove(args["id"])

    @rpc(RpcMethod.READ_FILE)
    def read_file(self, args: dict) -> Any:
        return self._ctx.backend.read_file(args["path"])

    @rpc(RpcMethod.SEARCH_CODE)
    def search_code(self, args: dict) -> Any:
        return self._ctx.backend.search_code(args["snippet"], args.get("max_results", 20))
