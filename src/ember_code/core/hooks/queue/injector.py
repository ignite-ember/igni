"""Tool_hook that drains the queue after each tool call.

Runs inside Agno's tool_hook chain around every tool execution. On
each call:

1. Executes the wrapped tool via ``func(**args)``, awaiting if the
   result is a coroutine.
2. Drains any items currently on the shared queue into the shared
   :class:`InjectedRunBuffer` so the persister can turn them into
   user-role history entries at run end.
3. If anything drained, renders an
   :class:`~ember_code.core.hooks.queue.schemas.InjectedNote` onto the
   tool result so the model sees the user message on its next
   iteration as part of the just-completed tool's output.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from .agno_adapter import AgnoCallableAdapter
from .buffer import InjectedRunBuffer
from .schemas import InjectedNote, QueueCallbacks, ToolHookInvocation


class QueueInjectorHook(AgnoCallableAdapter):
    """Agno tool_hook that bridges the message queue into a running agent.

    Parameters
    ----------
    buffer:
        Shared :class:`InjectedRunBuffer` — the injector writes drained
        messages here; the persister reads them at run end. Both hooks
        must be constructed with the SAME buffer instance (that's what
        :class:`~ember_code.core.hooks.queue.bridge.QueueBridge`
        guarantees).
    queue:
        The raw message queue (a plain ``list[str]``). Items are
        popped from the front (index 0) when injected.
    callbacks:
        Optional :class:`QueueCallbacks` bundle for UI notifications.
    """

    def __init__(
        self,
        buffer: InjectedRunBuffer,
        queue: list[str],
        callbacks: QueueCallbacks | None = None,
    ) -> None:
        self._mark_as_coroutine()
        self._buffer = buffer
        self._queue = queue
        self._callbacks = callbacks or QueueCallbacks()

    async def __call__(
        self,
        name: str = "",
        func: Callable[..., Any] | None = None,
        args: dict[str, Any] | None = None,
        agent: Any = None,
        **kwargs: Any,
    ) -> Any:
        """Hook entry point — called by Agno around each tool execution.

        Async to work correctly in Agno's async hook chain alongside
        other async hooks (e.g. ToolEventHook).

        NOTE: parameter names are pinned to what Agno's
        ``_build_hook_args`` recognises (``name``, ``func``, ``args``,
        ``agent``) — see :class:`AgnoCallableAdapter`. Do NOT rename
        these params.
        """
        invocation = ToolHookInvocation(
            name=name,
            func=func,
            args=args or {},
            agent=agent,
            extra=kwargs,
        )
        result = await self._execute_tool(invocation)
        drained = self._drain_into_buffer()
        if drained:
            result = InjectedNote(messages=drained).render_onto(result)
        return result

    async def _execute_tool(self, invocation: ToolHookInvocation) -> Any:
        """Run the wrapped tool through the hook chain, awaiting if
        needed."""
        if invocation.func is None:
            return None
        result = invocation.func(**invocation.args)
        if inspect.isawaitable(result):
            result = await result
        return result

    def _drain_into_buffer(self) -> list:
        """Pull every item off the raw queue into the shared buffer.

        Returns the list of :class:`InjectedMessage` instances created
        this call so the caller can render them onto the tool result.
        Also fires the UI callbacks.
        """
        drained = []
        while self._queue:
            text = self._queue.pop(0)
            message = self._buffer.append(text)
            drained.append(message)

        if not drained:
            return drained

        for message in drained:
            self._callbacks.notify_inject(message.text)
        self._callbacks.notify_queue_changed()
        return drained

    # ── Compatibility surface ────────────────────────────────────────
    #
    # The two properties below preserve the pre-refactor API on the
    # injector so external plugins (and the existing test file) that
    # read ``injector.injected_this_run`` / call
    # ``injector.clear_injected_this_run()`` keep working. New code
    # should read the buffer directly via ``QueueBridge.buffer``.

    @property
    def injected_this_run(self) -> list[str]:
        return [m.text for m in self._buffer.items]

    def clear_injected_this_run(self) -> None:
        self._buffer.clear()

    def reset(self) -> None:
        """No-op; kept for API compatibility with previous versions."""
        return


__all__ = ["QueueInjectorHook"]
