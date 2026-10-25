"""Where everything igni can do lives in the host object model.

``docs/HOST_OBJECT_MODEL.md`` makes the model complete by construction:
every RPC method, every action the UI sends and every event the host
broadcasts is a member of the model, or is listed under *Not
reachable* with its reason. This module is that mapping, and
``tests/test_the_host_model_covers_the_app.py`` fails the build when
something new arrives in igni without a line here — so the model
cannot fall behind the app.

A mapping names a member path (``chat.post``). The test checks the
member exists; this module only records the decision.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ember_code.protocol.rpc import RpcMethod
from ember_code.protocol.schemas.enums import PushChannel


class Unreachable(BaseModel):
    """One row of the spec's *Not reachable* table."""

    model_config = ConfigDict(frozen=True)

    key: str
    what: str
    why: str


#: The spec's *Not reachable* table, row for row.
NOT_REACHABLE: tuple[Unreachable, ...] = (
    Unreachable(
        key="credentials",
        what="The person's igni session and any credential (`login`, "
        "`reload_cloud_credentials`, `clear_cloud_credentials`, `set_server`, "
        "`discover_server` values, `cancel_login`)",
        why="An extension never holds the session (service-identity §9). It can start "
        "or end the person's sign-in through `host.account`, but never read what it "
        "produces.",
    ),
    Unreachable(
        key="as_person",
        what="Posting *as the person* (`UserMessage` from an extension)",
        why="Decided: an extension's messages are labelled as the extension's (`host.chat.post`).",
    ),
    Unreachable(
        key="shutdown",
        what="`shutdown`",
        why="Quitting igni is the person's decision.",
    ),
    Unreachable(
        key="channel",
        what="The model's own plumbing: the `host_model` and `window_model` RPCs, and the "
        "`host_signal`, `window_model_changed` and `open_external` pushes",
        why="They are how the model reaches a page and a window, not things igni does.",
    ),
    Unreachable(
        key="service",
        what="A page's token for its own service (`extension_service_token`)",
        why="Not igni's to model: it is how a page reaches its own service "
        "(`bridge.service`), the page's half of what `ext.service` is for the process. "
        "No `permissions.host` grant — having a service is the grant — and the audience "
        "is that extension's own service, never one it names.",
    ),
)

_ROWS = {row.key for row in NOT_REACHABLE}


# ── RPC methods ───────────────────────────────────────────────────

