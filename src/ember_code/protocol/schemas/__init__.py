"""Protocol wire schemas — split out of :mod:`ember_code.protocol.messages`.

This package holds every Pydantic model in the BE↔FE wire contract,
grouped by concern:

* :mod:`.envelope` — base :class:`Message` + :class:`RunHeader` +
  :class:`RunScopedMessage` mixin.
* :mod:`.enums` — every wire-contract StrEnum (retypes of previously
  free-string fields).
* :mod:`.be_events` — BE → FE run / tool / task / status events.
* :mod:`.mirroring` — multi-client session broadcasts (Welcome,
  Typing, UserMessageReceived, RequirementResolved).
* :mod:`.fe_actions` — FE → BE actions (UserMessage, HITLResponse,
  slash Command, Cancel, session/model switch, …).
* :mod:`.rpc` — process-split RPC (RPCRequest, RPCResponse with
  ``ok`` / ``fail`` factories).
* :mod:`.push` — BE → FE push notifications with typed channel enum
  and per-channel factory helpers.
* :mod:`.internal` — intermediate value objects the BE serializer
  builds and then unpacks into wire messages (:class:`ToolResultData`);
  NOT itself on the wire.

The public export surface lives on this package's ``__all__`` so
:mod:`ember_code.protocol.messages` can re-export the entire wire
contract via ``from ember_code.protocol.schemas import *``. That
star-import is what :class:`ember_code.protocol.registry.MessageRegistry`
reflection-scans for :class:`Message` subclasses at construction
time — every class listed in ``__all__`` here MUST be importable
under the ``messages`` module namespace or registry discovery
drops it and the wire deserializer starts logging
``Unknown message type`` warnings.
"""

from ember_code.protocol.schemas.be_events import (
    CommandResult,
    ContentDelta,
    Error,
    HITLRequest,
    Info,
    ModelCompleted,
    ReasoningStarted,
    RunCompleted,
    RunError,
    RunPaused,
    RunStarted,
    SchedulerEvent,
    SessionCleared,
    SessionListEntry,
    SessionListResult,
    StatusUpdate,
    StreamingDone,
    TaskCreated,
    TaskIteration,
    TaskSnapshot,
    TaskStateUpdated,
    TaskUpdated,
    ToolCompleted,
    ToolError,
    ToolStarted,
)
from ember_code.protocol.schemas.enums import (
    CommandAction,
    CommandResultKind,
    HITLAction,
    HITLChoice,
    OrchestrationTaskStatus,
    PermissionModeName,
    PushChannel,
    SchedulerEventType,
)
from ember_code.protocol.schemas.envelope import Message, RunHeader, RunScopedMessage
from ember_code.protocol.schemas.fe_actions import (
    Cancel,
    CancelLogin,
    Command,
    HITLDecision,
    HITLResponse,
    HITLResponseBatch,
    MCPToggle,
    ModelSwitch,
    QueueMessage,
    SessionList,
    SessionSwitch,
    Shutdown,
    StreamEnd,
    UserMessage,
)
from ember_code.protocol.schemas.internal import ToolResultData
from ember_code.protocol.schemas.mirroring import (
    RequirementResolved,
    Typing,
    UserMessageReceived,
    Welcome,
)
from ember_code.protocol.schemas.push import (
    PushNotification,
    push_background_process_done,
    push_file_edited,
    push_login_result,
    push_login_status,
    push_orchestrate_event,
    push_orchestrate_progress,
    push_permission_mode_changed,
    push_process_exited,
    push_process_line,
    push_process_started,
    push_scheduler_completed,
    push_scheduler_started,
    push_session_named,
)
from ember_code.protocol.schemas.rpc import RPCRequest, RPCResponse

__all__ = [
    # Envelope + shared value objects
    "Message",
    "RunHeader",
    "RunScopedMessage",
    # Enums
    "CommandAction",
    "CommandResultKind",
    "HITLAction",
    "HITLChoice",
    "OrchestrationTaskStatus",
    "PermissionModeName",
    "PushChannel",
    "SchedulerEventType",
    # BE → FE events
    "CommandResult",
    "ContentDelta",
    "Error",
    "HITLRequest",
    "Info",
    "ModelCompleted",
    "ReasoningStarted",
    "RunCompleted",
    "RunError",
    "RunPaused",
    "RunStarted",
    "SchedulerEvent",
    "SessionCleared",
    "SessionListEntry",
    "SessionListResult",
    "StatusUpdate",
    "StreamingDone",
    "TaskCreated",
    "TaskIteration",
    "TaskSnapshot",
    "TaskStateUpdated",
    "TaskUpdated",
    "ToolCompleted",
    "ToolError",
    "ToolStarted",
    # Mirroring
    "RequirementResolved",
    "Typing",
    "UserMessageReceived",
    "Welcome",
    # FE → BE actions
    "Cancel",
    "CancelLogin",
    "Command",
    "HITLDecision",
    "HITLResponse",
    "HITLResponseBatch",
    "MCPToggle",
    "ModelSwitch",
    "QueueMessage",
    "SessionList",
    "SessionSwitch",
    "Shutdown",
    "StreamEnd",
    "UserMessage",
    # RPC
    "RPCRequest",
    "RPCResponse",
    # Push
    "PushNotification",
    "push_background_process_done",
    "push_file_edited",
    "push_login_result",
    "push_login_status",
    "push_orchestrate_event",
    "push_orchestrate_progress",
    "push_permission_mode_changed",
    "push_process_exited",
    "push_process_line",
    "push_process_started",
    "push_scheduler_completed",
    "push_scheduler_started",
    "push_session_named",
    # Internal (not on wire)
    "ToolResultData",
]
