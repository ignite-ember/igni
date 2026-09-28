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
        return self._ctx.backend.artifact_add(args["source"], args.get("parent_id", ""))

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

    @rpc(RpcMethod.ARTIFACT_COMPARE)
    def artifact_compare(self, args: dict) -> Any:
        return self._ctx.backend.artifact_compare(
            args["id"], int(args["from_n"]), int(args["to_n"])
        )

    @rpc(RpcMethod.ARTIFACT_COMMENTS)
    def artifact_comments(self, args: dict) -> Any:
        return self._ctx.backend.artifact_comments(
            artifact_id=str(args.get("id") or ""),
            open_only=bool(args.get("open_only", True)),
        )

    @rpc(RpcMethod.ARTIFACT_RESOLVE_COMMENT)
    def artifact_resolve_comment(self, args: dict) -> Any:
        return self._ctx.backend.artifact_resolve_comment(
            comment_id=args["comment_id"],
            status=args["status"],
            content=str(args.get("content") or ""),
        )

    @rpc(RpcMethod.ARTIFACT_SYNC_COMMENTS)
    def artifact_sync_comments(self, args: dict) -> Any:
        return self._ctx.backend.artifact_sync_comments()

    @rpc(RpcMethod.ARTIFACT_RECONCILE_DIFF)
    def artifact_reconcile_diff(self, args: dict) -> Any:
        return self._ctx.backend.artifact_reconcile_diff(args["id"], args["source_id"])

    @rpc(RpcMethod.ARTIFACT_PREVIEW)
    def artifact_preview(self, args: dict) -> Any:
        return self._ctx.backend.artifact_preview(args["id"], int(args.get("version", 0)))

    @rpc(RpcMethod.ARTIFACT_EXPORT)
    def artifact_export(self, args: dict) -> Any:
        return self._ctx.backend.artifact_export(
            args["id"],
            int(args.get("version", 0)),
            args.get("destination", ""),
            args.get("format", "html"),
        )

    @rpc(RpcMethod.ARTIFACT_DRIFT_APPLY)
    def artifact_drift_apply(self, args: dict) -> Any:
        return self._ctx.backend.artifact_drift_apply(args["id"], args["source_id"])

    @rpc(RpcMethod.ARTIFACT_DRIFT)
    def artifact_drift(self, args: dict) -> Any:
        return self._ctx.backend.artifact_drift(args["id"], args["source_id"])

    @rpc(RpcMethod.ARTIFACT_PROVENANCE)
    def artifact_provenance(self, args: dict) -> Any:
        return self._ctx.backend.artifact_provenance(args["id"], int(args.get("n", 0)))

    @rpc(RpcMethod.ARTIFACT_CANDIDATE_EVIDENCE)
    def artifact_candidate_evidence(self, args: dict) -> Any:
        return self._ctx.backend.artifact_candidate_evidence(args["id"], args["source_id"])

    @rpc(RpcMethod.ARTIFACT_CANDIDATE_VERDICT)
    def artifact_candidate_verdict(self, args: dict) -> Any:
        return self._ctx.backend.artifact_candidate_verdict(args["id"], args["source_id"])

    @rpc(RpcMethod.ARTIFACT_COMMENT_ANCHOR)
    def artifact_comment_anchor(self, args: dict) -> Any:
        return self._ctx.backend.artifact_comment_anchor(args["comment_id"])

    @rpc(RpcMethod.ARTIFACT_COMMENT_RELEVANCE)
    def artifact_comment_relevance(self, args: dict) -> Any:
        return self._ctx.backend.artifact_comment_relevance(args["comment_id"])

    @rpc(RpcMethod.ARTIFACT_REPLY_COMMENT)
    def artifact_reply_comment(self, args: dict) -> Any:
        return self._ctx.backend.artifact_reply_comment(args["comment_id"], args.get("body", ""))

    @rpc(RpcMethod.ARTIFACT_TREE)
    def artifact_tree(self, args: dict) -> Any:
        return self._ctx.backend.artifact_tree()

    @rpc(RpcMethod.ARTIFACT_CREATE_FOLDER)
    def artifact_create_folder(self, args: dict) -> Any:
        return self._ctx.backend.artifact_create_folder(
            args.get("name", ""), args.get("parent_id", "")
        )

    @rpc(RpcMethod.ARTIFACT_RENAME_FOLDER)
    def artifact_rename_folder(self, args: dict) -> Any:
        return self._ctx.backend.artifact_rename_folder(args["folder_id"], args.get("name", ""))

    @rpc(RpcMethod.ARTIFACT_DELETE_FOLDER)
    def artifact_delete_folder(self, args: dict) -> Any:
        return self._ctx.backend.artifact_delete_folder(args["folder_id"])

    @rpc(RpcMethod.ARTIFACT_MOVE_NODE)
    def artifact_move_node(self, args: dict) -> Any:
        return self._ctx.backend.artifact_move_node(args["node_id"], args.get("parent_id", ""))

    @rpc(RpcMethod.ARTIFACT_COMMENT)
    def artifact_comment(self, args: dict) -> Any:
        return self._ctx.backend.artifact_comment(
            args["id"],
            int(args.get("n", 0)),
            args.get("body", ""),
            args.get("quote", ""),
            int(args.get("quote_start", -1)),
            int(args.get("quote_end", -1)),
            args.get("thread_id", ""),
            args.get("parent_id", ""),
            args.get("origin", "local"),
        )

    @rpc(RpcMethod.ARTIFACT_OPEN_EXPORT)
    def artifact_open_export(self, args: dict) -> Any:
        return self._ctx.backend.artifact_open_export(args["path"])

    @rpc(RpcMethod.ARTIFACT_SHARE)
    def artifact_share(self, args: dict) -> Any:
        return self._ctx.backend.artifact_share(
            args["id"],
            int(args.get("version", 0)),
            args.get("visibility", "private"),
            args.get("audience") or [],
            args.get("together_with") or [],
        )

    @rpc(RpcMethod.ARTIFACT_ASK)
    def artifact_ask(self, args: dict) -> Any:
        return self._ctx.backend.artifact_ask(args["question"])

    @rpc(RpcMethod.ARTIFACT_RUN_CYPHER)
    def artifact_run_cypher(self, args: dict) -> Any:
        return self._ctx.backend.artifact_run_cypher(args["cypher"])

    @rpc(RpcMethod.ARTIFACT_REMOVE)
    def artifact_remove(self, args: dict) -> Any:
        return self._ctx.backend.artifact_remove(args["id"])

    @rpc(RpcMethod.READ_FILE)
    def read_file(self, args: dict) -> Any:
        return self._ctx.backend.read_file(args["path"])

    @rpc(RpcMethod.SEARCH_CODE)
    def search_code(self, args: dict) -> Any:
        return self._ctx.backend.search_code(args["snippet"], args.get("max_results", 20))