#: Every base RPC method, by the member that reaches it.
RPC: dict[RpcMethod, str] = {
    RpcMethod.ENSURE_MCP: "mcp.ensure",
    RpcMethod.MCP_CONNECT: "mcp.connect",
    RpcMethod.MCP_DISCONNECT: "mcp.disconnect",
    RpcMethod.GET_MCP_STATUS: "mcp.status",
    RpcMethod.GET_MCP_SERVERS: "mcp.servers",
    RpcMethod.GET_MCP_SERVER_DETAILS: "mcp.details",
    RpcMethod.SET_MCP_TOOL_ENABLED: "mcp.set_tool_enabled",
    RpcMethod.GET_SESSION_ID: "chat.session_id",
    RpcMethod.GET_RUN_TIMEOUT: "chat.timeout",
    RpcMethod.GET_STATUS: "chat.status",
    RpcMethod.GET_PROCESSING: "chat.processing",
    RpcMethod.GET_CHAT_HISTORY: "chat.messages",
    RpcMethod.SEARCH_CHAT: "chat.search",
    RpcMethod.GET_PENDING_MESSAGES: "chat.pending",
    RpcMethod.GET_INTERRUPTED_RUNS: "chat.interrupted_runs",
    RpcMethod.DISCARD_INTERRUPTED_RUN: "chat.discard_interrupted",
    RpcMethod.LIST_SESSIONS: "sessions.list",
    RpcMethod.SWITCH_SESSION: "sessions.open",
    RpcMethod.CANCEL_RUN: "chat.cancel",
    RpcMethod.CANCEL_AGENT_RUN: "chat.cancel_agent",
    RpcMethod.COMPACT_IF_NEEDED: "chat.compact",
    RpcMethod.EXTRACT_LEARNINGS: "chat.extract_learnings",
    RpcMethod.POP_PENDING_LOOP_ITERATION: "loop.pop_pending",
    RpcMethod.CANCEL_PENDING_LOOP: "loop.cancel_pending",
    RpcMethod.LOOP_STATUS: "loop.status",
    RpcMethod.LOOP_RESUME: "loop.resume",
    RpcMethod.LOOP_PAUSE: "loop.pause",
    RpcMethod.GET_SKILL_NAMES: "commands.skills",
    RpcMethod.GET_SKILL_DEFINITIONS: "commands.skill_definitions",
    RpcMethod.GET_SLASH_COMMANDS: "commands.list",
    RpcMethod.GET_TODOS: "tasks.todos",
    RpcMethod.DISPATCH_VISUALIZATION_ACTION: "chat.views.dispatch",
    RpcMethod.LIST_BACKGROUND_PROCESSES: "processes.list",
    RpcMethod.READ_PROCESS_TAIL: "processes.tail",
    RpcMethod.STOP_BACKGROUND_PROCESS: "processes.stop",
    RpcMethod.GET_LATEST_PLAN: "plan.latest",
    RpcMethod.APPROVE_PLAN: "plan.approve",
    RpcMethod.DISMISS_PLAN: "plan.dismiss",
    RpcMethod.GET_OUTPUT_STYLES: "app.display",
    RpcMethod.UPLOAD_ATTACHMENT: "chat.upload",
    RpcMethod.FIRE_SESSION_START_HOOK: "hooks.fire_session_start",
    RpcMethod.GET_HOOKS_DETAILS: "hooks.details",
    RpcMethod.RELOAD_HOOKS: "hooks.reload",
    RpcMethod.START_SCHEDULER: "scheduler.start",
    RpcMethod.EXECUTE_SCHEDULED_TASK: "scheduler.run_now",
    RpcMethod.CANCEL_SCHEDULED_TASK: "scheduler.cancel",
    RpcMethod.GET_SCHEDULED_TASKS: "scheduler.tasks",
    RpcMethod.SWITCH_MODEL: "models.current",
    RpcMethod.GET_MODEL_REGISTRY: "models.registry",
    RpcMethod.GET_DISPLAY_CONFIG: "app.display",
    RpcMethod.GET_GROUP_POLICY: "policy.pack",
    RpcMethod.TOGGLE_VERBOSE: "app.set_verbose",
    RpcMethod.CHECK_PERMISSION: "permissions.check",
    RpcMethod.SAVE_PERMISSION_RULE: "permissions.save_rule",
    RpcMethod.CHECK_FOR_UPDATE: "app.check_for_update",
    RpcMethod.COMPLETE_FILES: "workspace.complete",
    RpcMethod.RUN_SHELL: "processes.run",
    RpcMethod.ATTACH_SESSION: "sessions.attach",
    RpcMethod.LIST_DIRS: "workspace.list",
    RpcMethod.PICK_DIR_NATIVE: "workspace.pick_dir",
    RpcMethod.GET_PROJECT_DIR: "workspace.root",
    RpcMethod.GET_AGENT_DETAILS: "agents.details",
    RpcMethod.PROMOTE_EPHEMERAL_AGENT: "agents.promote_ephemeral",
    RpcMethod.DISCARD_EPHEMERAL_AGENT: "agents.discard_ephemeral",
    RpcMethod.GET_SKILL_DETAILS: "commands.skill_details",
    RpcMethod.READ_FILE: "workspace.read",
    RpcMethod.TRUNCATE_HISTORY: "chat.truncate",
    RpcMethod.SEARCH_CODE: "workspace.search",
    RpcMethod.COUNT_CONTEXT_TOKENS: "chat.context",
    RpcMethod.GET_CONTEXT_BREAKDOWN: "chat.context",
    RpcMethod.LIST_WORKFLOWS: "workflows.list",
    RpcMethod.RUN_WORKFLOW: "workflows.run",
    RpcMethod.CANCEL_WORKFLOW: "workflows.cancel",
    RpcMethod.GET_PLUGIN_DETAILS: "plugins.installed",
    RpcMethod.GET_PLUGIN_CONTENTS: "plugins.contents",
    RpcMethod.PREVIEW_PLUGIN: "plugins.preview",
    RpcMethod.SET_PLUGIN_ENABLED: "plugins.set_enabled",
    RpcMethod.INSTALL_PLUGIN: "plugins.install",
    RpcMethod.UPDATE_PLUGIN: "plugins.update",
    RpcMethod.REMOVE_PLUGIN: "plugins.remove",
    RpcMethod.GET_MARKETPLACES: "plugins.marketplaces",
    RpcMethod.ADD_MARKETPLACE: "plugins.add_marketplace",
    RpcMethod.REMOVE_MARKETPLACE: "plugins.remove_marketplace",
    RpcMethod.REFRESH_MARKETPLACES: "plugins.refresh_marketplaces",
    RpcMethod.LIST_EXTENSIONS: "extensions.installed",
    RpcMethod.SET_EXTENSION_SETTINGS: "extensions.set_settings",
    RpcMethod.NOTIFICATIONS_LIST: "notifications.list",
    RpcMethod.NOTIFICATIONS_COUNTS: "notifications.unread",
    RpcMethod.NOTIFICATIONS_MARK_READ: "notifications.mark_read",
}

