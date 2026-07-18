"""Plugin-agent security envelope (CC parity, row 37).

Plugin-shipped agents are not allowed to declare their own hooks,
mcpServers, or permissionMode — they'd let a plugin escalate its
own privileges. This module encapsulates the enforcement layer as
:class:`PluginRestrictionPolicy`, taking over from the old free
function ``_apply_plugin_restrictions``.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from ember_code.core.agents.schemas import (
    _PLUGIN_RESTRICTED_FRONTMATTER_KEYS,
    AgentDefinition,
)

logger = logging.getLogger(__name__)


class PluginRestrictionPolicy:
    """Strip restricted fields from a plugin-shipped agent
    definition and force ``force_isolation="worktree"`` so its
    spawns run in a fresh worktree.

    ``RESTRICTED_KEYS`` is the class-level source of truth for the
    key blocklist — same set as the (deprecated) module constant
    ``_PLUGIN_RESTRICTED_FRONTMATTER_KEYS`` in ``schemas.py``.
    """

    RESTRICTED_KEYS: ClassVar[frozenset[str]] = _PLUGIN_RESTRICTED_FRONTMATTER_KEYS

    @classmethod
    def strict(cls) -> PluginRestrictionPolicy:
        """Default constructor for the enforcing policy."""
        return cls()

    def apply(
        self,
        definition: AgentDefinition,
        raw_keys: set[str],
        plugin_name: str = "",
    ) -> AgentDefinition:
        """Return a restricted copy of ``definition``.

        Restricted keys present in ``raw_keys`` (typically obtained
        from :meth:`AgentMarkdownFile.raw_frontmatter_keys`) trigger
        a single warning per agent so plugin authors can fix their
        manifests and a security audit can see the attempted
        escalation in the logs.
        """
        declared_restricted = raw_keys & self.RESTRICTED_KEYS
        if declared_restricted:
            agent_id = f"{plugin_name}:{definition.name}" if plugin_name else definition.name
            logger.warning(
                "Plugin agent %s declared restricted frontmatter keys %s — "
                "these are stripped (CC parity, row 37). Plugin-shipped "
                "agents cannot declare their own hooks, mcpServers, or "
                "permissionMode.",
                agent_id,
                sorted(declared_restricted),
            )
        return definition.model_copy(
            update={
                "mcp_servers": [],
                "force_isolation": "worktree",
            }
        )


__all__ = ["PluginRestrictionPolicy"]
