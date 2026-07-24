"""Typed schemas for the auth slash-command family.

Home for :class:`LogoutOutcome`, the composed result that
:meth:`ember_code.backend.cmd_auth.AuthCommand.logout` builds before
lowering into a wire :class:`CommandResult`. The coordinator used to
accumulate a bare ``list[str]`` and ``"\\n".join`` at the tail — the
list carried three orthogonal concerns (identity confirmation,
fallback-switch confirmation, warning) with no schema. Splitting the
data out here keeps the coordinator readable and the presentation
step ("stitch identity + fallback/warning") in one auditable place.

Sibling convention: matches :mod:`schemas_agents`,
:mod:`schemas_config`, :mod:`schemas_context`, … — one schemas
module per top-level backend concern.
"""

from __future__ import annotations

from pydantic import BaseModel

from ember_code.backend.command_result import CommandResult
from ember_code.protocol.messages import CommandAction, CommandResultKind


class CloudSwitchOutcome(BaseModel):
    """Result of the "swap-off-cloud-if-current-model-was-cloud" step
    inside ``/logout``.

    Exactly one of the three fields is set at a time — the model
    enforces the invariant that used to live only in a docstring
    over a bare ``tuple[str | None, str | None]`` return.
    """

    fallback_model: str | None = None
    warning: str | None = None
    switched: bool = False

    @classmethod
    def no_switch_needed(cls) -> CloudSwitchOutcome:
        return cls()

    @classmethod
    def switched_to(cls, name: str) -> CloudSwitchOutcome:
        return cls(fallback_model=name, switched=True)

    @classmethod
    def cloud_but_no_fallback(cls, message: str) -> CloudSwitchOutcome:
        return cls(warning=message)


class LogoutOutcome(BaseModel):
    """Composed result of a ``/logout`` invocation.

    Fields:

    * ``identity_message`` — either ``"Logged out (email)."`` when
      credentials were on disk, or ``"Not logged in."`` when the
      user hit ``/logout`` while unauthenticated. Always populated.
    * ``fallback_model`` — the model name we switched to when the
      currently-selected model was cloud-backed. ``None`` when no
      switch was needed (current model wasn't cloud) OR no
      non-cloud model was available (in which case
      ``warning`` is populated instead).
    * ``warning`` — user-facing warning string when current model
      was cloud but no non-cloud fallback exists. Mutually exclusive
      with ``fallback_model``: either we switched successfully or
      we warned; never both.

    :meth:`to_command_result` owns the string-joining presentation
    step so the caller doesn't reach into these fields to format.
    """

    identity_message: str
    fallback_model: str | None = None
    warning: str | None = None

    def to_command_result(self) -> CommandResult:
        """Lower the outcome into a wire :class:`CommandResult`.

        Preserves the pre-refactor ordering exactly: identity line
        first, then the fallback-switch confirmation or the warning
        (whichever is populated). Empty second-line case is legal —
        current model wasn't cloud, so no switch was needed.
        """
        lines: list[str] = [self.identity_message]
        if self.fallback_model is not None:
            lines.append(f"Switched to {self.fallback_model} (cloud model no longer available).")
        elif self.warning is not None:
            lines.append(self.warning)
        return CommandResult(
            kind=CommandResultKind.INFO,
            content="\n".join(lines),
            action=CommandAction.LOGOUT,
        )
