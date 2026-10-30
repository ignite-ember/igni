"""Tool hook: remember each finished call's result, for a turn a restart may cut.

An assistant turn can make several tool calls at once — a ``spawn_agent``
whose sub-agent then waits on a person, beside calls that finish or are
still running. If the backend dies while the spawn waits, continuing
that turn later needs every call's result, not only the spawn's. This
hook hands each finished call's result to the approval coordinator,
which keeps it with the waiting spawn
(:meth:`~ember_code.core.sub_agent_hitl.SubAgentHITLCoordinator.tool_finished`).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from ember_code.core.hooks.queue.agno_adapter import AgnoCallableAdapter
from ember_code.core.tools.spawn_context import ParentCall


@runtime_checkable
class TurnResultSink(Protocol):
    def tool_finished(self, run_id: str, tool_call_id: str, result: str) -> None: ...


class TurnResultHook(AgnoCallableAdapter):
    """Agno tool_hook: run the tool, then tell the sink what it returned."""

    def __init__(self, sink: TurnResultSink) -> None:
        self._mark_as_coroutine()
        self._sink = sink

    async def __call__(
        self,
        name: str = "",
        func: Callable[..., Any] | None = None,
        args: dict[str, Any] | None = None,
        run_context: Any = None,
        **_: Any,
    ) -> Any:
        # Parameter names are Agno's (see AgnoCallableAdapter).
        result = func(**(args or {})) if func is not None else None
        if inspect.isawaitable(result):
            result = await result
        if run_context is not None:
            call_id = ParentCall.find_call(
                list(getattr(run_context, "messages", None) or []), name, args or {}
            )
            if call_id:
                self._sink.tool_finished(str(run_context.run_id), call_id, str(result))
        return result


__all__ = ["TurnResultHook", "TurnResultSink"]
