"""BE → FE messages — run / tool / task / status / command events.

Every message here retypes previously-free-string fields into
:mod:`.enums` StrEnum members. Wire strings are unchanged; the
producers gain type-checker help.

Run-scoped events inherit :class:`RunScopedMessage` from
:mod:`.envelope` so ``run_id`` + ``parent_run_id`` live on one
base class (flat on the wire, so no FE client breaks).

TODO — ``HITLRequest.tool_args`` stays ``dict[str, Any]`` for
now. Tightening to a shared ``ToolInvocationArgs`` model would
also close over :class:`ToolStarted.args_summary`,
:class:`ToolCompleted.summary`, and the Agno tool-arg surface —
deferred until Agno's tool-arg schema stabilises.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ember_code.protocol.schemas.enums import (
    CommandAction,
    CommandResultKind,
    OrchestrationTaskStatus,
)
from ember_code.protocol.schemas.envelope import Message, RunScopedMessage

# Note: :class:`PermissionModeName` (for ``StatusUpdate.permission_mode``)
# and :class:`SchedulerEventType` (for ``SchedulerEvent.event_type``)
# are referenced by field docstrings but not imported here —
# those fields stay ``str`` on the wire for forward-compat.
# Producers import the enums directly from :mod:`.enums`.


class ContentDelta(Message):
    """Streamed text chunk from the model."""

    type: Literal["content_delta"] = "content_delta"
    text: str = ""
    is_thinking: bool = False


class ToolStarted(RunScopedMessage):
    """A tool call has begun."""

    type: Literal["tool_started"] = "tool_started"
    tool_name: str = ""
    friendly_name: str = ""
    args_summary: str = ""


class ToolCompleted(RunScopedMessage):
    """A tool call has finished."""

    type: Literal["tool_completed"] = "tool_completed"
    summary: str = ""
    full_result: str = ""
    has_markup: bool = False
    diff_rows: list[tuple[str, str]] | None = None  # (text, style) pairs for diff table
    # True when the tool returned an error payload (raised exception,
    # or returned a string starting with ``"Error:"`` — the convention
    # used by ember-code's own tools). The TUI renders ``✗`` with red
    # styling instead of ``✓`` so the user can see at a glance which
    # calls in a batch actually failed. Without this, an ``Edit`` that
    # returned ``"Error: old_string not found"`` was displayed with
    # a green checkmark, the user assumed success, and only the LLM
    # saw the failure — and tried to retry silently.
    is_error: bool = False


class ToolError(RunScopedMessage):
    """A tool call failed."""

    type: Literal["tool_error"] = "tool_error"
    error: str = ""


class ModelCompleted(RunScopedMessage):
    """Model finished — token counts available."""

    type: Literal["model_completed"] = "model_completed"
    input_tokens: int = 0
    output_tokens: int = 0


class RunStarted(RunScopedMessage):
    """An agent/team run has begun."""

    type: Literal["run_started"] = "run_started"
    agent_name: str = ""
    model: str = ""


class RunCompleted(RunScopedMessage):
    """An agent/team run has finished."""

    type: Literal["run_completed"] = "run_completed"
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    """Subset of ``output_tokens`` consumed by the model's reasoning
    chain (e.g. MiniMax-M2.7 'thinking'). The visible reply tokens are
    ``output_tokens - reasoning_tokens``. Surfaced as a separate field
    so the FE can split the stats line into ``N think · M out`` rather
    than conflating both as a single ``out`` number."""
    duration: float = 0.0
    """Run wall-clock seconds, from Agno's run metrics."""


class StreamingDone(RunScopedMessage):
    """Emitted by the BE when the model's content stream has finished
    but the run's post-stream tail (compression, memory, persistence,
    metrics) is still draining inside Agno. The FE uses this to
    optimistically unblock user input — the agent is *logically* done
    from the user's POV even though the backend stream stays open for
    several more seconds. Without this event, ``_processing`` stays
    True for the entire Agno tail and the queue panel hangs around
    long after the response is visible.

    The BE still serialises subsequent ``run_message`` calls behind an
    internal lock so the next run can't start until the previous tail
    finishes — but that wait is invisible to the user, who sees the
    normal "Thinking" UI as soon as they submit.
    """

    type: Literal["streaming_done"] = "streaming_done"


class RunError(Message):
    """Run-level error."""

    type: Literal["run_error"] = "run_error"
    error: str = ""


class ReasoningStarted(RunScopedMessage):
    """Model entered reasoning/thinking phase."""

    type: Literal["reasoning_started"] = "reasoning_started"


