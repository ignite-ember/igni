"""``/loop`` slash command implementation.

Extracted from :mod:`ember_code.backend.command_handler` — the
``/loop`` command family + its status/stop helpers.

Subcommands:

* ``/loop`` (no args) — open the TUI loop panel.
* ``/loop stop`` — cancel the active loop.
* ``/loop resume`` — re-fire the interrupted iteration after a
  restart (paused → running).
* ``/loop <prompt>`` — start with the default cap (30 iterations,
  implicit safety net that auto-extends past on cap-hit).
* ``/loop <N> <prompt>`` — start with explicit cap N. Explicit
  caps TERMINATE at N; implicit caps auto-extend the safety
  net and stop only at ``LOOP_HARD_CAP``.

Extraction ordering note: ``Session.start_loop`` /
``advance_loop`` / ``cancel_loop`` / ``resume_loop`` live in
``session/loop_ops.py`` (extracted iter 140). This module is
the slash-command surface for those; the agent-tool surface
lives in ``core/tools/loop.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ember_code.core.loop import wrap_iteration_prompt
from ember_code.core.loop.limits import (
    LOOP_DEFAULT_MAX_ITERATIONS,
    LOOP_HARD_CAP,
)

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler, CommandResult


async def cmd_loop(handler: "CommandHandler", args: str) -> "CommandResult":
    """Drive a prompt in a loop within the current session."""
    from ember_code.backend import command_handler as _handler
    from ember_code.protocol.messages import CommandAction, CommandResultKind

    CommandResult = _handler.CommandResult
    text = args.strip()

    # No args → open the TUI panel (the panel polls
    # ``loop_status`` live). ``_loop_status`` is kept on the
    # handler for scripted callers.
    if not text:
        return CommandResult.loop()

    # Stop.
    if text.lower() in {"stop", "cancel", "off", "end"}:
        return await loop_stop(handler)

    # Resume an interrupted (paused) loop. The session was killed
    # mid-iteration; the persisted state was loaded on startup but
    # nothing is firing yet. ``resume_loop`` flips the paused
    # flag and returns the prompt, which we send back as a
    # ``run_prompt`` action — the FE fires ``_run(prompt)``
    # directly, re-running the interrupted iteration.
    if text.lower() == "resume":
        sess = handler._session
        if sess.pending_loop_prompt is None:
            return CommandResult.error("No loop to resume.")
        if not sess.loop_paused:
            return CommandResult.info("Loop is already running — no resume needed.")
        prompt = await sess.resume_loop()
        if prompt is None:
            # Race: another caller flipped paused False between the
            # checks above and the resume call. Treat as already
            # running.
            return CommandResult.info("Loop is already running.")
        return CommandResult(
            kind=CommandResultKind.INFO,
            content=prompt,
            action=CommandAction.RUN_PROMPT,
        )

    # Parse leading "<N>" or "<N>x" as the iteration cap.
    # ``cap_explicit`` tracks whether the user supplied a number
    # at all — explicit means "exactly N", implicit means
    # "default safety net of LOOP_DEFAULT_MAX_ITERATIONS that
    # auto-extends past on cap-hit". The two semantics diverge
    # inside ``Session.advance_loop``.
    max_iter = LOOP_DEFAULT_MAX_ITERATIONS
    cap_explicit = False
    first, _, rest = text.partition(" ")
    first_num = first.rstrip("x")
    if first_num.isdigit():
        n = int(first_num)
        if n <= 0:
            return CommandResult.error(
                "Iteration cap must be positive. Try `/loop 5 your prompt`."
            )
        if n > LOOP_HARD_CAP:
            return CommandResult.error(
                f"Iteration cap {n} exceeds the hard cap of "
                f"{LOOP_HARD_CAP}. Pick a smaller number."
            )
        max_iter = n
        cap_explicit = True
        prompt = rest.strip()
    else:
        prompt = text

    if not prompt:
        return CommandResult.error(
            "Loop needs a prompt. Try `/loop fix the typo in foo.py, bar.py`."
        )

    # Refuse to start a second loop on top of an active one — the
    # user almost certainly wants to /loop stop first.
    sess = handler._session
    if sess.pending_loop_prompt is not None:
        return CommandResult.error(
            f"A loop is already running ({sess.loop_iteration_index} done, "
            f"{sess.loop_iterations_remaining} remaining). "
            "Run `/loop stop` first, then start a new one."
        )

    # Slash-command path: iteration 1 fires immediately via the
    # ``run_prompt`` action below, so we use ``immediate=True``
    # which initializes ``index=1, remaining=max-1``. Subsequent
    # iterations are driven by ``_check_loop_continuation`` →
    # ``advance_loop`` in the run controller.
    await sess.start_loop(prompt, max_iter, immediate=True, cap_explicit=cap_explicit)

    # Wrap iteration 1's prompt with the autonomous-loop
    # meta-instruction so the agent doesn't ask the user
    # questions between iterations. Iterations 2+ get wrapped
    # inside ``Session.advance_loop``; this branch handles
    # iteration 1 because the slash command fires it directly
    # via ``run_prompt`` rather than going through
    # ``advance_loop``. ``display_content`` carries the bare
    # prompt for chat rendering — the wrapper is only meant
    # for the agent. ``total`` is only included in the wrapper
    # when the user explicitly capped the run.
    wrapped = wrap_iteration_prompt(
        prompt, iteration=1, total=max_iter if cap_explicit else None
    )

    return CommandResult(
        kind=CommandResultKind.INFO,
        content=wrapped,
        display_content=prompt,
        action=CommandAction.RUN_PROMPT,
    )


def loop_status(handler: "CommandHandler") -> "CommandResult":
    """Return a text status snapshot (used by scripted callers —
    the TUI's live panel polls ``loop_status`` on the session
    directly)."""
    from ember_code.backend import command_handler as _handler

    CommandResult = _handler.CommandResult
    sess = handler._session
    if sess.pending_loop_prompt is None:
        return CommandResult.info(
            "No loop is running.\n\n"
            "Usage:\n"
            "  /loop <prompt>          start (default cap: 30 iterations)\n"
            "  /loop <N> <prompt>      start with explicit cap N\n"
            "  /loop stop              cancel the active loop\n"
            "  /loop resume            re-fire the interrupted iteration after a restart"
        )
    return CommandResult.info(
        f"Loop active: {sess.loop_iteration_index} done, "
        f"{sess.loop_iterations_remaining} remaining.\n"
        f"Prompt: {sess.pending_loop_prompt!r}"
    )


async def loop_stop(handler: "CommandHandler") -> "CommandResult":
    """Cancel the active loop."""
    from ember_code.backend import command_handler as _handler

    CommandResult = _handler.CommandResult
    sess = handler._session
    if sess.pending_loop_prompt is None:
        return CommandResult.info("No loop is running.")
    done = sess.loop_iteration_index
    await sess.cancel_loop()
    return CommandResult.info(f"Loop stopped after {done} iteration{'s' if done != 1 else ''}.")