#: RPC methods no member reaches, by the *Not reachable* row that says why.
RPC_UNREACHABLE: dict[RpcMethod, str] = {
    RpcMethod.LOGIN: "credentials",
    RpcMethod.DISCOVER_SERVER: "credentials",
    RpcMethod.SET_SERVER: "credentials",
    RpcMethod.RELOAD_CLOUD_CREDENTIALS: "credentials",
    RpcMethod.CLEAR_CLOUD_CREDENTIALS: "credentials",
    RpcMethod.SHUTDOWN: "shutdown",
    RpcMethod.HOST_MODEL: "channel",
    RpcMethod.WINDOW_MODEL: "channel",
    RpcMethod.EXTENSION_SERVICE_TOKEN: "service",
}


def rpc_destination(method: RpcMethod) -> tuple[str, str] | None:
    """``("member", path)`` or ``("unreachable", row)``; ``None`` if unmapped."""
    if method in RPC:
        return ("member", RPC[method])
    if method in RPC_UNREACHABLE:
        return ("unreachable", RPC_UNREACHABLE[method])
    return None


# ── UI actions (fe_actions) ───────────────────────────────────────

#: Every action the UI sends, by its wire ``type``.
FE_ACTIONS: dict[str, str] = {
    "queue_message": "chat.queue",
    "hitl_response": "chat.approvals.answer",
    "hitl_decision": "chat.approvals.answer_all",
    "hitl_response_batch": "chat.approvals.answer_all",
    "command": "commands.run",
    "cancel": "chat.cancel",
    "session_switch": "sessions.open",
    "session_list": "sessions.list",
    "model_switch": "models.current",
    "mcp_toggle": "mcp.connect",
    "stream_end": "chat.streaming_done",
}

FE_ACTIONS_UNREACHABLE: dict[str, str] = {
    "user_message": "as_person",
    "cancel_login": "credentials",
    "shutdown": "shutdown",
}


# ── Broadcasts (be_events, mirroring) ─────────────────────────────

