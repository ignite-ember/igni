"""Workspace-trust RPC handlers: the window's "Trust this folder?" dialog.

``get_workspace_trust`` → :class:`WorkspaceTrustStatus` for this runtime's
folder. ``set_workspace_trust`` with ``{"decision": "trust" | "distrust" |
"once"}`` records the answer ("once" lasts until the backend exits), pushes
the new status on ``workspace_trust_required`` and, when trust changed,
rebuilds the running session so the folder's tools, hooks, plugins, language
servers, monitors and config come in (or go). A refusal by the organisation's
policy fails the RPC with the policy's reason.

Not part of the extension host model: trusting a folder is the person's
decision (``coverage.RPC_UNREACHABLE``).
"""

from __future__ import annotations

from typing import Any

from ember_code.backend.rpc_handlers.base import RpcHandler, rpc
from ember_code.backend.workspace_trust_controller import WorkspaceTrustStatus
from ember_code.protocol.rpc import RpcMethod


class WorkspaceTrustRpcHandler(RpcHandler):
    """Read and answer the folder's workspace-trust decision."""

    @rpc(RpcMethod.GET_WORKSPACE_TRUST)
    def get_workspace_trust(self, args: dict) -> WorkspaceTrustStatus:
        return self._ctx.backend.workspace_trust.status()

    @rpc(RpcMethod.SET_WORKSPACE_TRUST)
    async def set_workspace_trust(self, args: dict) -> WorkspaceTrustStatus:
        backend: Any = self._ctx.backend
        status, changed = backend.workspace_trust.answer(str(args["decision"]))
        if changed:
            await backend.apply_workspace_trust()
        push = self._ctx.push
        if push is not None:
            push.workspace_trust(status.model_dump())
        return status
