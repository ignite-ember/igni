"""Shared :class:`CommandResult` — the single slash-command result type.

This module owns the ONE Python class every slash command returns.
It subclasses the wire model
:class:`ember_code.protocol.messages.CommandResult`, so instances are
already wire-shaped Pydantic messages (``type: "command_result"``,
enum-typed ``kind`` / ``action`` fields, ``content`` /
``display_content`` payload). :meth:`BackendServer.handle_command`
returns the subclass directly — no more rebuild-a-twin conversion.

Layering rationale — the backend class *inherits* from the wire
class rather than the other way around because the protocol layer
is a leaf (no imports from ``backend``). The wire class stays
canonical in ``protocol/messages.py`` where FE parsers, dispatch
tables, and cross-process consumers register it. This module adds
the constructor factories + behaviour methods on top so callers
still write ``CommandResult.info(...)`` / ``.for_action(...)`` at
the call site.

Cycle-break rationale — the file lives in ``backend/`` (not
alongside the dispatcher in ``command_handler.py``) so every
``cmd_*.py`` sibling can import :class:`CommandResult` at module
top per Rule 2 without a back-edge into
:mod:`ember_code.backend.command_handler`.

OOP surface — the class is the Rule-6 owner of every "how do I
build a CommandResult?" answer *and* the Rule-6 owner of the "what
does this result mean?" queries the FE bridge asks:

* Four semantic factories — :meth:`markdown`, :meth:`info`,
  :meth:`error`, :meth:`fork` — carry data (the text or the new
  session id) in ``content``.
* One generic factory — :meth:`for_action` — replaces the 14
  action-only shortcuts (``quit`` / ``clear`` / ``sessions`` /
  ``model`` / ``login`` / ``mcp`` / ``plugins`` / ``agents`` /
  ``skills`` / ``knowledge`` / ``codeindex`` / ``hooks`` /
  ``loop`` / ``watcher``) that used to hand-dispatch over the
  :class:`CommandAction` enum.
* Three query methods — :meth:`is_error`, :meth:`is_action`,
  :meth:`render_line` — encode "how do I read a result?" as
  methods on the class instead of ad-hoc call-site logic that
  reached in via ``getattr(result, ...)``.
"""

from __future__ import annotations

from ember_code.protocol.messages import CommandAction, CommandResultKind
from ember_code.protocol.messages import CommandResult as WireCommandResult


class CommandResult(WireCommandResult):
    """The single slash-command result type.

    Subclasses the wire model so instances serialise directly onto
    the BE→FE channel. Inherits the wire fields (``kind``,
    ``content``, ``action``, ``display_content``) and adds the
    factory / query surface below.
    """

    # ── Semantic factories ──────────────────────────────────────────

    @classmethod
    def markdown(cls, text: str) -> CommandResult:
        """Render ``text`` as a rich markdown block in chat."""
        return cls(kind=CommandResultKind.MARKDOWN, content=text)

    @classmethod
    def info(cls, text: str) -> CommandResult:
        """Single-line dim chat line — status updates, confirmations."""
        return cls(kind=CommandResultKind.INFO, content=text)

    @classmethod
    def error(cls, text: str) -> CommandResult:
        """Single-line red chat line — user-facing error."""
        return cls(kind=CommandResultKind.ERROR, content=text)

    @classmethod
    def fork(cls, new_session_id: str) -> CommandResult:
        """Session was duplicated — carries the new id in ``content``.

        The new session id rides in ``content`` so the FE has
        everything it needs to switch + load history from one
        round-trip.
        """
        return cls(
            kind=CommandResultKind.ACTION,
            action=CommandAction.FORK,
            content=new_session_id,
        )

    # ── Generic action factory ──────────────────────────────────────

    @classmethod
    def for_action(cls, action: CommandAction, content: str = "") -> CommandResult:
        """Emit an ACTION result for ``action`` (optionally carrying ``content``).

        Replaces the fourteen action-only shortcut factories
        (``quit`` / ``clear`` / ``sessions`` / ``model`` / ``login``
        / ``mcp`` / ``plugins`` / ``agents`` / ``skills`` /
        ``knowledge`` / ``codeindex`` / ``hooks`` / ``loop`` /
        ``watcher``) that used to hand-dispatch over the
        :class:`CommandAction` enum. Producers now pass the enum
        member directly, so the enum itself is the single truth for
        the action catalog.
        """
        return cls(kind=CommandResultKind.ACTION, action=action, content=content)

    # ── Query methods ───────────────────────────────────────────────

    def is_error(self) -> bool:
        """True when this result carries an error to be rendered red."""
        return self.kind == CommandResultKind.ERROR

    def is_action(self) -> bool:
        """True when this result asks the FE to dispatch on ``action``.

        FE bridges branch first on ``is_action()`` (dispatch panel
        / picker / quit / etc.) before falling back to rendering
        ``content`` off ``kind``.
        """
        return self.kind == CommandResultKind.ACTION or self.action != CommandAction.NONE

    def render_line(self) -> str:
        """Pick the text the FE should show in chat for this result.

        ``display_content`` wins when set (the ``/loop`` command
        uses it to show the unwrapped user prompt while ``content``
        carries the wrapped meta-prompt for the agent); otherwise
        fall back to ``content``.
        """
        return self.display_content or self.content


__all__ = ["CommandResult"]
