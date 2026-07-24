"""Scheduler (recurring / one-shot) RPC handlers."""

from __future__ import annotations

from typing import Any

from ember_code.backend.rpc_handlers.base import RpcHandler, rpc
from ember_code.protocol.rpc import RpcMethod


class SchedulerRpcHandler(RpcHandler):
    """Task execution, cancellation, listing, and scheduler
    (re)start. Starting the scheduler is delegated to the
    :class:`PushNotificationBridge` on the shared context, matching
    the boot-time auto-start path."""

    @rpc(RpcMethod.EXECUTE_SCHEDULED_TASK)
    def execute_scheduled_task(self, args: dict) -> Any:
        return self._ctx.backend.execute_scheduled_task(args["description"])

    @rpc(RpcMethod.CANCEL_SCHEDULED_TASK)
    def cancel_scheduled_task(self, args: dict) -> Any:
        return self._ctx.backend.cancel_scheduled_task(args["task_id"])

    @rpc(RpcMethod.GET_SCHEDULED_TASKS)
    def get_scheduled_tasks(self, args: dict) -> Any:
        return self._ctx.backend.get_scheduled_tasks(args.get("include_done", True))

    @rpc(RpcMethod.START_SCHEDULER)
    def start_scheduler(self, args: dict) -> None:
        self._ctx.push.start_scheduler(self._ctx.backend)
