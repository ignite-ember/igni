"""Context-related slash commands: ``/output-style``, ``/compact``, ``/ctx``.

Extracted from :mod:`ember_code.backend.command_handler` — three
commands that inspect / mutate the session's context surface:

* ``/output-style`` — list / set / show the active output style
  (CC parity, row 52). Hot-patches the agent's instructions so
  the next turn picks up the new tone without a rebuild.
* ``/compact`` — force a compaction pass (drop conversation
  runs, keep the summary + system floor).
* ``/ctx`` — break down the current context counter into
  conversation vs. floor, so users can see why `/compact`
  doesn't drop the meter to zero.

Output-style body files live at ``.ember/output-styles/<name>.md``
(project) or ``~/.ember/output-styles/<name>.md`` (user), plus
the ``.claude/`` equivalents when cross-tool reads are enabled.

Architecture: the three verbs are methods on a single
:class:`ContextCommand` coordinator, dispatched via a
``match`` inside :meth:`ContextCommand.output_style`. Presentation
lives in the sibling :mod:`schemas_context` module — every
``.to_command_result()`` render call flows through a typed view.
The public ``cmd_output_style`` / ``cmd_compact`` / ``cmd_ctx``
entry points are two-line shims so
:mod:`ember_code.backend.command_handler`'s dispatch table stays
intact.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ember_code.backend.command_result import CommandResult
from ember_code.backend.schemas_context import (
    ContextBreakdownView,
    OutputStylesListView,
    OutputStyleStatusView,
)
from ember_code.protocol.messages import CommandAction, CommandResultKind

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler
    from ember_code.core.session import Session


class ContextCommand:
    """Coordinator for the ``/output-style`` / ``/compact`` / ``/ctx``
    slash-command family.

    Holds a :class:`Session` reference and exposes each verb as a
    bound method. Constructed per invocation so the coordinator
    stays stateless between calls (nothing outlives one dispatch).

    The class accepts a ``Session`` directly rather than the
    :class:`CommandHandler` state object, so we don't reach into
    ``handler._session`` from inside the coordinator (Rule 6: no
    private-attribute reach-in). The active output style is read
    via the public :attr:`Session.active_output_style` property.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ── Verb methods ─────────────────────────────────────────────

    async def output_style(self, args: str) -> CommandResult:
        """List / set / show the active output style.

        Verb parsing routes on the first whitespace-separated
        token via a ``match`` block:

        * ``""`` / ``list`` → list view
        * ``status`` / ``show`` → status view
        * ``set <name>`` → switch to ``<name>``
        * anything else → treat the entire arg string as the
          target style name (so ``/output-style explanatory``
          and ``/output-style set explanatory`` both land on the
          same set path).
        """
        normalized = args.strip()
        cmd, _, rest = normalized.partition(" ")
        cmd = cmd.lower()

        match cmd:
            case "" | "list":
                if normalized in ("", "list"):
                    return self._render_list()
                # ``list`` prefix followed by junk falls through
                # to the set path so we don't silently swallow it.
                return await self._set_style(normalized)
            case "status" | "show":
                return self._render_status()
            case "set":
                return await self._set_style(rest.strip())
            case _:
                # Bare ``/output-style <name>`` — treat the whole
                # normalized string as the target style name.
                return await self._set_style(normalized)

    async def compact(self) -> CommandResult:
        """Force a compaction pass.

        Returns the summary as a separate field so the FE can
        render a structured "Context compacted" card with the
        model-generated summary as the body. This is a bare
        :class:`CommandResult` (not a view render) because the
        payload is an ACTION result with no markdown template to
        move — same shape as :meth:`CodeIndexCommand.clean`.
        """
        result = await self._session.force_compact()
        return CommandResult(
            kind=CommandResultKind.ACTION,
            action=CommandAction.COMPACT,
            content=result.status,
            display_content=result.summary,
        )

    async def ctx(self) -> CommandResult:
        """Break down the current ctx counter into floor vs conversation.

        ``/compact`` only clears the conversational runs — system
        prompt, tool schemas, project rules, memories and the
        injected session summary stay. ``/ctx`` shows the split so
        the user can see why the meter doesn't drop to zero after
        compaction.
        """
        breakdown = await self._session.context_breakdown()
        return ContextBreakdownView.from_domain(breakdown).to_command_result()

    # ── Private helpers ──────────────────────────────────────────

    def _render_list(self) -> CommandResult:
        return OutputStylesListView(
            styles=self._session.output_styles,
            active=self._session.active_output_style,
        ).to_command_result()

    def _render_status(self) -> CommandResult:
        return OutputStyleStatusView(
            active=self._session.active_output_style,
        ).to_command_result()

    async def _set_style(self, target_name: str) -> CommandResult:
        if not target_name:
            return CommandResult.error(
                "Usage: /output-style <name> (or `/output-style list` to see options)."
            )
        status_line = self._session.set_output_style(target_name)
        if status_line.startswith("Error"):
            return CommandResult.error(status_line)
        return CommandResult.info(status_line)


# ── Public shim entry points ─────────────────────────────────────
#
# Two-line shims preserved verbatim so
# :mod:`ember_code.backend.command_handler` keeps importing these
# by name and calling them with ``(self, ...)``. All real work
# lives on :class:`ContextCommand`.


async def cmd_output_style(handler: CommandHandler, args: str) -> CommandResult:
    """List / set / show the active output style."""
    return await ContextCommand(handler.session).output_style(args)


async def cmd_compact(handler: CommandHandler) -> CommandResult:
    """Force a compaction pass."""
    return await ContextCommand(handler.session).compact()


async def cmd_ctx(handler: CommandHandler) -> CommandResult:
    """Break down the current ctx counter into floor vs conversation."""
    return await ContextCommand(handler.session).ctx()
