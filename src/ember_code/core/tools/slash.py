"""SlashCommand — agent-facing re-entrant slash command tool.

Claude Code parity (the ``SlashCommand`` tool). Lets the agent
invoke any slash command from inside a tool-using turn — useful
for "go look up the codeindex for X and tell me what's there" or
"compact the conversation, you're going to be at this for a
while" without forcing the user to type the command themselves.

Threat model and why we still block a small list:

* User-interactive commands (``/quit``, ``/exit``, ``/clear``,
  ``/login``, ``/logout``) and turn-invalidating ones
  (``/model``) wouldn't take effect through this path anyway
  (the UI's dispatcher is what acts on ``CommandAction``s), but
  silently no-op'ing on them confuses the agent. Refusing with
  a short message is clearer.
* ``/compact`` is intentionally NOT blocked — letting a long-
  running agent decide to compact mid-turn is a feature, not a
  footgun.
* Markdown commands and skills that return ``RUN_PROMPT`` come
  back to the agent as their rendered prompt text — same content
  the user would have seen if they'd typed the command.

The tool dispatches through ``CommandHandler.handle`` directly
(not via the session-level ``dispatch`` wrapper), so
``CommandAction.QUIT`` / ``CLEAR`` etc. that would otherwise
SystemExit the process don't fire here. The action is recorded
on the returned ``CommandResult`` but discarded.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agno.tools import Toolkit

if TYPE_CHECKING:
    from ember_code.core.session.core import Session

logger = logging.getLogger(__name__)


# Refused with an explanatory error rather than executed.
# Everything else (info commands, codeindex / knowledge searches,
# compact, schedule, loop, etc.) goes through unchanged.
_BLOCKED_COMMANDS: frozenset[str] = frozenset(
    {
        "/quit",
        "/exit",
        "/clear",
        "/login",
        "/logout",
        "/model",
    }
)


class SlashCommandTool(Toolkit):
    """Single-method toolkit: ``slash_command``."""

    def __init__(self, session: Session) -> None:
        super().__init__(name="ember_slash_command")
        self._session = session
        self.register(self.slash_command)

    async def slash_command(self, command: str) -> str:
        """Invoke a slash command and return what it produced.

        ``command`` is the full command line with the leading
        slash (``"/help"``, ``"/ctx"``, ``"/codeindex search
        retry logic"``, etc.). Use this when you need to consult
        a built-in command's output (the codeindex / knowledge
        searches are the headline use case) or to trigger a
        side-effect command (``/compact``, ``/schedule``).

        Blocked commands (return an explanatory error instead of
        executing): ``/quit``, ``/exit``, ``/clear``, ``/login``,
        ``/logout``, ``/model``. These would either kill the
        session, invalidate the current turn, or require UI
        interaction the agent can't provide.

        Markdown commands and skills that produce a
        ``RUN_PROMPT`` come back as their rendered prompt text —
        treat the return value as "what the user would have seen
        if they'd typed this command themselves".
        """
        # Late import — keeps this module loadable without the
        # full backend tree (avoids circular-import noise during
        # tests that instantiate the toolkit directly).
        from ember_code.backend.command_handler import CommandHandler

        raw = (command or "").strip()
        if not raw:
            return "Error: empty command"
        # Normalize: agent might omit the slash or send case-shifted.
        if not raw.startswith("/"):
            raw = "/" + raw
        head = raw.split()[0].lower()

        if head in _BLOCKED_COMMANDS:
            return (
                f"Error: {head} is not invocable via the slash_command tool — it "
                "would end the session, invalidate the current turn, or require "
                "user interaction. Ask the user to run it manually if needed."
            )

        try:
            result = await CommandHandler(self._session).handle(raw)
        except Exception as exc:
            logger.warning("slash_command %s raised: %s", head, exc)
            return f"Error invoking {head}: {exc}"

        # ``CommandResult.error`` returns kind=ERROR with content
        # set to the message. Surface it as a string the agent
        # sees, prefixed so it reads as an error not as content.
        from ember_code.protocol.messages import CommandResultKind

        if result.kind == CommandResultKind.ERROR:
            return f"Error: {result.content}"
        # Markdown / skill RUN_PROMPT actions return the rendered
        # template body as ``content`` — exactly what the model
        # wants to see (the expanded prompt). Info / markdown /
        # success kinds also expose their payload as ``content``.
        return result.content or "(no output)"
