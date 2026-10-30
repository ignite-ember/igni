"""What igni does, as host-model signals.

Two sources, which together are every change igni makes:

* **every message igni sends its windows** — run events, mirroring, the
  push channels. :meth:`BroadcastSignals.observe` sees each one on its
  way out (the backend's transport is wrapped once, at the root) and
  translates it into the model's signal, in the model's shape;
* **every write an RPC makes**, whoever made it — a window or an
  extension. :meth:`BroadcastSignals.after_rpc` is told after each
  successful one, so "every write is a signal" holds for writes that
  broadcast nothing of their own.

The translation is the point of this module. igni's broadcasts are its
private wire and change with its UI; the model's signals are the
promise. A field renamed on the wire is fixed here, once, and no
extension notices.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ember_code.core.approvals.events import ApprovalChange
from ember_code.extensions.host.grants import HostGrants
from ember_code.extensions.host.members import AppMembers
from ember_code.protocol import messages as msg
from ember_code.protocol.rpc import RpcMethod

if TYPE_CHECKING:
    from ember_code.extensions.host.model import HostModel

logger = logging.getLogger(__name__)


def _run_ref(m: Any) -> dict[str, Any]:
    return {
        "session_id": m.session_id,
        "run_id": str(getattr(m, "run_id", "") or ""),
        "parent_run_id": str(getattr(m, "parent_run_id", "") or ""),
    }


class BroadcastSignals:
    """Translates igni's traffic into signals, and keeps what it implies."""

    def __init__(self, model: HostModel) -> None:
        self.model = model
        self.state = model.state
        self._activity_listeners: list[Callable[[dict[str, Any]], None]] = []

    def on_activity(self, listener: Callable[[dict[str, Any]], None]) -> None:
        """Be told the new ``app.activity`` whenever ``app.activity_changed`` is."""
        self._activity_listeners.append(listener)

    # ── Outbound messages ─────────────────────────────────────────

    #: Messages after which igni may have started or stopped doing something.
    _ACTIVITY_MOVES = frozenset(
        {
            "run_started",
            "run_completed",
            "run_error",
            "run_paused",
            "hitl_request",
            "requirement_resolved",
            "stream_end",
            "streaming_done",
        }
    )

    def observe(self, m: msg.Message) -> None:
        handler = getattr(self, f"_on_{m.type}", None)
        if handler is not None:
            handler(m)
        if m.type in self._ACTIVITY_MOVES:
            self.activity_moved()

    def activity_moved(self) -> None:
        """Tell ``app.activity_changed`` when the activity is not what it was."""
        try:
            activity = self.model.backend.activity()
        except Exception as exc:
            logger.debug("host model: activity unavailable: %s", exc)
            return
        if activity == self.state.activity:
            return
        self.state.activity = activity
        self.model.emit("app.activity_changed", activity)
        for listener in list(self._activity_listeners):
            try:
                listener(activity)
            except Exception:
                logger.exception("an activity listener failed")

    def approval_changed(self, change: ApprovalChange) -> None:
        """An approval appeared or went away (``core/approvals/events.py``)."""
        self.model.emit("chat.approvals.changed", change.model_dump(mode="json"))
        self.activity_moved()

    def _on_user_message_received(self, m: msg.UserMessageReceived) -> None:
        self.model.windows.touch_connection(m.client_id)
        self.model.emit(
            "chat.message_added",
            {
                "session_id": m.session_id,
                "text": m.text,
                "queued": m.queued,
                "from_extension": m.extension_id or None,
                "from_service": m.service_client_id or None,
            },
        )

    def _on_typing(self, m: msg.Typing) -> None:
        window = self.model.windows.by_connection(m.client_id)
        self.model.emit(
            "chat.typing", {"session_id": m.session_id, "text": m.text, "window": window}
        )
        if window is not None:
            # Typing is using the window. The draft itself is the
            # window's model, written by the window (``ui.composer``).
            self.model.windows.touch(window)

    def _on_content_delta(self, m: msg.ContentDelta) -> None:
        self.model.emit(
            "chat.content_delta",
            {"session_id": m.session_id, "text": m.text, "thinking": m.is_thinking},
        )

    def _on_reasoning_started(self, m: msg.ReasoningStarted) -> None:
        self.model.emit("chat.reasoning_started", _run_ref(m))

    def _on_run_started(self, m: msg.RunStarted) -> None:
        if not m.parent_run_id:
            self.state.last_run[m.session_id] = m.run_id
            self.state.run_files[m.session_id] = set()
        self.model.emit(
            "chat.run_started", {**_run_ref(m), "agent_name": m.agent_name, "model": m.model}
        )

    def _on_run_completed(self, m: msg.RunCompleted) -> None:
        files: set[str] = set()
        if not m.parent_run_id:
            files = self.state.run_files.pop(m.session_id, set())
            self.state.approvals.pop(m.session_id, None)
        base = {
            **_run_ref(m),
            "input_tokens": m.input_tokens,
            "output_tokens": m.output_tokens,
            "reasoning_tokens": m.reasoning_tokens,
            "duration": float(m.duration),
        }

        def seen(grants: HostGrants) -> dict[str, Any]:
            visible = sorted(f for f in files if grants.workspace.permits(f))
            return {**base, "changed_files": visible}

        self.model.emit("chat.run_completed", None, content=seen)

    def _on_run_error(self, m: msg.RunError) -> None:
        self.state.approvals.pop(m.session_id, None)
        self.state.run_files.pop(m.session_id, None)
        self.model.emit("chat.run_error", {"session_id": m.session_id, "error": m.error})

    def _approval(self, session_id: str, r: msg.HITLRequest) -> dict[str, Any]:
        approval = {
            "requirement_id": r.requirement_id,
            "session_id": session_id,
            "tool_name": r.tool_name,
            "friendly_name": r.friendly_name,
            "tool_args": dict(r.tool_args),
            "details": r.details,
            "agent_path": list(r.agent_path),
        }
        self.state.approvals.setdefault(session_id, {})[r.requirement_id] = approval
        return approval

    def restore_approvals(self, session_id: str, waiting: list[msg.Message]) -> None:
        """Take the session's waiting approvals from the backend, quietly.

        The backend keeps them in the database
        (:class:`~ember_code.core.approvals.journal.ApprovalJournal`);
        after a restart this is how the host learns of them before any
        window attaches and has them shown again. Nothing is emitted:
        they were requested before, and an extension that listened then
        heard it.
        """
        approvals = self.state.approvals.setdefault(session_id, {})
        for paused in waiting:
            if isinstance(paused, msg.RunPaused):
                for request in paused.requirements:
                    if request.requirement_id not in approvals:
                        self._approval(session_id, request)

    def _on_run_paused(self, m: msg.RunPaused) -> None:
        approvals = [self._approval(m.session_id, r) for r in m.requirements]
        self.model.emit(
            "chat.run_paused",
            {"session_id": m.session_id, "run_id": m.run_id, "requirements": approvals},
        )
        for approval in approvals:
            self.model.emit("chat.approvals.requested", approval)

    def _on_hitl_request(self, m: msg.HITLRequest) -> None:
        self.model.emit("chat.approvals.requested", self._approval(m.session_id, m))

    def _on_requirement_resolved(self, m: msg.RequirementResolved) -> None:
        self.state.approvals.get(m.session_id, {}).pop(m.requirement_id, None)
        self.model.emit(
            "chat.approvals.resolved",
            {"session_id": m.session_id, "requirement_id": m.requirement_id},
        )

    def _on_requirement_details(self, m: msg.RequirementDetails) -> None:
        found = self.state.approvals.get(m.session_id, {}).get(m.requirement_id)
        if found is not None:
            found["details"] = m.details
        self.model.emit(
            "chat.approvals.details",
            {"session_id": m.session_id, "requirement_id": m.requirement_id, "details": m.details},
        )

    def _on_streaming_done(self, m: msg.StreamingDone) -> None:
        self.model.emit("chat.streaming_done", _run_ref(m))

    def _on_status_update(self, m: msg.StatusUpdate) -> None:
        status = m.model_dump()
        previous = self.state.status.get(m.session_id)
        self.state.status[m.session_id] = status
        self.model.emit("chat.status_changed", self.model.status_of(status, m.session_id))
        connection = {"cloud_connected": m.cloud_connected, "model_configured": m.model_configured}
        account = self.model.account_of(status)
        if (
            previous is None
            or {
                "cloud_connected": previous.get("cloud_connected"),
                "model_configured": previous.get("model_configured"),
            }
            != connection
        ):
            self.model.emit("app.connection_changed", connection)
        if previous is None or self.model.account_of(previous) != account:
            self.model.emit("account.changed", account)

    def _on_info(self, m: msg.Info) -> None:
        self.model.emit("chat.info", {"session_id": m.session_id, "text": m.text})

    def _on_error(self, m: msg.Error) -> None:
        self.model.emit("chat.error", {"session_id": m.session_id, "text": m.text})

    def _on_tool_started(self, m: msg.ToolStarted) -> None:
        self.model.emit(
            "chat.tool_started",
            {
                **_run_ref(m),
                "tool_name": m.tool_name,
                "friendly_name": m.friendly_name,
                "args_summary": m.args_summary,
            },
        )

    def _on_tool_completed(self, m: msg.ToolCompleted) -> None:
        self.model.emit(
            "chat.tool_completed",
            {
                **_run_ref(m),
                "summary": m.summary,
                "is_error": m.is_error,
                "is_rejected": m.is_rejected,
            },
        )

    def _on_tool_error(self, m: msg.ToolError) -> None:
        self.model.emit("chat.tool_error", {**_run_ref(m), "error": m.error})

    def _on_model_completed(self, m: msg.ModelCompleted) -> None:
        self.model.emit(
            "chat.model_completed",
            {**_run_ref(m), "input_tokens": m.input_tokens, "output_tokens": m.output_tokens},
        )

    def _task(self, m: Any) -> dict[str, Any]:
        return {
            "session_id": m.session_id,
            "task_id": m.task_id,
            "title": getattr(m, "title", ""),
            "assignee": m.assignee,
            "status": m.status,
        }

    def _on_task_created(self, m: msg.TaskCreated) -> None:
        self.model.emit("tasks.created", self._task(m))

    def _on_task_updated(self, m: msg.TaskUpdated) -> None:
        self.model.emit("tasks.updated", self._task(m))

    def _on_task_iteration(self, m: msg.TaskIteration) -> None:
        self.model.emit(
            "tasks.iteration",
            {
                "session_id": m.session_id,
                "iteration": m.iteration,
                "max_iterations": m.max_iterations,
            },
        )

    def _on_task_state_updated(self, m: msg.TaskStateUpdated) -> None:
        self.model.emit(
            "tasks.snapshot",
            {"session_id": m.session_id, "tasks": [t.model_dump() for t in m.tasks]},
        )

    def _on_session_list_result(self, m: msg.SessionListResult) -> None:
        self.model.emit(
            "sessions.listed",
            {"sessions": [self.model.session_info(s.model_dump()) for s in m.sessions]},
        )

    def _on_session_cleared(self, m: msg.SessionCleared) -> None:
        self.model.emit(
            "sessions.cleared",
            {"session_id": m.session_id, "new_session_id": m.new_session_id, "summary": m.summary},
        )

    def _on_scheduler_event(self, m: msg.SchedulerEvent) -> None:
        self.model.emit(
            "scheduler.event",
            {
                "kind": m.event_type,
                "task_id": m.task_id,
                "description": m.description,
                "result": m.result,
            },
        )

    def _on_push_notification(self, m: msg.PushNotification) -> None:
        handler = getattr(self, f"_push_{m.channel}", None)
        if handler is not None:
            handler(m.session_id, dict(m.payload))

    # ── Push channels ─────────────────────────────────────────────

    def _scheduler(self, kind: str, p: dict[str, Any]) -> None:
        self.model.emit(
            "scheduler.event",
            {
                "kind": kind,
                "task_id": str(p.get("task_id", "")),
                "description": str(p.get("description", "")),
                "result": str(p.get("result", "") or p.get("error", "") or ""),
            },
        )

    def _push_scheduler_started(self, sid: str, p: dict[str, Any]) -> None:
        self._scheduler("started", p)

    def _push_scheduler_completed(self, sid: str, p: dict[str, Any]) -> None:
        self._scheduler("completed", p)

    def _push_scheduler_event(self, sid: str, p: dict[str, Any]) -> None:
        self._scheduler(str(p.get("event_type") or "event"), p)

    def _agent(self, sid: str, kind: str, p: dict[str, Any]) -> None:
        self.model.emit("chat.agent_event", {"session_id": sid, "kind": kind, "payload": p})

    def _push_orchestrate_progress(self, sid: str, p: dict[str, Any]) -> None:
        self._agent(sid, "progress", p)

    def _push_orchestrate_event(self, sid: str, p: dict[str, Any]) -> None:
        if p.get("type") == "visualization_delta":
            if p.get("final"):
                self.model.emit("chat.views.shown", self.view(sid, p))
            return
        self._agent(sid, str(p.get("type") or "event"), p)

    @staticmethod
    def view(sid: str, p: dict[str, Any]) -> dict[str, Any]:
        try:
            spec = json.loads(str(p.get("json") or "{}"))
        except json.JSONDecodeError:
            spec = {}
        return {
            "view_id": str(p.get("spec_id", "")),
            "session_id": sid,
            "title": str(p.get("title") or ""),
            "spec": spec,
            "source": str(p.get("source") or "visualizer"),
        }

    def _push_login_status(self, sid: str, p: dict[str, Any]) -> None:
        self._account_later(sid)

    def _push_login_result(self, sid: str, p: dict[str, Any]) -> None:
        self._account_later(sid)

    def _account_later(self, sid: str) -> None:
        if not self.model.bus.wanted("account.changed"):
            return

        async def read() -> None:
            self.model.emit(
                "account.changed",
                await self.model.account(sid or self.model.backend.default_session()),
            )

        self.model.spawn(read())

    def _push_session_named(self, sid: str, p: dict[str, Any]) -> None:
        self.model.emit(
            "sessions.named",
            {"session_id": str(p.get("session_id") or sid), "name": str(p.get("name", ""))},
        )

    def _push_permission_mode_changed(self, sid: str, p: dict[str, Any]) -> None:
        self.model.emit(
            "permissions.mode_changed",
            {"mode": str(p.get("mode", "")), "previous": str(p.get("previous", ""))},
        )

    def _push_output_style_changed(self, sid: str, p: dict[str, Any]) -> None:
        self._display_later(sid)

    def _display_later(self, sid: str) -> None:
        if not self.model.bus.wanted("app.display_changed"):
            return

        async def read() -> None:
            display = await AppMembers(self.model).display_value(
                sid or self.model.backend.default_session()
            )
            self.model.emit("app.display_changed", display)

        self.model.spawn(read())

    def _push_plan_submitted(self, sid: str, p: dict[str, Any]) -> None:
        self.model.emit("plan.changed", {"session_id": sid, "kind": "submitted", "payload": p})

    def _push_plan_decided(self, sid: str, p: dict[str, Any]) -> None:
        self.model.emit("plan.changed", {"session_id": sid, "kind": "decided", "payload": p})

    def _push_file_edited(self, sid: str, p: dict[str, Any]) -> None:
        relative = self._relative(sid, str(p.get("path", "")))
        if relative is None:
            return
        self.state.run_files.setdefault(sid, set()).add(relative)
        value = {"session_id": sid, "path": relative}
        self.model.emit(
            "chat.file_edited",
            value,
            content=lambda grants: value if grants.workspace.permits(relative) else None,
        )

    def _relative(self, sid: str, path: str) -> str | None:
        if not path:
            return None
        try:
            root = self.model.backend.project_dir_of(
                sid or self.model.backend.default_session()
            ).resolve()
            target = Path(path)
            target = target.resolve() if target.is_absolute() else (root / target).resolve()
            return target.relative_to(root).as_posix()
        except (ValueError, OSError):
            # Outside the project: nothing an extension may be told.
            return None

    def _push_notifications_changed(self, sid: str, p: dict[str, Any]) -> None:
        counts = p.get("counts") if isinstance(p.get("counts"), dict) else p
        self.model.emit(
            "notifications.changed",
            None,
            content=lambda grants: self.model.counts_for(grants, counts),
        )

    def _process(self, kind: str, p: dict[str, Any]) -> None:
        self.model.emit("processes.changed", {"kind": kind, "payload": p})

    def _push_process_started(self, sid: str, p: dict[str, Any]) -> None:
        self._process("started", p)

    def _push_process_line(self, sid: str, p: dict[str, Any]) -> None:
        self._process("line", p)

    def _push_process_exited(self, sid: str, p: dict[str, Any]) -> None:
        self._process("exited", p)

    def _push_background_process_done(self, sid: str, p: dict[str, Any]) -> None:
        self._process("done", p)

    def _push_workflow_event(self, sid: str, p: dict[str, Any]) -> None:
        self.model.emit(
            "workflows.changed",
            {"what": str(p.get("type") or "event"), "name": p.get("name"), "payload": p},
        )

    def _push_todos_updated(self, sid: str, p: dict[str, Any]) -> None:
        todos = p.get("todos") if isinstance(p.get("todos"), list) else []
        self.model.emit("tasks.todos_changed", {"session_id": sid, "todos": todos})

    def _push_http_retry_attempt(self, sid: str, p: dict[str, Any]) -> None:
        self.model.emit("chat.retry", {"session_id": sid, "kind": "attempt", "payload": p})

    def _push_http_retry_succeeded(self, sid: str, p: dict[str, Any]) -> None:
        self.model.emit("chat.retry", {"session_id": sid, "kind": "succeeded", "payload": p})

    def _push_visualization_action_dispatched(self, sid: str, p: dict[str, Any]) -> None:
        self.model.emit(
            "chat.views.action",
            {
                "session_id": sid,
                "action": str(p.get("action", "")),
                "params": p.get("params") or {},
            },
        )

    # ── Writes made through RPC ───────────────────────────────────

    def after_rpc(
        self, method: RpcMethod, args: dict[str, Any], session_id: str, result: Any
    ) -> None:
        """A write succeeded — whoever made it. Signal what it changed."""
        name = args.get("name") or args.get("server_name")
        if method is RpcMethod.TOGGLE_VERBOSE:
            self._display_later(session_id)
        elif method is RpcMethod.SWITCH_MODEL:
            self.model.emit("models.changed", {"current": str(args.get("model_name", ""))})
        elif method in _MCP_WRITES:
            self.model.emit(
                "mcp.changed", {"what": _MCP_WRITES[method], "name": name, "payload": None}
            )
        elif method in _PLUGIN_WRITES:
            self.model.emit(
                "plugins.changed",
                {
                    "what": _PLUGIN_WRITES[method],
                    "name": name or args.get("url") or args.get("ref"),
                    "payload": None,
                },
            )
        elif method in (RpcMethod.PROMOTE_EPHEMERAL_AGENT, RpcMethod.DISCARD_EPHEMERAL_AGENT):
            what = "promoted" if method is RpcMethod.PROMOTE_EPHEMERAL_AGENT else "discarded"
            self.model.emit("agents.changed", {"what": what, "name": name, "payload": None})
        elif method is RpcMethod.RELOAD_HOOKS:
            self.model.emit("hooks.changed", {"what": "reloaded", "name": None, "payload": None})
        elif method in _LOOP_WRITES:
            self._loop_later(session_id)
        elif method is RpcMethod.SET_EXTENSION_SETTINGS:
            extension_id = str(args.get("extension_id", ""))
            entry = self.model.registry.get(extension_id)
            if entry is not None:
                values = dict(self.model.store.settings(entry.manifest))
                self.model.emit("self.settings_changed", {"values": values}, only=extension_id)
        elif method in (RpcMethod.CLEAR_CLOUD_CREDENTIALS, RpcMethod.RELOAD_CLOUD_CREDENTIALS):
            self._account_later(session_id)
        elif method is RpcMethod.CANCEL_SCHEDULED_TASK:
            self._scheduler("cancelled", {"task_id": args.get("task_id", "")})
        elif method is RpcMethod.TRUNCATE_HISTORY:
            self.model.emit(
                "chat.history_changed",
                {
                    "session_id": session_id,
                    "what": "truncated",
                    "run_id": str(args.get("run_id", "")),
                },
            )
        elif method is RpcMethod.DISCARD_INTERRUPTED_RUN:
            self.model.emit(
                "chat.history_changed",
                {
                    "session_id": session_id,
                    "what": "discarded",
                    "run_id": str(args.get("message_id", "")),
                },
            )
        elif method is RpcMethod.SWITCH_SESSION:
            self.model.session_switched(str(args.get("session_id", "")), None)

    def _loop_later(self, session_id: str) -> None:
        if not self.model.bus.wanted("loop.changed"):
            return

        async def read() -> None:
            status = await self.model.backend.rpc(RpcMethod.LOOP_STATUS, {}, session_id=session_id)
            self.model.emit("loop.changed", status)

        self.model.spawn(read())


_MCP_WRITES: dict[RpcMethod, str] = {
    RpcMethod.MCP_CONNECT: "connected",
    RpcMethod.MCP_DISCONNECT: "disconnected",
    RpcMethod.SET_MCP_TOOL_ENABLED: "tool_toggled",
    RpcMethod.ENSURE_MCP: "ensured",
}

_PLUGIN_WRITES: dict[RpcMethod, str] = {
    RpcMethod.INSTALL_PLUGIN: "installed",
    RpcMethod.UPDATE_PLUGIN: "updated",
    RpcMethod.REMOVE_PLUGIN: "removed",
    RpcMethod.SET_PLUGIN_ENABLED: "toggled",
    RpcMethod.ADD_MARKETPLACE: "marketplace_added",
    RpcMethod.REMOVE_MARKETPLACE: "marketplace_removed",
    RpcMethod.REFRESH_MARKETPLACES: "marketplaces_refreshed",
}

_LOOP_WRITES = frozenset(
    {
        RpcMethod.LOOP_PAUSE,
        RpcMethod.LOOP_RESUME,
        RpcMethod.CANCEL_PENDING_LOOP,
        RpcMethod.POP_PENDING_LOOP_ITERATION,
    }
)

__all__ = ["BroadcastSignals"]