#: Every message the host sends a window, by its wire ``type``.
BROADCASTS: dict[str, str] = {
    # be_events
    "content_delta": "chat.content_delta",
    "tool_started": "chat.tool_started",
    "tool_completed": "chat.tool_completed",
    "tool_error": "chat.tool_error",
    "model_completed": "chat.model_completed",
    "run_started": "chat.run_started",
    "run_completed": "chat.run_completed",
    "streaming_done": "chat.streaming_done",
    "run_error": "chat.run_error",
    "reasoning_started": "chat.reasoning_started",
    "hitl_request": "chat.approvals.requested",
    "task_created": "tasks.created",
    "task_updated": "tasks.updated",
    "task_iteration": "tasks.iteration",
    "task_state_updated": "tasks.snapshot",
    "command_result": "commands.run",
    "status_update": "chat.status_changed",
    "session_list_result": "sessions.listed",
    "session_cleared": "sessions.cleared",
    "info": "chat.info",
    "error": "chat.error",
    "scheduler_event": "scheduler.event",
    "run_paused": "chat.run_paused",
    # mirroring
    "welcome": "windows.opened",
    "typing": "chat.typing",
    "user_message_received": "chat.message_added",
    "requirement_resolved": "chat.approvals.resolved",
    "requirement_details": "chat.approvals.details",
}

#: The value objects that travel *inside* a broadcast rather than as
#: one, by class name. ``TaskSnapshot`` rides ``task_state_updated``,
#: ``SessionListEntry`` rides ``session_list_result``.
BROADCAST_PARTS: dict[str, str] = {
    "TaskSnapshot": "tasks.snapshot",
    "SessionListEntry": "sessions.list",
}


# ── Push channels ─────────────────────────────────────────────────

PUSH: dict[PushChannel, str] = {
    PushChannel.SCHEDULER_STARTED: "scheduler.event",
    PushChannel.SCHEDULER_COMPLETED: "scheduler.event",
    PushChannel.SCHEDULER_EVENT: "scheduler.event",
    PushChannel.ORCHESTRATE_PROGRESS: "chat.agent_event",
    PushChannel.ORCHESTRATE_EVENT: "chat.agent_event",
    PushChannel.LOGIN_STATUS: "account.changed",
    PushChannel.LOGIN_RESULT: "account.changed",
    PushChannel.SESSION_NAMED: "sessions.named",
    PushChannel.PERMISSION_MODE_CHANGED: "permissions.mode_changed",
    PushChannel.OUTPUT_STYLE_CHANGED: "app.display_changed",
    PushChannel.PLAN_SUBMITTED: "plan.changed",
    PushChannel.FILE_EDITED: "chat.file_edited",
    PushChannel.NOTIFICATIONS_CHANGED: "notifications.changed",
    PushChannel.PROCESS_STARTED: "processes.changed",
    PushChannel.PROCESS_LINE: "processes.changed",
    PushChannel.PROCESS_EXITED: "processes.changed",
    PushChannel.BACKGROUND_PROCESS_DONE: "processes.changed",
    PushChannel.WORKFLOW_EVENT: "workflows.changed",
    PushChannel.TODOS_UPDATED: "tasks.todos_changed",
    PushChannel.PLAN_DECIDED: "plan.changed",
    PushChannel.HTTP_RETRY_ATTEMPT: "chat.retry",
    PushChannel.HTTP_RETRY_SUCCEEDED: "chat.retry",
    PushChannel.VISUALIZATION_ACTION_DISPATCHED: "chat.views.action",
}

PUSH_UNREACHABLE: dict[PushChannel, str] = {
    PushChannel.HOST_SIGNAL: "channel",
    PushChannel.OPEN_EXTERNAL: "channel",
    PushChannel.WINDOW_MODEL_CHANGED: "channel",
}

#: ``UNKNOWN`` is the enum's catch-all for a channel nobody listed,
#: not a channel; the coverage test skips it.
PUSH_NOT_A_CHANNEL = frozenset({PushChannel.UNKNOWN})


def rows() -> set[str]:
    return set(_ROWS)


__all__ = [
    "BROADCASTS",
    "BROADCAST_PARTS",
    "FE_ACTIONS",
    "FE_ACTIONS_UNREACHABLE",
    "NOT_REACHABLE",
    "PUSH",
    "PUSH_NOT_A_CHANNEL",
    "PUSH_UNREACHABLE",
    "RPC",
    "RPC_UNREACHABLE",
    "Unreachable",
    "rows",
    "rpc_destination",
]
