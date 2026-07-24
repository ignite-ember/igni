"""Typed view models for the ``/loop`` slash command's chat output.

Extracted out of :mod:`ember_code.backend.cmd_loop` — the old
procedural module built usage/status/error strings inline inside
three free functions. Every markdown/info template that the
:class:`LoopCommand` coordinator emits into chat now lives here
as a Pydantic view model with a single ``.to_command_result()``
render entry point.

Same naming + purpose pattern as the sibling
:mod:`schemas_codeindex` / :mod:`schemas_context` modules already
in ``backend/``.

Consumers:

* :class:`LoopUsageView` — the four-line usage help block shown by
  ``loop_status`` when no loop is active. Zero-field classmethod
  view because there is no per-invocation state.
* :class:`LoopStatusView` — the "Loop active: X done, Y remaining"
  snapshot for the scripted callers of ``loop_status``.
* :class:`LoopAlreadyRunningView` — the "A loop is already running"
  error branch, when ``/loop <prompt>`` fires on top of a live loop.
* :class:`LoopStoppedView` — the "Loop stopped after N iteration(s)"
  info line, with the singular/plural agreement fix.
* :class:`LoopStartedView` — wraps the ``CommandResult`` returned by
  the successful ``/loop <prompt>`` start path. Carries the
  already-wrapped meta-prompt (state-dependent on ``cap_explicit``,
  so the wrapping happens inside :meth:`LoopCommand._start` and the
  finished string arrives here).
* :class:`LoopResumedView` — wraps the ``CommandResult`` returned by
  the successful ``/loop resume`` path. Symmetric with
  :class:`LoopStartedView` — both action-carrying results get their
  own view instead of the coordinator constructing
  ``CommandResult(kind=INFO, ..., action=RUN_PROMPT)`` inline.
* :class:`LoopStatusSnapshot` — RPC wire snapshot for the ``/loop``
  panel header (:meth:`LoopController.status`). Co-located with the
  chat views so all ``/loop`` wire shapes live in one file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from ember_code.backend.command_result import CommandResult
from ember_code.protocol.messages import CommandAction, CommandResultKind

if TYPE_CHECKING:
    from ember_code.core.session import Session


class LoopUsageView(BaseModel):
    """Static ``/loop`` usage help block.

    Rendered by ``loop_status`` when no loop is active. Kept as a
    zero-field Pydantic model so the coordinator invokes it the
    same way as every other view (``.to_command_result()``).
    """

    @classmethod
    def to_command_result(cls) -> CommandResult:
        return CommandResult.info(
            "No loop is running.\n\n"
            "Usage:\n"
            "  /loop <prompt>          start (default cap: 30 iterations)\n"
            "  /loop <N> <prompt>      start with explicit cap N\n"
            "  /loop stop              cancel the active loop\n"
            "  /loop resume            re-fire the interrupted iteration after a restart"
        )


class LoopStatusView(BaseModel):
    """Three-line snapshot of the active loop's state.

    ``pending_prompt`` is rendered with ``repr()`` (matches the
    original inline template) so newlines / quoting stay readable
    in a single-line chat card.
    """

    iteration_index: int
    iterations_remaining: int
    pending_prompt: str

    def to_command_result(self) -> CommandResult:
        return CommandResult.info(
            f"Loop active: {self.iteration_index} done, "
            f"{self.iterations_remaining} remaining.\n"
            f"Prompt: {self.pending_prompt!r}"
        )


class LoopAlreadyRunningView(BaseModel):
    """ "Loop already running" error for ``/loop <prompt>`` on top of a live loop.

    Guides the user to ``/loop stop`` first — the tool tips are
    already baked into the message so the coordinator doesn't have
    to duplicate them.
    """

    iteration_index: int
    iterations_remaining: int

    def to_command_result(self) -> CommandResult:
        return CommandResult.error(
            f"A loop is already running ({self.iteration_index} done, "
            f"{self.iterations_remaining} remaining). "
            "Run `/loop stop` first, then start a new one."
        )


class LoopStoppedView(BaseModel):
    """ "Loop stopped after N iteration(s)" info line.

    Handles the singular/plural agreement in one place — the old
    inline ``{'s' if done != 1 else ''}`` ternary lived at the call
    site.
    """

    iterations_done: int

    def to_command_result(self) -> CommandResult:
        suffix = "s" if self.iterations_done != 1 else ""
        return CommandResult.info(f"Loop stopped after {self.iterations_done} iteration{suffix}.")


class LoopStartedView(BaseModel):
    """Successful ``/loop <prompt>`` start — carries a ``RUN_PROMPT`` action.

    ``wrapped_prompt`` is the autonomous-loop-wrapped meta-prompt
    (produced by :func:`ember_code.core.loop.wrap_iteration_prompt`
    inside the coordinator, where ``cap_explicit`` is in scope).
    ``display_prompt`` is the bare user text — carried on
    :attr:`CommandResult.display_content` so chat renders the human
    prompt while the agent receives the wrapper.
    """

    wrapped_prompt: str
    display_prompt: str

    def to_command_result(self) -> CommandResult:
        return CommandResult(
            kind=CommandResultKind.INFO,
            content=self.wrapped_prompt,
            display_content=self.display_prompt,
            action=CommandAction.RUN_PROMPT,
        )


class LoopResumedView(BaseModel):
    """Successful ``/loop resume`` — re-fires the interrupted iteration.

    Symmetric with :class:`LoopStartedView`: both action-carrying
    results move out of the coordinator. Resume has no wrapper
    handling (the previously-persisted prompt is already wrapped
    on the persistence side), so this view only carries the raw
    prompt.
    """

    prompt: str

    def to_command_result(self) -> CommandResult:
        return CommandResult(
            kind=CommandResultKind.INFO,
            content=self.prompt,
            action=CommandAction.RUN_PROMPT,
        )


class LoopStatusSnapshot(BaseModel):
    """RPC wire shape for :meth:`LoopController.status`.

    Renamed from the old inline ``LoopStatus`` (in ``server_loop.py``)
    to avoid colliding with the sibling :class:`LoopStatusView` chat
    card in this same module. Same eight fields, same wire contract
    (the RPC layer serialises via ``.model_dump()`` so the FE JSON
    payload is byte-identical).

    ``from_session`` is the canonical constructor — it encapsulates
    the announced-total lookup on :class:`LoopProgressStore` so
    :class:`LoopController` doesn't have to know that the total lives
    on a per-run key/value store.
    """

    active: bool
    paused: bool
    prompt: str
    iteration_index: int
    iterations_remaining: int
    cap_explicit: bool
    announced_total: int | None

    @classmethod
    async def from_session(cls, session: Session) -> LoopStatusSnapshot:
        """Build a snapshot from the current :class:`Session` state.

        Reads the announced-total via
        :meth:`LoopProgressStore.get_announced_total` — the store owns
        the ``__loop_total__`` key contract so callers don't reach
        into a class-private constant to spell it correctly.
        """
        announced_total: int | None = None
        if session.loop_run_id:
            announced_total = await session.loop_progress_store.get_announced_total(
                session.loop_run_id
            )
        return cls(
            active=session.pending_loop_prompt is not None,
            paused=session.loop_paused,
            prompt=session.pending_loop_prompt or "",
            iteration_index=session.loop_iteration_index,
            iterations_remaining=session.loop_iterations_remaining,
            cap_explicit=session.loop_cap_explicit,
            announced_total=announced_total,
        )


__all__ = [
    "LoopUsageView",
    "LoopStatusView",
    "LoopAlreadyRunningView",
    "LoopStoppedView",
    "LoopStartedView",
    "LoopResumedView",
    "LoopStatusSnapshot",
]
