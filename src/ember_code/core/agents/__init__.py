"""Agent discovery, build, and pool management.

Public surface — re-exports the canonical class names so external
callers can ``from ember_code.core.agents import AgentPool`` without
knowing the internal layout. Mirrors the sibling packages
``core/mcp/`` and ``core/plugins/``.
"""

from ember_code.core.agents.builder import AgentBuilder
from ember_code.core.agents.compat import PoolCompatShim
from ember_code.core.agents.ephemeral import EphemeralAgentStore
from ember_code.core.agents.loader import AgentDefinitionLoader
from ember_code.core.agents.markdown import AgentMarkdownFile
from ember_code.core.agents.plugin_policy import PluginRestrictionPolicy
from ember_code.core.agents.pool import AgentPool
from ember_code.core.agents.schemas import (
    AgentBuildContext,
    AgentConstructorArgs,
    AgentDefinition,
    AgentEntry,
    AgentInfo,
    AgentPriority,
    LoadError,
    LoadReport,
)

__all__ = [
    "AgentBuildContext",
    "AgentBuilder",
    "AgentConstructorArgs",
    "AgentDefinition",
    "AgentDefinitionLoader",
    "AgentEntry",
    "AgentInfo",
    "AgentMarkdownFile",
    "AgentPool",
    "AgentPriority",
    "EphemeralAgentStore",
    "LoadError",
    "LoadReport",
    "PluginRestrictionPolicy",
    "PoolCompatShim",
]
