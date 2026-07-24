"""Tool permission settings — Claude Code-style allow/ask/deny with argument rules.

Package facade. Re-exports the OOP collaborators so external callers
(``core.pool``, ``core.tools.registry``, ``core.agents.builder``,
``core.session.core``, ``core.session.agent_builder.tools_builder``,
``backend.server``, ``backend.schemas_hitl``, ``backend.hitl_controller``,
plus the test suite) continue to import from
``ember_code.core.config.tool_permissions`` unchanged.

Module layout:

* :mod:`schemas`             — Pydantic types + polymorphic :class:`RuleArgPattern`
* :mod:`tool_invocation`     — :class:`ToolInvocation` value object
                               (owns ``exact_rule`` / ``pattern_rule``)
* :mod:`tool_name_resolver`  — Agno function-name → catalog tool-name
                               resolver (owns the ``FUNC_TO_TOOL`` table)
* :mod:`settings_files`      — :class:`SettingsFileLoader` +
                               :class:`SettingsFileWriter` (disk I/O)
* :mod:`store`               — :class:`ToolPermissions` orchestrator

Reads from (highest priority last):

1. ``~/.ember/settings.json`` (user global defaults)
2. ``~/.ember/settings.local.json`` (user local overrides, runtime saves)
3. ``.ember/settings.json`` (project overrides, committed)
4. ``.ember/settings.local.json`` (project local overrides)

Format::

    {
      "permissions": {
        "allow": [
          "Read",
          "Grep",
          "Bash(git status)",
          "Bash(git diff:*)",
          "WebFetch(domain:github.com)"
        ],
        "ask": ["Bash", "Write", "Edit"],
        "deny": ["WebSearch"]
      }
    }

Rules:

* ``ToolName``              — matches all calls to that tool
* ``ToolName(exact args)``  — matches specific arguments
* ``ToolName(prefix:*)``    — matches arguments starting with prefix
* ``ToolName(key:value)``   — matches a specific key in the tool args dict
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ember_code.core.config.tool_permissions.schemas import (
    BarePattern,
    CategoryToToolMap,
    DomainPattern,
    EmberSettingsPermissionsFile,
    GlobPattern,
    KeyValueGlobPattern,
    LoadResult,
    PathPattern,
    PermissionLevel,
    PermissionRule,
    RuleArgPattern,
    ToolInvocationArgs,
    ToolPermissionDefaults,
)
from ember_code.core.config.tool_permissions.settings_files import (
    SettingsFileLoader,
    SettingsFileWriter,
)
from ember_code.core.config.tool_permissions.store import ToolPermissions
from ember_code.core.config.tool_permissions.tool_invocation import ToolInvocation
from ember_code.core.config.tool_permissions.tool_name_resolver import ToolNameResolver

# Live read-only view over :class:`ToolNameResolver`'s canonical map.
# Consumed by `test_tool_permissions.py` to pin the func-name → tool
# mapping without importing the class-private ``_MAP`` attribute.
FUNC_TO_TOOL: Mapping[str, str] = MappingProxyType(ToolNameResolver._MAP)


__all__ = [
    # Primary public surface
    "PermissionLevel",
    "PermissionRule",
    "ToolInvocation",
    "ToolInvocationArgs",
    "ToolNameResolver",
    "ToolPermissions",
    "ToolPermissionDefaults",
    # Rule pattern hierarchy
    "RuleArgPattern",
    "BarePattern",
    "DomainPattern",
    "GlobPattern",
    "KeyValueGlobPattern",
    "PathPattern",
    # Settings file I/O
    "EmberSettingsPermissionsFile",
    "LoadResult",
    "SettingsFileLoader",
    "SettingsFileWriter",
    # Category mapping
    "CategoryToToolMap",
    "FUNC_TO_TOOL",
]
