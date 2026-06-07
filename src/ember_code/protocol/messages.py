"""Protocol message types for BE↔FE communication.

Each message is a Pydantic model with plain types (str, int, bool, list, dict).
No Agno imports — these are the contract between processes.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ── Base envelope ─────────────────────────────────────────────────────


class Message(BaseModel):
    """Base envelope for all protocol messages."""

    type: str
    id: str = ""  # optional correlation ID


# ── BE → FE messages ─────────────────────────────────────────────────


class ContentDelta(Message):
    """Streamed text chunk from the model."""

    type: Literal["content_delta"] = "content_delta"
    text: str = ""
    is_thinking: bool = False


class ToolStarted(Message):
    """A tool call has begun."""

    type: Literal["tool_started"] = "tool_started"
    tool_name: str = ""
    friendly_name: str = ""
    args_summary: str = ""
    run_id: str = ""


class ToolCompleted(Message):
    """A tool call has finished."""

    type: Literal["tool_completed"] = "tool_completed"
    summary: str = ""
    full_result: str = ""
    has_markup: bool = False
    diff_rows: list[tuple[str, str]] | None = None  # (text, style) pairs for diff table
    run_id: str = ""


class ToolError(Message):
    """A tool call failed."""

    type: Literal["tool_error"] = "tool_error"
    error: str = ""
    run_id: str = ""


class ModelCompleted(Message):
    """Model finished — token counts available."""

    type: Literal["model_completed"] = "model_completed"
    input_tokens: int = 0
    output_tokens: int = 0
    run_id: str = ""
    parent_run_id: str = ""


class RunStarted(Message):
    """An agent/team run has begun."""

    type: Literal["run_started"] = "run_started"
    agent_name: str = ""
    run_id: str = ""
    parent_run_id: str = ""
    model: str = ""


class RunCompleted(Message):
    """An agent/team run has finished."""

    type: Literal["run_completed"] = "run_completed"
    run_id: str = ""
    parent_run_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


class RunError(Message):
    """Run-level error."""

    type: Literal["run_error"] = "run_error"
    error: str = ""


class ReasoningStarted(Message):
    """Model entered reasoning/thinking phase."""

    type: Literal["reasoning_started"] = "reasoning_started"
    run_id: str = ""


class HITLRequest(Message):
    """Permission dialog needed — BE pauses until FE responds."""

    type: Literal["hitl_request"] = "hitl_request"
    requirement_id: str = ""
    tool_name: str = ""
    friendly_name: str = ""
    tool_args: dict[str, Any] = Field(default_factory=dict)
    details: str = ""
    # Chain of agents that produced this request, parent → leaf.
    # Empty / single-entry means it's from the main agent. For sub-agent
    # HITL it's the dispatch path, e.g. ["architect"] when the main agent
    # spawned the architect, or ["architect", "reviewer"] if the architect
    # then spawned a reviewer that asked for shell access.
    agent_path: list[str] = Field(default_factory=list)


class TaskCreated(Message):
    """Orchestration task created."""

    type: Literal["task_created"] = "task_created"
    task_id: str = ""
    title: str = ""
    assignee: str = ""
    status: str = "pending"


class TaskUpdated(Message):
    """Orchestration task status changed."""

    type: Literal["task_updated"] = "task_updated"
    task_id: str = ""
    status: str = ""
    assignee: str = ""


class TaskIteration(Message):
    """Task iteration progress."""

    type: Literal["task_iteration"] = "task_iteration"
    iteration: int = 0
    max_iterations: int = 0


class TaskStateUpdated(Message):
    """Batch task state update."""

    type: Literal["task_state_updated"] = "task_state_updated"
    tasks: list[dict[str, Any]] = Field(default_factory=list)


class CommandResult(Message):
    """Result of a slash command."""

    type: Literal["command_result"] = "command_result"
    kind: str = "info"  # "markdown", "info", "error"
    content: str = ""
    action: str = ""  # "quit", "clear", "login", "schedule", etc.
    # Optional override for what to render in chat when ``action ==
    # "run_prompt"``. The loop slash command sets this to the
    # unwrapped prompt while ``content`` carries the wrapped form
    # (with the ``<loop-iteration>`` meta tag) for the agent. When
    # empty, the FE displays ``content`` directly — the normal
    # case for skill prompts and any other ``run_prompt`` action.
    display_content: str = ""


class StatusUpdate(Message):
    """Status bar data pushed from BE."""

    type: Literal["status_update"] = "status_update"
    input_tokens: int = 0
    output_tokens: int = 0
    context_tokens: int = 0
    max_context: int = 0
    model: str = ""
    cloud_connected: bool = False
    cloud_org: str = ""


class SessionListResult(Message):
    """Response to session_list request."""

    type: Literal["session_list_result"] = "session_list_result"
    sessions: list[dict[str, Any]] = Field(default_factory=list)


class SessionCleared(Message):
    """Session was compacted or cleared."""

    type: Literal["session_cleared"] = "session_cleared"
    new_session_id: str = ""
    summary: str = ""


class Info(Message):
    """Informational message."""

    type: Literal["info"] = "info"
    text: str = ""


class Error(Message):
    """Error message."""

    type: Literal["error"] = "error"
    text: str = ""


class SchedulerEvent(Message):
    """Notification about a scheduled task."""

    type: Literal["scheduler_event"] = "scheduler_event"
    task_id: str = ""
    description: str = ""
    event_type: str = ""  # "started", "completed", "failed"
    result: str = ""


class RunPaused(Message):
    """Run paused for HITL — wraps hitl_request with run context."""

    type: Literal["run_paused"] = "run_paused"
    run_id: str = ""
    requirements: list[HITLRequest] = Field(default_factory=list)


# ── FE → BE messages ─────────────────────────────────────────────────


class UserMessage(Message):
    """User sends a chat message."""

    type: Literal["user_message"] = "user_message"
    text: str = ""
    file_contents: dict[str, str] = Field(default_factory=dict)  # path → content


class QueueMessage(Message):
    """User types while agent is running."""

    type: Literal["queue_message"] = "queue_message"
    text: str = ""


class HITLResponse(Message):
    """User responded to a permission dialog."""

    type: Literal["hitl_response"] = "hitl_response"
    requirement_id: str = ""
    action: str = ""  # "confirm" | "reject"
    choice: str = ""  # "once" | "always" | "similar"


class Command(Message):
    """Slash command from user."""

    type: Literal["command"] = "command"
    text: str = ""


class Cancel(Message):
    """Cancel current run."""

    type: Literal["cancel"] = "cancel"


class CancelLogin(Message):
    """Cancel an in-progress login flow."""

    type: Literal["cancel_login"] = "cancel_login"


class SessionSwitch(Message):
    """Switch to a different session."""

    type: Literal["session_switch"] = "session_switch"
    session_id: str = ""


class SessionList(Message):
    """Request session list."""

    type: Literal["session_list"] = "session_list"


class ModelSwitch(Message):
    """Switch model."""

    type: Literal["model_switch"] = "model_switch"
    model_name: str = ""


class MCPToggle(Message):
    """Toggle MCP server connection."""

    type: Literal["mcp_toggle"] = "mcp_toggle"
    server_name: str = ""
    connect: bool = True


class Shutdown(Message):
    """Graceful shutdown."""

    type: Literal["shutdown"] = "shutdown"


# ── Process-split protocol ──────────────────────────────────────────


class StreamEnd(Message):
    """Marks end of a streaming response (run_message, resolve_hitl)."""

    type: Literal["stream_end"] = "stream_end"


class RPCRequest(Message):
    """Generic RPC call for accessor/utility methods."""

    type: Literal["rpc_request"] = "rpc_request"
    method: str = ""
    args: dict[str, Any] = Field(default_factory=dict)


class RPCResponse(Message):
    """Response to an RPCRequest."""

    type: Literal["rpc_response"] = "rpc_response"
    result: Any = None
    error: str | None = None


class PushNotification(Message):
    """BE→FE push for callbacks (scheduler, progress, login status)."""

    type: Literal["push_notification"] = "push_notification"
    channel: str = ""  # "scheduler_event", "orchestrate_progress", "login_status"
    payload: dict[str, Any] = Field(default_factory=dict)
