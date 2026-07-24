"""Explicit facade for the deprecated ``core/pool.py`` surface.

The old ``core/pool.py`` was a 218-line backward-compat shim
carrying four legacy free functions (:func:`parse_agent_file`,
:func:`_raw_frontmatter_keys`, :func:`_apply_plugin_restrictions`,
:func:`build_agent`) plus module-level rebinds of ``Agent`` /
``ModelRegistry`` / ``ToolRegistry`` used only as test-patch
seams. That shape violated the audit's Rule 1 (free functions
taking a state object as first arg) and Rule 6 (utility module
of related helpers).

This module concentrates the deprecation surface onto
:class:`PoolCompatShim` — one class, four classmethods, one
:class:`ClassVar` blocklist so a future removal is a single
class-deletion rather than a search-and-destroy across the
loose names in ``core/pool.py``.

Every classmethod delegates one-line to the canonical owner:

* :meth:`PoolCompatShim.parse_agent_file` →
  :meth:`AgentMarkdownFile.parse`
* :meth:`PoolCompatShim.raw_frontmatter_keys` →
  :meth:`AgentMarkdownFile.raw_frontmatter_keys`
* :meth:`PoolCompatShim.apply_plugin_restrictions` →
  :meth:`PluginRestrictionPolicy.apply`
* :meth:`PoolCompatShim.build_agent` →
  :meth:`AgentBuilder.build` (via a wrapped
  :class:`AgentBuildContext`)

``core/pool.py`` separately binds ``pool.logger`` to
:data:`ember_code.core.agents.plugin_policy.logger` (the same
singleton :class:`PluginRestrictionPolicy` emits through), so
``tests/test_plugin_agent_restrictions.py``'s
``monkeypatch.setattr(...logger, "warning", ...)`` lands on the
right object regardless of whether the caller reaches through
the shim or the canonical package.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from ember_code.core.agents.builder import AgentBuilder
from ember_code.core.agents.markdown import AgentMarkdownFile
from ember_code.core.agents.plugin_policy import PluginRestrictionPolicy
from ember_code.core.agents.schemas import (
    AgentBuildContext,
    AgentDefinition,
    Broadcast,
    DbHandle,
    KnowledgeManager,
    McpClient,
)

if TYPE_CHECKING:
    from agno.agent import Agent

    from ember_code.core.config.settings import Settings


class PoolCompatShim:
    """Facade holding the deprecated ``core/pool.py`` surface.

    Every method here is a classmethod that delegates one-line
    to the canonical owner. Kept as a single class so:

    * Deletion is a one-file, one-class removal once the four
      legacy tests migrate off it.
    * A grep on the class name shows every legacy call-site in
      one pass.
    * :attr:`__deprecated_names__` is the exhaustive list of
      names ``core/pool.py`` re-binds — no name can slip through
      a rename unnoticed.
    """

    #: Exhaustive list of the deprecated name surface this class
    #: replaces. ``core/pool.py`` binds each name to the same
    #: classmethod so ``from ember_code.core.pool import <name>``
    #: still resolves for legacy callers.
    __deprecated_names__: ClassVar[tuple[str, ...]] = (
        "parse_agent_file",
        "_raw_frontmatter_keys",
        "_apply_plugin_restrictions",
        "build_agent",
    )

    @classmethod
    def parse_agent_file(cls, path: Path) -> AgentDefinition:
        """Deprecated — use :meth:`AgentMarkdownFile.parse`.

        Still relied on by ``tests/test_pool.py`` (until migrated)
        and any legacy plugin that ``from ember_code.core.pool
        import parse_agent_file``.
        """
        return AgentMarkdownFile(path).parse()

    @classmethod
    def raw_frontmatter_keys(cls, path: Path) -> set[str]:
        """Deprecated — use
        :meth:`AgentMarkdownFile.raw_frontmatter_keys`.

        Still relied on by
        ``tests/test_plugin_agent_restrictions.py`` (until
        migrated).
        """
        return AgentMarkdownFile(path).raw_frontmatter_keys()

    @classmethod
    def apply_plugin_restrictions(
        cls,
        definition: AgentDefinition,
        raw_keys: set[str],
        plugin_name: str = "",
    ) -> AgentDefinition:
        """Deprecated — use
        :meth:`PluginRestrictionPolicy.apply` or
        :meth:`AgentDefinition.with_plugin_restrictions`.

        Still relied on by
        ``tests/test_plugin_agent_restrictions.py`` (until
        migrated).
        """
        return PluginRestrictionPolicy().apply(definition, raw_keys, plugin_name)

    @classmethod
    def build_agent(
        cls,
        definition: AgentDefinition,
        settings: Settings,
        base_dir: str | None = None,
        mcp_clients: dict[str, McpClient] | None = None,
        knowledge_mgr: KnowledgeManager | None = None,
        db: DbHandle | None = None,
        broadcast: Broadcast | None = None,
    ) -> Agent:
        """Deprecated — instantiate :class:`AgentBuilder` directly.

        Wraps the six loose params into an
        :class:`AgentBuildContext` and delegates to
        :meth:`AgentBuilder.build`. The typed :class:`Broadcast`,
        :class:`KnowledgeManager`, :class:`DbHandle`, and
        :class:`McpClient` protocols close the audit AP5 ``Any``
        holes on the public surface.

        Still relied on by
        ``tests/test_pool.py::TestBuildAgentMCPFiltering`` (until
        migrated). Those tests patch
        :attr:`AgentBuilder._agent_cls` /
        :attr:`AgentBuilder._model_registry_cls` /
        :attr:`AgentBuilder._tool_registry_cls` — the ClassVar
        seams — so the shim doesn't need any private-attr
        reach-in of its own.
        """
        context = AgentBuildContext(
            settings=settings,
            base_dir=base_dir,
            mcp_clients=mcp_clients,
            knowledge_mgr=knowledge_mgr,
            db=db,
            broadcast=broadcast,
        )
        return AgentBuilder(context).build(definition)


__all__ = ["PoolCompatShim"]
