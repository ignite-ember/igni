"""``/loop`` slash command implementation.

Owns the ``/loop`` command family + its status/stop helpers via a
coordinator class:

* :class:`LoopCommand` — one instance per ``handler.session``,
  exposes ``run(args) / status() / stop()`` as methods; the four
  subcommands (start / stop / resume / open-panel) live as private
  methods on the class.
* :class:`LoopCapArgs` — Pydantic result of parsing the leading
  ``<N>`` / ``<N>x`` iteration-cap token; ``LoopCapArgs.parse`` is
  the classmethod constructor so parsing + shape live together.

Subcommands:

* ``/loop`` (no args) — open the TUI loop panel.
* ``/loop stop`` — cancel the active loop.
* ``/loop resume`` — re-fire the interrupted iteration after a
  restart (paused → running).
* ``/loop <prompt>`` — start with the default cap (30 iterations,
  implicit safety net that auto-extends past on cap-hit).
* ``/loop <N> <prompt>`` — start with explicit cap N. Explicit
  caps TERMINATE at N; implicit caps auto-extend the safety net
  and stop only at ``LOOP_HARD_CAP``.

Presentation strings all live in :mod:`schemas_loop` — the
coordinator constructs a view model and returns its
``.to_command_result()``. Mirrors the sibling ``cmd_codeindex.py``
+ ``schemas_codeindex.py`` / ``cmd_context.py`` +
``schemas_context.py`` pattern.

``Session.start_loop`` / ``advance_loop`` / ``cancel_loop`` /
``resume_loop`` live in ``session/loop_ops.py``; this module is
the slash-command surface for those.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from ember_code.backend.command_result import CommandResult
from ember_code.backend.schemas_loop import (
    LoopAlreadyRunningView,
    LoopResumedView,
    LoopStartedView,
    LoopStatusView,
    LoopStoppedView,
    LoopUsageView,
)
from ember_code.core.loop import wrap_iteration_prompt
from ember_code.core.loop.limits import (
    LOOP_DEFAULT_MAX_ITERATIONS,
    LOOP_HARD_CAP,
)
from ember_code.protocol.messages import CommandAction

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler
    from ember_code.core.session import Session


class LoopCapArgs(BaseModel):
    """Parsed ``<N>`` / ``<N>x`` prefix from a ``/loop`` argument string.

    ``cap_explicit=True`` means the user supplied a number — the
    loop should terminate at exactly ``max_iter``. False means we
    fell back to :data:`LOOP_DEFAULT_MAX_ITERATIONS` as an
    auto-extending safety net.

    ``error`` is ``None`` on success; a non-empty string when the
    number was out-of-range. Following the ``SyncResult`` shape from
    CODE_STANDARDS Pattern 3 — callers check ``args.ok`` rather than
    scanning a sentinel empty string.
    """

    max_iter: int
    cap_explicit: bool
    prompt: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @classmethod
    def parse(cls, text: str) -> LoopCapArgs:
        """Parse a leading ``<N>``/``<N>x`` token as the iteration cap."""
        first, _, rest = text.partition(" ")
        first_num = first.rstrip("x")
        if not first_num.isdigit():
            return cls(
                max_iter=LOOP_DEFAULT_MAX_ITERATIONS,
                cap_explicit=False,
                prompt=text,
            )
        n = int(first_num)
        if n <= 0:
            return cls(
                max_iter=0,
                cap_explicit=True,
                prompt="",
                error="Iteration cap must be positive. Try `/loop 5 your prompt`.",
            )
        if n > LOOP_HARD_CAP:
            return cls(
                max_iter=0,
                cap_explicit=True,
                prompt="",
                error=(
                    f"Iteration cap {n} exceeds the hard cap of "
                    f"{LOOP_HARD_CAP}. Pick a smaller number."
                ),
            )
        return cls(max_iter=n, cap_explicit=True, prompt=rest.strip())


class LoopCommand:
    """``/loop`` command coordinator — one instance per
    :class:`Session`. Every subcommand is a method; presentation
    lives in :mod:`schemas_loop`.
    """

    _STOP_TOKENS = frozenset({"stop", "cancel", "off", "end"})

    def __init__(self, session: Session) -> None:
        self._session = session

    async def run(self, args: str) -> CommandResult:
        """Route the ``/loop <args>`` verb to the right subcommand."""
        text = args.strip()

        # No args → open the TUI panel (the panel polls
        # ``loop_status`` live). ``status`` is kept for scripted
        # callers.
        if not text:
            return CommandResult.for_action(CommandAction.LOOP)

        if text.lower() in self._STOP_TOKENS:
            return await self.stop()

        if text.lower() == "resume":
            return await self._resume()

        # Parse leading "<N>" or "<N>x" as the iteration cap. Explicit
        # means "exactly N"; implicit means the default safety net
        # that auto-extends past on cap-hit.
        parsed = LoopCapArgs.parse(text)
        if not parsed.ok:
            assert parsed.error is not None  # ok=False ⇒ error is set
            return CommandResult.error(parsed.error)

        if not parsed.prompt:
            return CommandResult.error(
                "Loop needs a prompt. Try `/loop fix the typo in foo.py, bar.py`."
            )

        return await self._start(parsed)

    def status(self) -> CommandResult:
        """Return a text status snapshot — used by scripted callers.
        The TUI's live panel polls ``loop_status`` on the session
        directly."""
        sess = self._session
        if sess.pending_loop_prompt is None:
            return LoopUsageView.to_command_result()
        return LoopStatusView(
            iteration_index=sess.loop_iteration_index,
            iterations_remaining=sess.loop_iterations_remaining,
            pending_prompt=sess.pending_loop_prompt,
        ).to_command_result()

    async def stop(self) -> CommandResult:
        """Cancel the active loop."""
        sess = self._session
        if sess.pending_loop_prompt is None:
            return CommandResult.info("No loop is running.")
        done = sess.loop_iteration_index
        await sess.cancel_loop()
        return LoopStoppedView(iterations_done=done).to_command_result()

    async def _start(self, parsed: LoopCapArgs) -> CommandResult:
        """Start a new loop iteration 1 fires immediately via the
        returned ``run_prompt`` action."""
        sess = self._session
        # Refuse to start a second loop on top of an active one —
        # the user almost certainly wants to /loop stop first.
        if sess.pending_loop_prompt is not None:
            return LoopAlreadyRunningView(
                iteration_index=sess.loop_iteration_index,
                iterations_remaining=sess.loop_iterations_remaining,
            ).to_command_result()

        # Slash-command path: iteration 1 fires immediately via the
        # ``run_prompt`` action below, so we use ``immediate=True``
        # which initializes ``index=1, remaining=max-1``. Subsequent
        # iterations are driven by ``_check_loop_continuation`` →
        # ``advance_loop`` in the run controller.
        await sess.start_loop(
            parsed.prompt,
            parsed.max_iter,
            immediate=True,
            cap_explicit=parsed.cap_explicit,
        )

        # Wrap iteration 1's prompt with the autonomous-loop meta-
        # instruction so the agent doesn't ask the user questions
        # between iterations. Iterations 2+ get wrapped inside
        # ``Session.advance_loop``; this branch handles iteration 1
        # because the slash command fires it directly via
        # ``run_prompt`` rather than through ``advance_loop``.
        # ``total`` is only included when the user explicitly capped.
        wrapped = wrap_iteration_prompt(
            parsed.prompt,
            iteration=1,
            total=parsed.max_iter if parsed.cap_explicit else None,
        )
        return LoopStartedView(
            wrapped_prompt=wrapped, display_prompt=parsed.prompt
        ).to_command_result()

    async def _resume(self) -> CommandResult:
        """Re-fire the interrupted iteration after a restart. The
        session was killed mid-iteration; the persisted state was
        loaded on startup but nothing is firing yet. ``resume_loop``
        flips the paused flag and returns the prompt, which we send
        back as a ``run_prompt`` action — the FE fires ``_run(prompt)``
        directly, re-running the interrupted iteration.
        """
        sess = self._session
        if sess.pending_loop_prompt is None:
            return CommandResult.error("No loop to resume.")
        if not sess.loop_paused:
            return CommandResult.info("Loop is already running — no resume needed.")
        prompt = await sess.resume_loop()
        if prompt is None:
            # Race: another caller flipped ``paused`` False between
            # the checks above and the resume call. Treat as already
            # running.
            return CommandResult.info("Loop is already running.")
        return LoopResumedView(prompt=prompt).to_command_result()


# ── Handler dispatch shims (kept for command_handler.py callers) ─


async def cmd_loop(handler: CommandHandler, args: str) -> CommandResult:
    """Dispatch shim used by ``CommandHandler._cmd_loop`` — routes
    into :class:`LoopCommand`. Kept as a module-level function so
    the existing dispatch table in ``command_handler.py`` doesn't
    have to change shape."""
    return await LoopCommand(handler.session).run(args)


def loop_status(handler: CommandHandler) -> CommandResult:
    """Dispatch shim — see :meth:`LoopCommand.status`."""
    return LoopCommand(handler.session).status()


async def loop_stop(handler: CommandHandler) -> CommandResult:
    """Dispatch shim — see :meth:`LoopCommand.stop`."""
    return await LoopCommand(handler.session).stop()


__all__ = ["LoopCapArgs", "LoopCommand", "cmd_loop", "loop_status", "loop_stop"]
