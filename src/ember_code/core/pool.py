"""Backward-compatibility shim over :mod:`ember_code.core.agents`.

Deprecated — new code MUST import from
:mod:`ember_code.core.agents`. This module exists only to keep
four legacy test files and any out-of-tree plugins that reach
for ``from ember_code.core.pool import ...`` working during the
deprecation window.

The whole deprecation surface lives on
:class:`ember_code.core.agents.compat.PoolCompatShim` — one
class, four classmethods, one :class:`ClassVar` blocklist. This
module just binds the deprecated names to those classmethods so
``from ember_code.core.pool import parse_agent_file`` (etc.)
resolves without a second implementation.

Note: :mod:`ember_code.core.agents.pool_legacy` is a *separate*
legacy shim living inside the ``core/agents/`` package (the
:class:`LegacyAgentPoolMixin` for :class:`AgentPool` reach-in
attribute compat). It is intentionally out of scope here — its
own removal PR belongs on a different audit target.

Documented resolution order (highest priority wins on name
collision). Within the same scope, native Ember sources beat
cross-tool Claude sources by +1::

    10  ephemeral agents created at runtime via ``create_ephemeral``
     4  <project>/.ember/agents/          (project, native)
     3  <project>/.ember/agents.local/    (project personal, gitignored)
     2  <project>/.claude/agents/         (project, cross-tool)
     1  ~/.ember/agents/                  (user, native)
     0  ~/.claude/agents/                 (user, cross-tool)
"""

from __future__ import annotations

import warnings

# Canonical re-exports — pinned to an explicit list rather than
# ``from ember_code.core.agents import *`` so this shim's public
# surface is immune to future changes in ``agents/__init__.__all__``.
from ember_code.core.agents import (
    AgentBuilder,
    AgentDefinition,
    AgentEntry,
    AgentInfo,
    AgentMarkdownFile,
    AgentPool,
    AgentPriority,
    EphemeralAgentStore,
    LoadError,
    LoadReport,
    PluginRestrictionPolicy,
    PoolCompatShim,
)
from ember_code.core.agents import plugin_policy as _plugin_policy_mod
from ember_code.core.agents.schemas import (
    _PLUGIN_RESTRICTED_FRONTMATTER_KEYS,
    AgentBuildContext,
)

# Deprecation nudge for downstream importers. Scoped to
# ``DeprecationWarning`` so it surfaces under ``-W default`` /
# pytest without spamming end-user runs.
warnings.warn(
    "ember_code.core.pool is deprecated; import from "
    "ember_code.core.agents instead. This shim will be removed "
    "in a follow-up release.",
    DeprecationWarning,
    stacklevel=2,
)

# ``pool.logger`` points at the canonical WARN emitter — the same
# singleton :class:`PluginRestrictionPolicy` writes through — so
# ``tests/test_plugin_agent_restrictions.py``'s
# ``monkeypatch.setattr(pool_mod.logger, "warning", ...)`` lands
# on the right object. Not a rebind of the ``logger`` name in
# the plugin_policy module — a direct reference to the same
# ``logging.Logger`` instance.
logger = _plugin_policy_mod.logger

# ── Legacy deprecated names ─────────────────────────────────────
#
# Each of these was a free function in the old shim; today they
# all delegate to :class:`PoolCompatShim` classmethods so the
# implementation lives in one place. Legacy test decorators
# ``@patch("ember_code.core.pool.Agent")`` etc. no longer
# resolve here — the canonical seam is
# :attr:`AgentBuilder._agent_cls` (see the builder docstring).
parse_agent_file = PoolCompatShim.parse_agent_file
_raw_frontmatter_keys = PoolCompatShim.raw_frontmatter_keys
_apply_plugin_restrictions = PoolCompatShim.apply_plugin_restrictions
build_agent = PoolCompatShim.build_agent


__all__ = [
    "AgentBuildContext",
    "AgentBuilder",
    "AgentDefinition",
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
    "_PLUGIN_RESTRICTED_FRONTMATTER_KEYS",
    "_apply_plugin_restrictions",
    "_raw_frontmatter_keys",
    "build_agent",
    "logger",
    "parse_agent_file",
]
