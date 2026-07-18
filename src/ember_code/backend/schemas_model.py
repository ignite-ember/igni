"""Typed view models for the ``/model`` slash command's chat output.

Extracted from :mod:`ember_code.backend.command_handler` — the
old ``_cmd_model`` inline body assembled ``"Switched to model: X"``
and the "unknown model" error string procedurally. Both now flow
through :class:`ModelSwitchResult`, the typed return of the new
public :meth:`Session.set_default_model` method.
"""

from __future__ import annotations

from pydantic import BaseModel

from ember_code.backend.command_result import CommandResult
from ember_code.protocol.messages import CommandAction, CommandResultKind


class ModelSwitchResult(BaseModel):
    """Pattern-3 result envelope returned by
    :meth:`Session.set_default_model`.

    Callers stop try/except-ing on unknown model names — they
    branch on :attr:`ok` and let :meth:`to_command_result` produce
    the right :class:`CommandResult` (info + ``model_switched``
    action on success, error on failure).
    """

    ok: bool
    model_name: str
    available: list[str] = []
    error: str | None = None

    def to_command_result(self) -> CommandResult:
        if not self.ok:
            avail = ", ".join(sorted(self.available))
            reason = self.error or f"Unknown model: '{self.model_name}'."
            return CommandResult.error(f"{reason} Available: {avail}")
        # ``action="model_switched"`` tells the FE to refresh the
        # status-bar model slot. Without it the bar showed the OLD
        # model after ``/model <name>`` direct switches — nothing
        # else triggers a refresh on that code path. (Live cross-
        # cutting constraint — preserved verbatim from the pre-
        # refactor implementation.)
        return CommandResult(
            kind=CommandResultKind.INFO,
            content=f"Switched to model: {self.model_name}",
            action=CommandAction.MODEL_SWITCHED,
        )


__all__ = ["ModelSwitchResult"]
