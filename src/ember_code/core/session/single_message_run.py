"""Non-interactive session runner — the OOP peer of
:class:`InteractiveSessionLoop`.

Both coordinators subclass :class:`SessionRun`, so the SessionStart
/ SessionEnd hook emit sites and the ``@`` mention + MediaResolver
+ ``handle_message`` + :class:`RunStats` pipeline live in one place
(``session_run.py``). This module adds only what's unique to the
single-shot case:

* :meth:`SingleMessageRun._dispatch_slash` — the slash-command
  early-return branch. Wraps :class:`CommandHandler` and prints any
  non-empty result. It stays a method (rather than a full
  :class:`PromptHandler` chain) because the non-interactive path
  has exactly two branches — slash vs message — so a chain would
  be over-engineered here.
* :meth:`SingleMessageRun.run` — the top-level orchestration:
  SessionStart hook, slash-dispatch (early return), else
  :meth:`SessionRun._run_turn`, SessionEnd hook.

.. note::

    ``runner.py`` keeps a module-level ``async def
    run_single_message`` shim so the CLI's
    ``_session_module.run_single_message`` pattern (and any
    ``patch("ember_code.core.session.runner.run_single_message")``
    test target) continues to work.
"""

from __future__ import annotations

from ember_code.backend.command_handler import CommandHandler
from ember_code.core.session.session_run import SessionRun


class SingleMessageRun(SessionRun):
    """Runs one message end-to-end and exits.

    Subclasses :class:`SessionRun` so the base owns
    :class:`Session` construction, the SessionStart / SessionEnd
    hook emit sites, and the shared ``_run_turn`` pipeline. This
    class contributes only what's unique to the non-interactive
    path.
    """

    # ── Slash-command dispatch ────────────────────────────────────
    #
    # TODO: the four :class:`PromptHandler` subclasses in
    # ``interactive_loop.py`` are REPL-coupled (``_QuitHandler``
    # raises a private ``_LoopExit`` sentinel, ``_SkillHandler``
    # uses ``print_info``, ``_MessageHandler`` calls the loop's
    # ``_run_turn``). A future refactor could extract a headless
    # ``SlashCommandRouter`` collaborator both coordinators share,
    # but that's out of scope for this diff — the non-interactive
    # path only has two branches, so a full chain here would be
    # over-engineered.

    async def _dispatch_slash(self, message: str) -> bool:
        """Try to consume ``message`` as a slash command.

        Returns ``True`` when the message started with ``/`` and
        was handled by :class:`CommandHandler` (caller should
        early-return to the SessionEnd hook without calling the
        model); ``False`` otherwise.
        """
        if not message.startswith("/"):
            return False
        handler = CommandHandler(self._session)
        result = await handler.handle(message)
        if result.content:
            self._session.display.print_info(result.content)
        return True

    # ── Public entry point ────────────────────────────────────────

    async def run(self, message: str) -> None:  # type: ignore[override]
        """Run one message: SessionStart → dispatch → SessionEnd.

        Slash-prefixed messages take the ``_dispatch_slash`` branch
        and intentionally skip ``_run_turn`` — they never reach the
        model. Non-slash messages flow through the shared
        :meth:`SessionRun._run_turn` pipeline. SessionEnd always
        fires exactly once.
        """
        await self._fire_session_start()

        if await self._dispatch_slash(message):
            # Slash branch: no model call. Jump straight to SessionEnd.
            await self._fire_session_end()
            return

        await self._run_turn(message)
        await self._fire_session_end()
