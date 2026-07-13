"""Queue-aware hooks — bridge user-typed messages into a running agent.

Two hooks work together to make queued messages both **visible to the
model mid-run** and **persisted in the session history as real user
messages**:

1. ``QueueInjectorHook`` (a ``tool_hook``) drains the queue after every
   tool execution and **appends queued text onto the tool result**. The
   model sees it on its next iteration as part of the just-completed
   tool's output.

2. ``QueuePersisterHook`` (a ``post_hook``) fires once at run end, after
   ``run_output.messages`` is finalised but before Agno saves the
   session. It adds a proper ``role='user'`` message for each drained
   item, so the conversation history shows what the user typed even
   though the model first encountered it inside a tool result.

Why two hooks rather than one?

- Agno reads ``agent.additional_input`` only at run-start, so setting
  it mid-run is a no-op.
- ``run_context.messages`` is handed to tool_hooks as a shallow copy
  (Agno reassigns the live list back in a ``finally`` after the hook
  returns), so any list mutations are dropped.
- Tool results, in contrast, flow through Agno's normal append path —
  piggy-backing on them is the only safe mid-run injection point Agno
  exposes.
- ``run_output.messages`` is mutable inside ``post_hooks`` and the
  session save reads from it directly, so that's where persistence
  lives.

The two hooks share a list of "drained this run" via the injector
instance — the persister reads it, writes user messages, then clears
it for the next run.
"""

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from agno.models.message import Message

USER_NOTE_HEADER = "USER MESSAGE WHILE YOU WERE WORKING"


class QueueInjectorHook:
    """Agno tool_hook that bridges the message queue into a running agent.

    Parameters
    ----------
    queue:
        The shared message queue (a plain ``list[str]``). Items are popped
        from the front (index 0) when injected.
    on_inject:
        Optional callback ``(message: str) -> None`` called for each
        injected message. Used to update the TUI (e.g., show a notification,
        sync the queue panel).
    on_queue_changed:
        Optional callback ``() -> None`` called after the queue is mutated
        so the UI can refresh the panel.
    """

    # Agno introspects callable hooks for ``__name__`` (telemetry / log
    # tags). Class instances don't have one by default.
    __name__ = "QueueInjectorHook"

    def __init__(
        self,
        queue: list[str],
        on_inject: Callable[[str], None] | None = None,
        on_queue_changed: Callable[[], None] | None = None,
    ):
        if hasattr(inspect, "markcoroutinefunction"):
            inspect.markcoroutinefunction(self)
        else:
            self._is_coroutine = asyncio.coroutines._is_coroutine
        self._queue = queue
        self._on_inject = on_inject
        self._on_queue_changed = on_queue_changed
        # Messages drained during the current run — handed to the persister
        # post_hook for conversion into proper user-role history entries.
        # Cleared on every persist so each run starts with an empty list.
        self._injected_this_run: list[str] = []

    @property
    def injected_this_run(self) -> list[str]:
        return self._injected_this_run

    def clear_injected_this_run(self) -> None:
        self._injected_this_run = []

    async def __call__(
        self,
        name: str = "",
        func: Callable | None = None,
        args: dict[str, Any] | None = None,
        agent: Any = None,
        **kwargs: Any,
    ) -> Any:
        """Hook entry point — called by Agno around each tool execution.

        Async to work correctly in Agno's async hook chain alongside
        other async hooks (e.g. ToolEventHook).

        NOTE: The parameter MUST be named ``func`` (not ``next_func`` etc.)
        because Agno's ``_build_hook_args`` only recognises specific names:
        ``func``, ``function``, ``function_call``, ``name``, ``args``, etc.
        """
        # Execute the actual tool via the chain.
        if args is None:
            args = {}
        result: Any = None
        if func is not None:
            result = func(**args)
            if inspect.isawaitable(result):
                result = await result

        # If anything is queued, append it to the tool result so the model
        # sees it on its next iteration as part of this tool's output.
        if self._queue:
            messages = self._drain_queue()
            if messages:
                result = self._augment_result(result, messages)

        return result

    def _drain_queue(self) -> list[str]:
        messages: list[str] = []
        while self._queue:
            messages.append(self._queue.pop(0))

        if not messages:
            return messages

        # Track for the persister.
        self._injected_this_run.extend(messages)

        for msg in messages:
            if self._on_inject:
                self._on_inject(msg)
        if self._on_queue_changed:
            self._on_queue_changed()
        return messages

    @staticmethod
    def _augment_result(result: Any, messages: list[str]) -> Any:
        """Suffix the tool's output with a clearly-marked user-note block."""
        joined = "\n".join(f"- {m}" for m in messages)
        note = f"\n\n[{USER_NOTE_HEADER}]\n{joined}\n[END USER MESSAGE]"
        if result is None:
            return note.lstrip("\n")
        if isinstance(result, str):
            return result + note
        # Non-string result (rare but possible — e.g. dicts from custom tools).
        # Wrap it so the model sees the original payload plus the note.
        return f"{result!r}{note}"

    def reset(self) -> None:
        """No-op; kept for API compatibility with previous versions."""
        return


class QueuePersisterHook:
    """Agno post_hook that persists drained queue messages as user history.

    Fires once per run, after ``run_output.messages`` is built but before
    the session is saved. Reads the in-run injection list from a paired
    :class:`QueueInjectorHook` and appends a proper ``role='user'``
    message per drained item — so the conversation timeline preserves
    what the user typed even though the model first saw it embedded in
    a tool result.
    """

    # Agno introspects callable hooks for ``__name__`` (telemetry / log
    # tags). Class instances don't have one by default; expose it
    # explicitly so registration doesn't error out.
    __name__ = "QueuePersisterHook"

    def __init__(self, injector: QueueInjectorHook) -> None:
        self._injector = injector

    def __call__(self, run_output: Any = None, **_kwargs: Any) -> None:
        injected = self._injector.injected_this_run
        if not injected or run_output is None:
            self._injector.clear_injected_this_run()
            return

        # ``run_output.messages`` is the list Agno will persist with the
        # session. Append our user-role messages so they show up in
        # /sessions, history filters, and learning extraction.
        if getattr(run_output, "messages", None) is None:
            run_output.messages = []
        for text in injected:
            run_output.messages.append(
                Message(
                    role="user",
                    content=text,
                    add_to_agent_memory=True,
                )
            )
        self._injector.clear_injected_this_run()


def create_queue_hook(
    queue: list[str],
    on_inject: Callable[[str], None] | None = None,
    on_queue_changed: Callable[[], None] | None = None,
) -> tuple[QueueInjectorHook, QueuePersisterHook]:
    """Build both hooks (injector for tool_hooks, persister for post_hooks)."""
    injector = QueueInjectorHook(
        queue=queue,
        on_inject=on_inject,
        on_queue_changed=on_queue_changed,
    )
    persister = QueuePersisterHook(injector)
    return injector, persister
