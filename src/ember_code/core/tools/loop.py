"""LoopTools — agent-facing control of the in-session ``/loop`` primitive.

The user-invokable slash command (``/loop <prompt>``, ``/loop stop``)
mutates the same state these tools touch. Having an agent-facing tool
means the user can also say things in plain language — *"keep doing
this for every file in this list"*, *"stop the loop, we're done"* —
and the agent translates the intent into a ``loop_start`` /
``loop_stop`` call.

Mechanics:

- ``loop_start(prompt, max_iterations)`` writes onto the session's
  ``pending_loop_prompt`` field. The next time the run controller's
  ``_drain_queue`` returns to idle, ``_check_loop_continuation`` sees
  the field set and fires ``prompt`` as the next turn. Repeats until
  ``max_iterations`` is exhausted, the field is cleared, or the user
  types a non-/loop message (which is treated as an interrupt).
- ``loop_stop()`` clears the field. If the agent calls this inside an
  iteration, the loop won't fire again after the current turn — the
  hook reads the cleared state right after this turn ends.

These tools deliberately have no side effects beyond touching the
session fields. They never produce code, files, or external calls;
the *agent's own next-turn prompt* is what does the work. The tools
just decide whether that prompt re-fires.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agno.tools import Toolkit

if TYPE_CHECKING:
    from ember_code.core.session.core import Session

logger = logging.getLogger(__name__)


# Mirrors the slash-command caps. Defined here too so the tool is
# self-contained — the agent doesn't need to know about the command
# handler's internals.
_LOOP_DEFAULT_MAX_ITERATIONS = 30
_LOOP_HARD_CAP = 200


class LoopTools(Toolkit):
    """Agent-facing tools that drive the in-session ``/loop`` primitive."""

    def __init__(self, session: Session) -> None:
        super().__init__(name="ember_loop")
        self._session = session
        self.register(self.loop_start)
        self.register(self.loop_stop)
        self.register(self.loop_status)

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
        if not prompt or not prompt.strip():
            return "ERROR: loop_start needs a non-empty prompt."
        if max_iterations <= 0:
            return "ERROR: max_iterations must be positive."
        if max_iterations > _LOOP_HARD_CAP:
            return (
                f"ERROR: max_iterations={max_iterations} exceeds the hard "
                f"cap of {_LOOP_HARD_CAP}. Pick a smaller number."
            )
        sess = self._session
        if sess.pending_loop_prompt is not None:
            return (
                f"ERROR: a loop is already active "
                f"({sess.loop_iteration_index} done, "
                f"{sess.loop_iterations_remaining} remaining). "
                "Call loop_stop() first if you want to start a new one."
            )
        sess.pending_loop_prompt = prompt.strip()
        sess.loop_iteration_index = 0
        sess.loop_iterations_remaining = max_iterations
        return (
            f"Loop armed. Will re-fire this prompt up to {max_iterations} "
            "more times. First iteration runs as the next turn."
        )

    async def loop_stop(self) -> str:
        """Cancel the active loop. The current turn finishes normally;
        no further iterations fire.

        Use when the user says they're done, when the work is finished,
        or when continuing would be wasteful (e.g., the last iteration
        already revealed everything that's left to do). It's also fine
        to call this defensively if you can't tell whether a loop is
        active — it's a no-op when there's nothing to cancel.
        """
        sess = self._session
        if sess.pending_loop_prompt is None:
            return "No loop is active. Nothing to stop."
        done = sess.loop_iteration_index
        sess.pending_loop_prompt = None
        sess.loop_iterations_remaining = 0
        return f"Loop stopped after {done} iteration{'s' if done != 1 else ''}."

    async def loop_status(self) -> str:
        """Report whether a loop is active and how many iterations
        remain. Useful when the user asks something like *"are we still
        looping?"* — answer from this rather than guessing."""
        sess = self._session
        if sess.pending_loop_prompt is None:
            return "No loop is active."
        return (
            f"Loop active: iteration {sess.loop_iteration_index} done, "
            f"{sess.loop_iterations_remaining} remaining. "
            f"Prompt: {sess.pending_loop_prompt!r}"
        )
