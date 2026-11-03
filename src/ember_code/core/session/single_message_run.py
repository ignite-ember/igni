"""Non-interactive session runner — the OOP peer of
:class:`InteractiveSessionLoop`.

Both coordinators subclass :class:`SessionRun`, so the SessionStart
/ SessionEnd hook emit sites and the ``@`` mention + MediaResolver
+ ``handle_message`` + :class:`RunStats` pipeline live in one place
(``session_run.py``). This module adds only what's unique to the
single-shot case:

* :meth:`SingleMessageRun._dispatch_slash` — what the turn is. Wraps
  :class:`CommandHandler`: a built-in command prints its result and is
  the whole run; a skill, a markdown command or ``/loop <prompt>`` hands
  back its prompt, which runs as the turn, as at the interactive prompt.
* :meth:`SingleMessageRun.run` — the top-level orchestration:
  SessionStart hook, slash-dispatch, :meth:`SessionRun._run_turn`,
  SessionEnd hook, and the exit code the turn earned.

.. note::

    ``runner.py`` keeps a module-level ``async def
    run_single_message`` shim so the CLI's
    ``_session_module.run_single_message`` pattern (and any
    ``patch("ember_code.core.session.runner.run_single_message")``
    test target) continues to work.
"""

from __future__ import annotations

import logging

from ember_code.backend.command_handler import CommandHandler
from ember_code.core.session.session_run import SessionRun
from ember_code.protocol.messages import CommandAction

logger = logging.getLogger(__name__)


class SingleMessageRun(SessionRun):
    """Runs one message end-to-end and exits.

    Subclasses :class:`SessionRun` so the base owns
    :class:`Session` construction, the SessionStart / SessionEnd
    hook emit sites, and the shared ``_run_turn`` pipeline. This
    class contributes only what's unique to the non-interactive
    path.
    """

    #: The exit code of a ``-m`` / ``-p`` run whose turn failed.
    FAILED_EXIT_CODE = 1

    # ── Slash-command dispatch ────────────────────────────────────

    async def _dispatch_slash(self, message: str) -> str | None:
        """What the model is sent for ``message``, or ``None`` if nothing.

        A plain message is sent as it is. A slash command runs through
        :class:`CommandHandler`: a skill, a markdown command and ``/loop
        <prompt>`` answer with the prompt to run (``RUN_PROMPT``), and that
        prompt is the turn, as at the interactive prompt. Any other command
        prints its result, and no turn runs.
        """
        if not message.startswith("/"):
            return message
        handler = CommandHandler(self._session)
        result = await handler.handle(message)
        if result.action == CommandAction.RUN_PROMPT:
            return result.content
        if result.content:
            self._session.display.print_info(result.content)
        elif result.action == CommandAction.HELP:
            # A bare ``/help`` opens a panel in the clients; headless,
            # its content is empty, so print what the panel would show.
            from ember_code.backend.cmd_help import help_overview

            self._session.display.print_markdown(help_overview())
        elif result.action == CommandAction.LOGIN:
            # ``/login`` is an action the clients carry out (a browser and a
            # callback); its result has no content, so ``igni -m /login``
            # printed nothing and exited 0, as if it had signed in.
            from ember_code.core.session.commands import InteractiveCommandDispatcher

            self._session.display.print_info(InteractiveCommandDispatcher.LOGIN_HINT)
        return None

    # ── Public entry point ────────────────────────────────────────

    async def run(self, message: str) -> None:  # type: ignore[override]
        """Run one message: extensions up → SessionStart → dispatch → SessionEnd → extensions down.

        A built-in slash command runs without a model turn. A skill, a
        markdown command and ``/loop <prompt>`` run their prompt as the
        turn, and everything else is the turn itself, through the shared
        :meth:`SessionRun._run_turn` pipeline; the exit code follows that
        turn. SessionEnd always fires exactly once.
        """
        # The extensions installed here apply to a headless run as they
        # do in the app: their tools, hooks and prompt contributions.
        # Imported here: the headless extensions reach the host channel,
        # whose model reaches the session package this module is in, so a
        # top-level import is a cycle for whoever imports the channel first.
        from ember_code.backend.headless_extensions import HeadlessExtensions

        extensions = HeadlessExtensions(self._session)
        await extensions.start()
        try:
            await self._fire_session_start()

            turn = await self._dispatch_slash(message)
            if turn is None:
                # A command that was the whole run: no model call.
                await self._fire_session_end()
                return

            succeeded = await self._run_turn(turn)
            self._emit_pending_plan()
            await self._fire_session_end()
        finally:
            await extensions.stop()
        if not succeeded:
            # The run stopped at a confirmation nobody here can answer,
            # found no model, or failed: said on stderr already, and the
            # exit code says it to whatever ran ``igni -m``.
            raise SystemExit(self.FAILED_EXIT_CODE)

    def _emit_pending_plan(self) -> None:
        """Print a plan the agent submitted, because nothing else will.

        ``--read-only`` selects plan mode, where a denied edit tells the model
        to "call ``exit_plan_mode(plan)`` when ready". That writes the plan to
        :class:`PlanStore`, and the only consumer is the frontend's plan card —
        ``approve_plan`` is an FE operation and ``/plan`` is an interactive
        command. Headlessly there is no card and no one to approve, so the run
        ended having produced a plan nobody could read: the session looked like
        it had simply declined to do the work.

        Printing it is the whole fix. A headless run is one-shot by definition —
        there is no second turn for an approval to unblock — so the useful
        outcome is the plan itself, delivered rather than filed.
        """
        store = getattr(self._session, "plan_store", None)
        plan = getattr(store, "latest", "") if store is not None else ""
        # Typed check rather than truthiness. ``latest`` is a plain attribute, so
        # anything holding the session loosely — the test doubles in this suite,
        # a partially built session — offers a truthy non-string here and turns a
        # reporting step into a TypeError at the end of an otherwise good run.
        if not isinstance(plan, str) or not plan.strip():
            return
        self._session.display.print_info(
            "\n".join(
                [
                    "",
                    "Plan (read-only session — nothing was executed):",
                    "",
                    plan,
                    "",
                    "Re-run without --read-only to carry it out.",
                ]
            )
        )
