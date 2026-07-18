"""Plugin + marketplace RPC handlers."""

from __future__ import annotations

from typing import Any

from ember_code.backend.rpc_handlers.base import RpcHandler, rpc
from ember_code.protocol.rpc import RpcMethod


class PluginsRpcHandler(RpcHandler):
    """Plugin details / contents / preview / enable-toggle + install
    / update / remove, plus marketplace CRUD + refresh."""

    @rpc(RpcMethod.GET_PLUGIN_DETAILS)
    def get_plugin_details(self, args: dict) -> Any:
        return self._ctx.backend.get_plugin_details()

    @rpc(RpcMethod.GET_PLUGIN_CONTENTS)
    def get_plugin_contents(self, args: dict) -> Any:
        return self._ctx.backend.get_plugin_contents(args["name"])

    @rpc(RpcMethod.PREVIEW_PLUGIN)
    def preview_plugin(self, args: dict) -> Any:
        return self._ctx.backend.preview_plugin(
            source=args["source"],
            branch=args.get("branch"),
            subdir=args.get("subdir"),
        )

    @rpc(RpcMethod.SET_PLUGIN_ENABLED)
    def set_plugin_enabled(self, args: dict) -> Any:
        return self._ctx.backend.set_plugin_enabled(args["name"], args["enabled"])

    @rpc(RpcMethod.INSTALL_PLUGIN)
    def install_plugin(self, args: dict) -> Any:
        return self._ctx.backend.install_plugin(args["ref"], args.get("install_ref"))

    @rpc(RpcMethod.UPDATE_PLUGIN)
    def update_plugin(self, args: dict) -> Any:
        return self._ctx.backend.update_plugin(args["name"], args.get("install_ref"))

    @rpc(RpcMethod.REMOVE_PLUGIN)
    def remove_plugin(self, args: dict) -> Any:
        return self._ctx.backend.remove_plugin(args["name"])

    @rpc(RpcMethod.GET_MARKETPLACES)
    def get_marketplaces(self, args: dict) -> Any:
        return self._ctx.backend.get_marketplaces()

    @rpc(RpcMethod.ADD_MARKETPLACE)
    def add_marketplace(self, args: dict) -> Any:
        return self._ctx.backend.add_marketplace(args["url"])

    @rpc(RpcMethod.REMOVE_MARKETPLACE)
    def remove_marketplace(self, args: dict) -> Any:
        return self._ctx.backend.remove_marketplace(args["name"])

    @rpc(RpcMethod.REFRESH_MARKETPLACES)
    def refresh_marketplaces(self, args: dict) -> Any:
        return self._ctx.backend.refresh_marketplaces(args.get("name"))
