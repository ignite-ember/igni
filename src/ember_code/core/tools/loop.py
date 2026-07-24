"""LoopTools — agent-facing adapter for the in-session ``/loop`` primitive.

This module is a pure :class:`agno.tools.Toolkit` adapter. The
validation, state-machine transitions, and typed results all live
on :class:`LoopController` (``core/session/loop_ops.py``); each
method here is a two-line delegate to
``self._session.<verb>_from_tool(...)`` followed by
:meth:`LoopToolResult.render` to convert the structured outcome
into the English string Agno passes back to the model.

Why the split:

* Data + behaviour re-unify on :class:`LoopController` — it owns
  ``/loop`` state and now also owns the "is a loop active?"
  validation the tools used to duplicate.
* :class:`LoopToolResult` carries structured fields (``ok``,
  ``code``, ``iteration_index``, …) so a future non-prose consumer
  (JSON tool-call transport, telemetry, structured hooks) can
  branch on the outcome without parsing English.
* Only :class:`LoopTools` (the tool-adapter boundary) calls
  :meth:`LoopToolResult.render`. The domain layer stays prose-free.

The user-invokable slash command (``/loop <prompt>``, ``/loop stop``)
mutates the same state — see ``backend/command_handler.py``. Both
surfaces funnel through :class:`LoopController` so behaviour is
consistent regardless of how the loop was started/stopped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.tools import Toolkit

# Caps live in ``core/loop/limits.py`` so the slash command and the
# agent tool stay in lockstep — bumping the safety net in one place
# updates both call sites.
from ember_code.core.loop.limits import (
    LOOP_DEFAULT_MAX_ITERATIONS as _LOOP_DEFAULT_MAX_ITERATIONS,
)

# ``LoopProgressTool`` lives in ``loop_progress.py`` — a separate
# module so the agent-facing control tool (this file) and the
# per-iteration progress scratchpad are each single-responsibility.
# Top-of-module import keeps callers that spell
# ``ember_code.core.tools.loop.LoopProgressTool`` working (part of
# the public API of this module) without an inline import at the
# bottom of the file.
from ember_code.core.tools.loop_progress import LoopProgressTool

if TYPE_CHECKING:
    from ember_code.core.session.core import Session


class LoopTools(Toolkit):
    """Agent-facing tools that drive the in-session ``/loop`` primitive.

    Pure Toolkit adapter: each async method registers with Agno,
    delegates to the matching :meth:`Session.*_from_tool` method,
    and renders the returned :class:`LoopToolResult` into the
    English string Agno passes back to the model.
    """

    def __init__(self, session: Session) -> None:
        super().__init__(name="ember_loop")
        self._session = session
        self.register(self.loop_start)
        self.register(self.loop_stop)
        self.register(self.loop_status)
        self.register(self.loop_resume)
        self.register(self.loop_set_total)

    async def loop_start(
        self,
        prompt: str,
        max_iterations: int = _LOOP_DEFAULT_MAX_ITERATIONS,
    ) -> str:
        """Start an in-session loop: the same prompt fires as the next
        user turn over and over, until ``max_iterations`` is reached,
        the user interrupts with non-loop input, or ``loop_stop`` is
        called.

        Use when the user describes work that repeats — *"do X for each
        of A, B, C"*, *"keep fixing failures until the suite passes"*,
        *"go through these one at a time"*. **Do not** use for a single
        task that just happens to mention multiple things; the loop is
        a real repetition primitive.

        The first iteration fires automatically after this tool call
        completes (i.e. as the next agent turn). Each iteration runs
        the full agent loop with ``prompt`` as the user input — so the
        prompt should be self-contained enough to drive one iteration's
        worth of work without further user input.

        Args:
            prompt: The text re-fired on every iteration. Make it
                stand on its own — the loop runner doesn't add context
                between iterations; conversation history persists, but
                the user-input text the agent sees is just this string.
            max_iterations: Safety cap. Defaults to 30; the hard ceiling
                is 200. Pick the smallest value that fits the work — an
                over-tight cap is much cheaper to recover from than an
                infinite loop.

        Returns:
            A confirmation string the agent can show inline.
        """
        result = await self._session.start_loop_from_tool(prompt, max_iterations)
        return result.render()

    async def loop_stop(self) -> str:
        """Cancel the active loop. The current turn finishes normally;
        no further iterations fire.

        Use when the user says they're done, when the work is finished,
        or when continuing would be wasteful (e.g., the last iteration
        already revealed everything that's left to do). It's also fine
        to call this defensively if you can't tell whether a loop is
        active — it's a no-op when there's nothing to cancel.
        """
        result = await self._session.stop_loop_from_tool()
        return result.render()

    async def loop_set_total(self, total: int) -> str:
        """Announce the total number of iterations this loop will run.

        The user's natural-language prompt rarely matches the
        ``/loop N <prompt>`` literal syntax — they say *"loop
        through these 12 files"* or *"check every section"*. Call
        this once you've determined the actual count (e.g. after
        listing files, parsing a spec, or counting sections); the
        panel then renders ``N / total`` instead of just the
        current iteration number.

        Calling this is *informational only* — it does not change
        the loop's safety cap or auto-extend behavior. If the
        count turns out wrong, call this again with the corrected
        value; subsequent panel renders pick up the new number.

        Args:
            total: Positive integer — the expected iteration count.
        """
        result = await self._session.set_announced_total_from_tool(total)
        return result.render()

    async def loop_resume(self) -> str:
        """Resume an interrupted ``/loop`` (one whose state was
        loaded from disk on session startup but hasn't fired any
        iterations yet).

        Use when the user asks to *"continue the loop"*,
        *"pick up where we left off"*, or similar after a restart.
        The tool unpauses the loop; the next iteration fires
        automatically after this turn ends, via the run controller's
        post-turn continuation hook.

        Semantic note vs. ``/loop resume`` from chat: the slash
        version re-fires the *interrupted* iteration K (because the
        user is outside an agent turn and K was killed mid-flight).
        This tool runs *during* an agent turn — by the time the
        post-turn hook fires, the next-iteration counter has
        already advanced, so iteration K+1 runs. Both are correct
        for their contexts.

        Errors when there's no loop to resume or when the loop is
        already pumping.
        """
        result = await self._session.resume_loop_from_tool()
        return result.render()

    async def loop_status(self) -> str:
        """Report whether a loop is active and how many iterations
        remain. Useful when the user asks something like *"are we still
        looping?"* — answer from this rather than guessing."""
        result = self._session.loop_status_from_tool()
        return result.render()


__all__ = ["LoopTools", "LoopProgressTool"]
