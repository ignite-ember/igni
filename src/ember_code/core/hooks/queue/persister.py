"""Post_hook that persists drained queue messages as user history.

Fires once per run, after ``run_output.messages`` is built but before
the session is saved. Reads the shared
:class:`~ember_code.core.hooks.queue.buffer.InjectedRunBuffer` and
appends a proper ``role='user'`` message per drained item — so the
conversation timeline preserves what the user typed even though the
model first saw it embedded in a tool result.

The persister no longer holds an injector reference. It holds the
shared buffer directly; both hooks are constructed with the same
buffer instance by
:class:`~ember_code.core.hooks.queue.bridge.QueueBridge`.
"""

from __future__ import annotations

from typing import Any

from agno.models.message import Message

from .agno_adapter import AgnoCallableAdapter
from .buffer import InjectedRunBuffer
from .schemas import PostHookInvocation, SupportsRunOutput


class QueuePersisterHook(AgnoCallableAdapter):
    """Agno post_hook that persists drained queue messages as user history."""

    def __init__(self, buffer: InjectedRunBuffer) -> None:
        self._buffer = buffer

    def __call__(self, run_output: Any = None, **_kwargs: Any) -> None:
        """Hook entry point — Agno calls this once per run.

        NOTE: the parameter name ``run_output`` is pinned; Agno's
        ``_build_hook_args`` matches by exact name.
        """
        invocation = PostHookInvocation(run_output=run_output, extra=_kwargs)
        self._append_user_messages(invocation.run_output)

    def _append_user_messages(self, run_output: Any) -> None:
        """Turn each buffered :class:`InjectedMessage` into a user
        ``Message`` on ``run_output.messages``, then clear the buffer.

        Buffer is cleared only after successful persistence so a raise
        mid-append leaves data intact for a retry.
        """
        if self._buffer.is_empty() or run_output is None:
            # No run_output to write onto (rare — mostly tests) —
            # still clear so the next run starts empty.
            self._buffer.clear()
            return

        if not isinstance(run_output, SupportsRunOutput):
            self._buffer.clear()
            return
        if run_output.messages is None:
            run_output.messages = []
        for message in self._buffer.snapshot():
            run_output.messages.append(
                Message(
                    role="user",
                    content=message.text,
                    add_to_agent_memory=True,
                )
            )
        self._buffer.clear()


__all__ = ["QueuePersisterHook"]
