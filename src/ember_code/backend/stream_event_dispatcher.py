"""Per-event side-effects for the run's streaming loop.

Extracted out of the ``if isinstance(proto, RunCompleted): …
elif isinstance(proto, (ToolCompleted, ToolError)): …`` chain that
used to sit inline in ``server_run.run_message_locked``.

Every event that fires an ``await`` gets its own private method
here so the RunController's main loop reads as:

    async for proto in self._multiplexer.stream(...):
        yield proto
        await self._dispatcher.handle(proto, team)

Adding a new per-event side-effect (e.g. per-tool metric emit) is a
new method on this class, not another ``elif`` branch in a growing
chain.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from agno.team import Team

from ember_code.protocol import messages as msg

if TYPE_CHECKING:
    from ember_code.core.session import Session


class StreamEventDispatcher:
    """Dispatch per-event side-effects for the run's streaming loop.

    Constructor takes the ``session`` (for the input-token latch)
    and a checkpoint callable (bound to whichever
    ``SessionCheckpointer``-shaped object the RunController owns).
    Both are injected rather than fetched off a
    ``backend._x`` — the class is a pure orchestrator.
    """

    def __init__(
        self,
        session: Session,
        checkpoint: Callable[[Team], Awaitable[None]],
    ) -> None:
        self._session = session
        self._checkpoint = checkpoint

    async def handle(self, event: msg.Message, team: Team) -> None:
        """Route a single event to its side-effect method."""
        if isinstance(event, msg.RunCompleted):
            await self._on_run_completed(event)
            return
        if isinstance(event, (msg.ToolCompleted, msg.ToolError)):
            await self._on_tool_boundary(team)
            return
        # No side-effect for other event kinds today; keeping the
        # method explicit so a future branch has a clear insertion
        # point without reshaping the main loop.

    async def _on_run_completed(self, event: msg.RunCompleted) -> None:
        """Latch the top-level run's ``input_tokens`` count onto the
        session so the status-footer read is O(1) instead of an
        ``aget_session`` roundtrip."""
        if event.parent_run_id:
            # Sub-agent completions don't represent the "live"
            # context — skip.
            return
        if not event.input_tokens:
            return
        self._session.latch_input_tokens(event.input_tokens)

    async def _on_tool_boundary(self, team: Team) -> None:
        """Fire an incremental session checkpoint after each tool
        boundary (completed OR errored).

        Agno's default persistence is end-of-run only; without this
        write a mid-chain crash loses every tool result. The cost
        is one SQLite upsert per tool call — a few ms."""
        await self._checkpoint(team)
