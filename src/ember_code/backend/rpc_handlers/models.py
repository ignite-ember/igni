"""Model registry / permission / update-check RPC handlers."""

from __future__ import annotations

import logging
from typing import Any

from ember_code.backend.rpc_handlers.base import RpcHandler, rpc
from ember_code.backend.schemas_hitl import ToolCallArgs
from ember_code.backend.schemas_rpc import (
    DisplayConfigResult,
    ModelRegistryResult,
    UpdateAvailable,
)
from ember_code.core.utils.update_checker import PackageMetadata, check_for_update
from ember_code.protocol.rpc import RpcMethod

logger = logging.getLogger(__name__)


class ModelsRpcHandler(RpcHandler):
    """Model switching, verbose toggle, permission rule check + save,
    display-config + model-registry readers, and the update-check
    probe that surfaces "new version available" to the FE."""

    @rpc(RpcMethod.SWITCH_MODEL)
    def switch_model(self, args: dict) -> Any:
        return self._ctx.backend.switch_model(args["model_name"])

    @rpc(RpcMethod.TOGGLE_VERBOSE)
    def toggle_verbose(self, args: dict) -> Any:
        return self._ctx.backend.toggle_verbose()

    @rpc(RpcMethod.CHECK_PERMISSION)
    def check_permission(self, args: dict) -> Any:
        # Wire→domain boundary: validate the raw ``tool_args`` dict
        # into :class:`ToolCallArgs` here so downstream consumers get
        # a typed model rather than a raw ``dict[str, Any]``.
        tool_args = ToolCallArgs.model_validate(args["tool_args"])
        return self._ctx.backend.check_permission(args["tool_name"], args["func_name"], tool_args)

    @rpc(RpcMethod.SAVE_PERMISSION_RULE)
    def save_permission_rule(self, args: dict) -> Any:
        return self._ctx.backend.save_permission_rule(args["rule"], args["level"])

    @rpc(RpcMethod.GET_DISPLAY_CONFIG)
    def get_display_config(self, args: dict) -> DisplayConfigResult:
        return DisplayConfigResult.from_display(self._ctx.backend.settings.display)

    @rpc(RpcMethod.GET_MODEL_REGISTRY)
    def get_model_registry(self, args: dict) -> ModelRegistryResult:
        return ModelRegistryResult.from_settings(self._ctx.backend.settings.models)

    @rpc(RpcMethod.CHECK_FOR_UPDATE)
    async def check_for_update(self, args: dict) -> UpdateAvailable | None:
        try:
            info = await check_for_update()
            if info and info.available:
                return UpdateAvailable.from_update_info(info, pkg_name=PackageMetadata.load().name)
        except Exception:
            # Best-effort probe — network / packaging errors must
            # not surface as an RPC failure. The FE shows "no
            # update" as the neutral state; the debug log preserves
            # the traceback so silent schema-drift is still visible.
            logger.debug("check_for_update failed", exc_info=True)
        return None
