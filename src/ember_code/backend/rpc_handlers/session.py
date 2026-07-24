"""Session lifecycle + status RPC handlers.

Handles everything about "which session am I on, what is it doing,
stop it, switch it" — the history / search / truncate cluster lives
in the sibling :mod:`.session_history` module to keep each class
under the 15-method ceiling.
"""

from __future__ import annotations

from typing import Any

from ember_code.backend.rpc_handlers.base import RpcHandler, rpc
from ember_code.protocol.rpc import RpcMethod


class SessionRpcHandler(RpcHandler):
    """Session lifecycle + status + agent-run control + hook fires."""

    @rpc(RpcMethod.SHUTDOWN)
    def shutdown(self, args: dict) -> Any:
        return self._ctx.backend.shutdown()

    @rpc(RpcMethod.UPLOAD_ATTACHMENT)
    def upload_attachment(self, args: dict) -> Any:
        return self._ctx.backend.upload_attachment(args["filename"], args["content_base64"])

    @rpc(RpcMethod.GET_PENDING_MESSAGES)
    def get_pending_messages(self, args: dict) -> Any:
        return self._ctx.backend.get_pending_messages(args["session_id"])

    @rpc(RpcMethod.LIST_SESSIONS)
    def list_sessions(self, args: dict) -> Any:
        return self._ctx.backend.list_sessions()

    @rpc(RpcMethod.SWITCH_SESSION)
    def switch_session(self, args: dict) -> Any:
        return self._ctx.backend.switch_session(args["session_id"])

    @rpc(RpcMethod.GET_PROCESSING)
    def get_processing(self, args: dict) -> bool:
        return self._ctx.backend.processing

    @rpc(RpcMethod.GET_SESSION_ID)
    def get_session_id(self, args: dict) -> str:
        return self._ctx.backend.session_id

    @rpc(RpcMethod.GET_RUN_TIMEOUT)
    def get_run_timeout(self, args: dict) -> int:
        return self._ctx.backend.run_timeout

    @rpc(RpcMethod.GET_STATUS)
    def get_status(self, args: dict) -> Any:
        return self._ctx.backend.get_status()

    @rpc(RpcMethod.CANCEL_RUN)
    def cancel_run(self, args: dict) -> Any:
        return self._ctx.backend.cancel_run()

    @rpc(RpcMethod.CANCEL_AGENT_RUN)
    def cancel_agent_run(self, args: dict) -> Any:
        return self._ctx.backend.cancel_agent_run(args.get("run_id", ""))

    @rpc(RpcMethod.FIRE_SESSION_START_HOOK)
    def fire_session_start_hook(self, args: dict) -> Any:
        return self._ctx.backend.fire_session_start_hook()

    @rpc(RpcMethod.AUTO_SYNC_KNOWLEDGE)
    def auto_sync_knowledge(self, args: dict) -> Any:
        return self._ctx.backend.auto_sync_knowledge()

    @rpc(RpcMethod.GET_PROJECT_DIR)
    def get_project_dir(self, args: dict) -> str:
        return str(self._ctx.backend.project_dir)
