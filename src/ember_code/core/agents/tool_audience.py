"""Which agents may use an MCP server, when it is not all of them.

Every agent with an empty ``mcp_servers`` gets every connected
server's tools. For most servers that is right. For some it breaks
a rule the capability depends on: a document store whose writes
must declare their sources gives its tools to *one* agent — the one
whose job is to weigh that declaration — because "an agent that
could grant itself the capability is the same as having no rule",
and because a trace calling the tool must belong to one role.

igni does not know what extensions exist, so the rule has to arrive
as a declaration and be enforced without a name in it.

**The declaration is ``toolAudience: own-agents``.** The extension
ships its agents in the plugin it carries, which igni installs at
``<plugins>/<extension-id>/``. So "its own agents" is a fact igni can
check by path — is this agent's file inside that directory — without
knowing which extension it is, what the agent is called, or that a
document store is involved.

``core`` owns the table and the extension system registers into it,
the shape :mod:`ember_code.core.prompts.contributions` uses, which keeps
``core`` from importing the extension system.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class ToolAudience:
    """MCP server name -> the only directory whose agents may use it."""

    def __init__(self) -> None:
        self._restricted: dict[str, Path] = {}

    def restrict(self, server: str, agents_from: Path) -> None:
        """Offer ``server`` only to agents defined under ``agents_from``."""
        self._restricted[server] = agents_from.resolve()
        logger.debug("%s is offered only to agents under %s", server, agents_from)

    def release(self, server: str) -> None:
        """Forget the restriction — the server is gone."""
        self._restricted.pop(server, None)

    def allows(self, server: str, agent_source: Path | None) -> bool:
        """May an agent defined at ``agent_source`` use ``server``?

        Unrestricted servers are open to everyone, which is MCP's
        ordinary behaviour and what most extensions want. A
        restricted one is refused to any agent with no file at all —
        the main agent, an ephemeral one — because "I could not tell
        where you came from" must not read as "you came from the
        right place".
        """
        directory = self._restricted.get(server)
        if directory is None:
            return True
        if agent_source is None:
            return False
        return agent_source.resolve().is_relative_to(directory)


#: The table the agent builder reads when nobody passes one.
audience = ToolAudience()

__all__ = ["ToolAudience", "audience"]