class HITLRequest(Message):
    """Permission dialog needed — BE pauses until FE responds."""

    type: Literal["hitl_request"] = "hitl_request"
    requirement_id: str = ""
    tool_name: str = ""
    friendly_name: str = ""
    # ``tool_args`` stays ``dict[str, Any]`` because it carries
    # tool-invocation payloads produced by Agno / third-party MCP
    # servers that we don't control the schema of. Tightening this
    # to a shared ``ToolInvocationArgs`` model is a follow-up (would
    # need to be shared with ``ToolStarted`` / ``ToolCompleted``
    # and depends on a closed Agno tool-arg surface — deferred).
    tool_args: dict[str, Any] = Field(default_factory=dict)
    details: str = ""
    # Chain of agents that produced this request, parent → leaf.
    # Empty / single-entry means it's from the main agent. For sub-agent
    # HITL it's the dispatch path, e.g. ["architect"] when the main agent
    # spawned the architect, or ["architect", "reviewer"] if the architect
    # then spawned a reviewer that asked for shell access.
    agent_path: list[str] = Field(default_factory=list)

    @classmethod
    def from_agno_requirement(
        cls,
        requirement: Any,
        friendly_name_lookup: Any,
    ) -> HITLRequest:
        """Build a ``HITLRequest`` from an Agno pause requirement.

        ``friendly_name_lookup`` is any callable with the shape
        ``(tool_name: str, default: str = "") -> str`` — in practice
        :meth:`AgnoToolEventFormatter.friendly_name`. Injected rather
        than imported so the protocol layer stays leaf (no Agno /
        formatter imports at module scope).
        """
        tool_exec = getattr(requirement, "tool_execution", None)
        raw_name = str(getattr(tool_exec, "tool_name", "") if tool_exec else "")
        return cls(
            requirement_id=str(id(requirement)),
            tool_name=raw_name,
            friendly_name=friendly_name_lookup(raw_name, default=""),
            tool_args=dict(getattr(tool_exec, "tool_args", {}) if tool_exec else {}),
        )


class TaskCreated(Message):
    """Orchestration task created.

    ``status`` is a raw ``str`` on the wire; producers SHOULD use
    :class:`OrchestrationTaskStatus` members (which coerce to their
    string values via ``StrEnum``). Kept as ``str`` rather than a
    strict enum so an Agno-emitted status we haven't yet enumerated
    survives verbatim through Pydantic instead of being rewritten
    to ``"unknown"`` by the enum's ``_missing_`` fallback.
    """

    type: Literal["task_created"] = "task_created"
    task_id: str = ""
    title: str = ""
    assignee: str = ""
    status: str = OrchestrationTaskStatus.PENDING.value


class TaskUpdated(Message):
    """Orchestration task status changed.

    See :class:`TaskCreated` — ``status`` is a raw ``str`` for
    forward-compat with un-enumerated Agno statuses.
    """

    type: Literal["task_updated"] = "task_updated"
    task_id: str = ""
    status: str = ""
    assignee: str = ""


class TaskIteration(Message):
    """Task iteration progress."""

    type: Literal["task_iteration"] = "task_iteration"
    iteration: int = 0
    max_iterations: int = 0


class TaskSnapshot(BaseModel):
    """One task row inside a :class:`TaskStateUpdated` batch.

    Pre-refactor, ``TaskStateUpdated.tasks`` was
    ``list[dict[str, Any]]`` — an untyped bag that every callsite
    had to remember the keys of (``task_id`` / ``title`` /
    ``status`` / ``assignee``). Tightening it to this schema is
    wire-compatible: Pydantic serialises ``TaskSnapshot`` to the
    exact same JSON dict, and Pydantic's dict-coercion means
    ``TaskStateUpdated(tasks=[{...}])`` still works for callers
    that pass raw dicts.
    """

    task_id: str = ""
    title: str = ""
    status: str = ""
    assignee: str = ""

    @classmethod
    def from_agno(cls, task: Any) -> TaskSnapshot:
        """Extract a snapshot from an Agno task object.

        Reads via ``getattr`` so this stays tolerant of the Agno
        team-event task shape (which is a dataclass in current
        versions but has changed shape historically). ``status``
        is a raw ``str`` — producers can compare to
        :class:`OrchestrationTaskStatus` members for typed
        dispatch, but the wire preserves the original Agno
        string verbatim.
        """
        return cls(
            task_id=str(getattr(task, "task_id", "")),
            title=str(getattr(task, "title", "")),
            status=str(getattr(task, "status", "")),
            assignee=str(getattr(task, "assignee", "") or ""),
        )


class TaskStateUpdated(Message):
    """Batch task state update."""

    type: Literal["task_state_updated"] = "task_state_updated"
    tasks: list[TaskSnapshot] = Field(default_factory=list)


