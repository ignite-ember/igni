"""RPC handlers for the workflow runner.

Mirrors :class:`SkillsRpcHandler`: each method is tagged with
``@rpc(RpcMethod.X)`` and the router picks them up automatically
via :meth:`RpcHandler.methods`. The runner lives on
``backend.workflow_runner`` (set in :class:`BackendApp.run`); the
handlers just reach for it via the context bundle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ember_code.backend.rpc_handlers.base import RpcHandler, rpc
from ember_code.backend.schemas_workflows import WorkflowMetaEnvelope
from ember_code.protocol.rpc import RpcMethod

if TYPE_CHECKING:
    from ember_code.core.session.core import Session


class WorkflowsRpcHandler(RpcHandler):
    """Discover and run ``.claude/workflows/*.mjs`` scripts.

    Discovery is read-only and cached. ``run_workflow`` is the
    async entry point — it returns the ``workflow_run_id``
    immediately; the FE then folds subsequent ``workflow_event``
    pushes into a chat item keyed by that id.
    """

    @rpc(RpcMethod.LIST_WORKFLOWS)
    async def list_workflows(self, args: dict) -> list[dict[str, Any]]:
        runner = getattr(self._ctx.backend, "workflow_runner", None)
        if runner is None:
            return []
        envelopes: list[WorkflowMetaEnvelope] = await runner.list_workflows()
        return [
            {
                "name": env.meta.name,
                "description": env.meta.description,
                "whenToUse": env.meta.whenToUse,
                "phases": [p.model_dump() for p in env.meta.phases],
                "path": env.path,
            }
            for env in envelopes
        ]

    @rpc(RpcMethod.RUN_WORKFLOW)
    async def run_workflow(self, args: dict) -> dict[str, Any]:
        runner = getattr(self._ctx.backend, "workflow_runner", None)
        if runner is None:
            raise RuntimeError(
                "workflow_runner not initialised — BackendApp.run "
                "didn't construct one (check the boot path)"
            )
        name = str(args.get("name", ""))
        if not name:
            raise ValueError("run_workflow: 'name' is required")
        run_args = args.get("args") or {}
        if not isinstance(run_args, dict):
            raise TypeError(
                f"run_workflow: 'args' must be a dict, got {type(run_args).__name__}"
            )
        session = self._resolve_session(args.get("session_id", ""))
        workflow_run_id = await runner.run(
            name=name,
            args=run_args,
            session=session,
        )
        return {"workflow_run_id": workflow_run_id, "name": name}

    def _resolve_session(self, session_id: str) -> "Session":
        """Look up the live :class:`Session` for ``session_id``.

        Falls back to the default runtime's session when the id
        is empty or unknown (the BE's own session — useful for
        the boot "auto-name" workflow if we ever add one).
        """
        backend = self._ctx.backend
        if not session_id:
            return backend._session
        runtime = backend.sessions.find(session_id)
        if runtime is None or runtime.backend is None:
            return backend._session
        return runtime.backend._session
