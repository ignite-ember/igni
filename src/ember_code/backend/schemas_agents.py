"""Typed view models for the ``/agents`` slash command's chat output.

Extracted from :mod:`ember_code.backend.command_handler` — the
old ``_cmd_agents`` inline body built markdown strings inside the
handler and returned raw :class:`CommandResult` objects. Every
markdown template that the :class:`AgentsCommand` coordinator emits
now lives here as a Pydantic view model with a single
``.to_command_result()`` render entry point. Mirrors the sibling
:mod:`schemas_codeindex` pattern.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from ember_code.backend.command_result import CommandResult

if TYPE_CHECKING:
    from ember_code.core.agents.schemas import AgentDefinition


class PromoteResult(BaseModel):
    """Outcome of :meth:`AgentPool.promote_ephemeral` wrapped as a
    typed result envelope. The pool itself still raises (tests
    lock that in) — the coordinator catches once and packages it.
    """

    ok: bool
    destination: str | None = None
    error: str | None = None

    def to_command_result(self, name: str) -> CommandResult:
        if self.ok:
            return CommandResult.info(f"Promoted '{name}' to {self.destination}")
        return CommandResult.error(self.error or "unknown error")


class DiscardResult(BaseModel):
    """Outcome of :meth:`AgentPool.discard_ephemeral` — same shape
    as :class:`PromoteResult` minus the destination field.
    """

    ok: bool
    error: str | None = None

    def to_command_result(self, name: str) -> CommandResult:
        if self.ok:
            return CommandResult.info(f"Discarded ephemeral agent '{name}'.")
        return CommandResult.error(self.error or "unknown error")


class EphemeralAgentsView(BaseModel):
    """Wraps the ``list[AgentDefinition]`` returned by
    :meth:`AgentPool.list_ephemeral`. Empty list renders as an
    info result; non-empty renders the markdown catalogue that
    the old inline body assembled.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    agents: list[AgentDefinition]

    def to_command_result(self) -> CommandResult:
        if not self.agents:
            return CommandResult.info("No ephemeral agents.")
        lines = "## Ephemeral Agents\n"
        for defn in self.agents:
            tools = ", ".join(defn.tools) if defn.tools else "none"
            lines += f"- **{defn.name}** — {defn.description}\n  tools: {tools}\n"
        lines += "\n*`/agents promote <name>` to save · `/agents discard <name>` to remove*\n"
        return CommandResult.markdown(lines)


__all__ = ["PromoteResult", "DiscardResult", "EphemeralAgentsView"]
