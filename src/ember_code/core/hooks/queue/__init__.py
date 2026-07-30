"""Queue-hook package — bridge user-typed messages into a running agent.

Two hooks work together to make queued messages both **visible to the
model mid-run** and **persisted in the session history as real user
messages**:

1. :class:`QueueInjectorHook` (a ``tool_hook``) drains the queue after
   every tool execution and **appends queued text onto the tool result**.
   The model sees it on its next iteration as part of the just-completed
   tool's output.

2. :class:`QueuePersisterHook` (a ``post_hook``) fires once at run end,
   after ``run_output.messages`` is finalised but before Agno saves the
   session. It adds a proper ``role='user'`` message for each drained
   item, so the conversation history shows what the user typed even
   though the model first encountered it inside a tool result.

Both hooks are manufactured by :class:`QueueBridge`, which owns the
shared :class:`InjectedRunBuffer` they read/write. Usage::

    bridge = QueueBridge(queue=my_queue)
    bridge.register_on(team)

Why two hooks rather than one?

* Agno reads ``agent.additional_input`` only at run-start, so setting
  it mid-run is a no-op.
* ``run_context.messages`` is handed to tool_hooks as a shallow copy
  (Agno reassigns the live list back in a ``finally`` after the hook
  returns), so any list mutations are dropped.
* Tool results, in contrast, flow through Agno's normal append path —
  piggy-backing on them is the only safe mid-run injection point Agno
  exposes.
* ``run_output.messages`` is mutable inside ``post_hooks`` and the
  session save reads from it directly, so that's where persistence
  lives.
"""

from .agno_adapter import AgnoCallableAdapter
from .bridge import QueueBridge
from .buffer import InjectedRunBuffer
from .injector import QueueInjectorHook
from .persister import QueuePersisterHook
from .schemas import (
    USER_NOTE_HEADER,
    InjectedMessage,
    InjectedNote,
    PostHookInvocation,
    QueueCallbacks,
    SupportsRunOutput,
    ToolHookInvocation,
)

__all__ = [
    "USER_NOTE_HEADER",
    "AgnoCallableAdapter",
    "InjectedMessage",
    "InjectedNote",
    "InjectedRunBuffer",
    "PostHookInvocation",
    "QueueBridge",
    "QueueCallbacks",
    "QueueInjectorHook",
    "QueuePersisterHook",
    "SupportsRunOutput",
    "ToolHookInvocation",
]