class CommandResult(Message):
    """Result of a slash command.

    Canonical wire model. The single Python subclass
    :class:`ember_code.backend.command_result.CommandResult` inherits
    from this class, adds the semantic + generic classmethod factories
    (``markdown`` / ``info`` / ``error`` / ``fork`` / ``for_action``),
    and hangs the behaviour methods (``is_error`` / ``is_action`` /
    ``render_line``) on it — the backend class IS the wire model, so
    ``server.handle_command`` returns it directly instead of rebuilding
    a twin. See that module for the layering rationale (protocol is
    the leaf layer; backend depends on protocol, never vice versa).

    Fields are enum-typed (``CommandResultKind`` / ``CommandAction``)
    rather than free strings — Pydantic still serialises them to their
    string values on the wire, so FE parsers that key off literals
    like ``"markdown"`` / ``"quit"`` are unaffected. Producers gain
    autocompletion and typo-checking; a raw ``kind="markdon"`` now
    fails Pydantic validation instead of silently rendering as
    generic-info on the FE.

    ``display_content`` is the optional chat-render override for the
    ``run_prompt`` action: the loop slash command sets it to the
    unwrapped prompt while ``content`` carries the wrapped form
    (with the ``<loop-iteration>`` meta tag) for the agent. When
    empty, the FE displays ``content`` directly — the normal case
    for skill prompts and any other ``run_prompt`` action.
    """

    type: Literal["command_result"] = "command_result"
    kind: CommandResultKind = CommandResultKind.INFO
    content: str = ""
    action: CommandAction = CommandAction.NONE
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
    # Active ``PermissionEvaluator`` mode (``default`` / ``plan`` /
    # ``acceptEdits`` / ``bypassPermissions`` / ``dontAsk``). The
    # FE renders a badge when this is ``plan`` (row 50 — plan-mode
    # UI). Pushed on every status update + via the
    # ``permission_mode_changed`` push when the slash command
    # toggles it mid-session.
    # ``permission_mode`` stays a raw ``str`` on the wire; producers
    # SHOULD use :class:`PermissionModeName` members (which mirror
    # :class:`ember_code.core.config.permission_eval.PermissionMode`).
    # Kept as ``str`` for forward-compat with any future mode we
    # haven't yet enumerated.
    permission_mode: str = "default"


class SessionListEntry(BaseModel):
    """One row of :attr:`SessionListResult.sessions`.

    Wire shape produced by
    :meth:`~ember_code.core.session.schemas.SessionListRow.from_agno`:
    ``{session_id, name, created_at, updated_at, run_count, summary,
    agent_name}``. Kept permissive (``extra='allow'``) so a future
    field on the persistence side lands on the wire without a
    schema-drift error at the boundary — the FE tolerates unknown
    keys.

    Named ``SessionListEntry`` rather than ``SessionSummary`` to
    avoid collision with Agno's ``agno.session.summary.SessionSummary``,
    which is used in
    :mod:`ember_code.core.session.compaction`. Defined in this
    module (not ``backend/schemas_history.py``) because ``protocol``
    is the leaf layer — the backend depends on protocol, never
    the reverse — and :class:`SessionListResult` needs to reference
    this schema at the wire boundary.
    """

    model_config = ConfigDict(extra="allow")

    session_id: str = ""
    name: str = ""
    created_at: int = 0
    updated_at: int = 0
    run_count: int = 0
    summary: str = ""
    agent_name: str = ""


class SessionListResult(Message):
    """Response to session_list request."""

    type: Literal["session_list_result"] = "session_list_result"
    sessions: list[SessionListEntry] = Field(default_factory=list)


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
    """Notification about a scheduled task.

    ``event_type`` stays a raw ``str`` on the wire; producers
    SHOULD use :class:`SchedulerEventType` members (``STARTED`` /
    ``COMPLETED`` / ``FAILED`` / ``ERROR``). Kept as ``str`` for
    forward-compat with any un-enumerated event kind Agno / a
    future scheduler emits.
    """

    type: Literal["scheduler_event"] = "scheduler_event"
    task_id: str = ""
    description: str = ""
    event_type: str = ""  # see :class:`SchedulerEventType`
    result: str = ""


class RunPaused(RunScopedMessage):
    """Run paused for HITL — wraps hitl_request with run context."""

    type: Literal["run_paused"] = "run_paused"
    requirements: list[HITLRequest] = Field(default_factory=list)


__all__ = [
    "ContentDelta",
    "ToolStarted",
    "ToolCompleted",
    "ToolError",
    "ModelCompleted",
    "RunStarted",
    "RunCompleted",
    "StreamingDone",
    "RunError",
    "ReasoningStarted",
    "HITLRequest",
    "TaskCreated",
    "TaskUpdated",
    "TaskIteration",
    "TaskSnapshot",
    "TaskStateUpdated",
    "CommandResult",
    "StatusUpdate",
    "SessionListEntry",
    "SessionListResult",
    "SessionCleared",
    "Info",
    "Error",
    "SchedulerEvent",
    "RunPaused",
]
