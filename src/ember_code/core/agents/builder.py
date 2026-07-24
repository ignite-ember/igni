"""Build Agno :class:`Agent` objects from :class:`AgentDefinition` s.

Owns the three sub-problems that used to be procedural free
functions (``_resolve_model``, ``_resolve_tools``,
``_build_instructions``, ``build_agent``): pick the model, assemble
the tool list, compose the instructions. Consolidated onto one
class that captures the six shared-state params once via
:class:`AgentBuildContext`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from ember_code.core.agents.schemas import (
    AgentBuildContext,
    AgentConstructorArgs,
    AgentDefinition,
)
from ember_code.core.config.models import ModelRegistry
from ember_code.core.config.tool_permissions import ToolPermissions
from ember_code.core.tools.knowledge import KnowledgeTools
from ember_code.core.tools.registry import ToolRegistry
from ember_code.core.tools.schedule import ScheduleTools

if TYPE_CHECKING:
    from agno.agent import Agent


class AgentBuilder:
    """Turn one :class:`AgentDefinition` into a runnable Agno
    :class:`Agent`.

    Constructor takes an :class:`AgentBuildContext` bundle so the
    six shared-state params (``settings``, ``base_dir``,
    ``mcp_clients``, ``knowledge_mgr``, ``db``, ``broadcast``)
    live on ``self`` — the public :meth:`build` method then takes
    only the definition (what varies per agent).
    """

    #: Sentinel value the pool historically wrote into
    #: ``AgentDefinition.model``; treat it the same as "no override"
    #: so a checked-in ``.md`` doesn't lock the user to a specific
    #: model.
    BUILTIN_DEFAULT_MODEL: ClassVar[str] = "MiniMax-M2.7"

    # ── Test seams (class-level) ────────────────────────────────
    #
    # Three ClassVar slots that let tests substitute the three
    # collaborator classes without patching module-level names on
    # an external shim. Prefer ``patch.object(AgentBuilder, '_agent_cls', mock)``
    # over ``patch("ember_code.core.pool.Agent", ...)``: the class
    # attribute is the canonical seam; the module-level shim was
    # only historically necessary because the free ``build_agent``
    # looked up ``Agent`` via ``globals()``.
    #
    # ``_agent_cls`` stays ``None`` in prod so
    # :meth:`_resolve_agent_cls` performs the lazy Agno import at
    # first ``build()`` call (~350ms saved on module import).
    # ``tests/test_pool.py::TestBuildAgentMCPFiltering`` patches
    # this slot to intercept construction.

    #: Override for Agno's ``Agent`` class. ``None`` in prod →
    #: :meth:`_resolve_agent_cls` imports and caches the real class
    #: lazily. Tests patch this via
    #: ``@patch.object(AgentBuilder, "_agent_cls", MagicMock())``.
    _agent_cls: ClassVar[type | None] = None

    #: Override for :class:`ModelRegistry`. Tests patch via
    #: ``@patch.object(AgentBuilder, "_model_registry_cls", ...)``.
    _model_registry_cls: ClassVar[type[ModelRegistry]] = ModelRegistry

    #: Override for :class:`ToolRegistry`. Tests patch via
    #: ``@patch.object(AgentBuilder, "_tool_registry_cls", ...)``.
    _tool_registry_cls: ClassVar[type[ToolRegistry]] = ToolRegistry

    @classmethod
    def _resolve_agent_cls(cls) -> type:
        """Return the ``Agent`` class the builder should construct.

        Honours the ``_agent_cls`` ClassVar test-seam if set;
        otherwise imports and caches ``agno.agent.Agent`` (heavy —
        ~350ms). The lazy import is preserved by deferring the
        import into this classmethod rather than doing it at
        module load time.
        """
        if cls._agent_cls is not None:
            return cls._agent_cls
        from agno.agent import Agent

        return Agent

    def __init__(self, context: AgentBuildContext) -> None:
        self._context = context

    @property
    def context(self) -> AgentBuildContext:
        """Current build context. Mutations go through
        :meth:`replace_context` so downstream consumers of
        derived state (a cached model registry, for instance)
        get to observe the swap."""
        return self._context

    def replace_context(self, new_context: AgentBuildContext) -> None:
        """Swap the shared context. Used by :class:`AgentPool` after
        MCP connects or when the knowledge manager attaches."""
        self._context = new_context

    def build(self, definition: AgentDefinition) -> Agent:
        """Construct one :class:`Agent` from ``definition``.

        Delegates to :meth:`_resolve_model`, :meth:`_resolve_tools`
        and :meth:`_build_instructions`. Everything else is the
        typed :class:`AgentConstructorArgs` bridge.
        """
        # Lazy Agno import + test-seam lookup rolled into one class
        # method — see ``_resolve_agent_cls`` docstring for the
        # ~350ms rationale.
        agent_cls = type(self)._resolve_agent_cls()

        model = self._resolve_model(definition)
        tools, agent_mcp = self._resolve_tools(definition)
        instructions = self._build_instructions(definition, agent_mcp)

        args = AgentConstructorArgs(
            name=definition.name,
            model=model,
            description=definition.description,
            instructions=instructions if instructions else None,
            tools=tools if tools else None,
            markdown=True,
            num_history_runs=self._context.settings.storage.max_history_runs,
            retries=self._context.settings.models.retries,
            db=self._context.db,
            reasoning=definition.reasoning,
            reasoning_min_steps=(definition.reasoning_min_steps if definition.reasoning else None),
            reasoning_max_steps=(definition.reasoning_max_steps if definition.reasoning else None),
        )
        return agent_cls(**args.to_agno_kwargs())

    def _resolve_model(self, definition: AgentDefinition) -> Any:
        """Pick the model for ``definition`` and apply the two
        per-agent overrides (``temperature`` / ``max_tokens``)."""
        settings = self._context.settings
        agent_model = definition.model
        if not agent_model or agent_model == self.BUILTIN_DEFAULT_MODEL:
            agent_model = settings.models.default
        model = type(self)._model_registry_cls(settings).get_model(agent_model)
        if definition.temperature is not None:
            model.temperature = definition.temperature
        if definition.max_tokens is not None:
            model.max_tokens = definition.max_tokens
        return model

    def _resolve_tools(self, definition: AgentDefinition) -> tuple[list[Any], dict[str, Any]]:
        """Assemble the full tool list + return the MCP subset.

        Returns ``(tools, agent_mcp)`` — ``agent_mcp`` is the
        subset of context ``mcp_clients`` this agent will actually
        see. Caller uses it to compose the MCP-hint instruction."""
        ctx = self._context
        tools: list[Any] = []
        if definition.tools:
            permissions = ToolPermissions(project_dir=Path(ctx.base_dir) if ctx.base_dir else None)
            registry = type(self)._tool_registry_cls(
                base_dir=ctx.base_dir,
                permissions=permissions,
                broadcast=ctx.broadcast,
            )
            tools = registry.resolve(definition.tools)

        if tools:
            tools.append(ScheduleTools(project_dir=ctx.base_dir))
        if tools and ctx.knowledge_mgr is not None:
            tools.append(KnowledgeTools(ctx.knowledge_mgr))

        agent_mcp: dict[str, Any] = {}
        if tools and ctx.mcp_clients:
            # Empty ``mcp_servers`` means "include everything"
            # (backward-compat); a populated list is a whitelist.
            if definition.mcp_servers:
                agent_mcp = {
                    name: client
                    for name, client in ctx.mcp_clients.items()
                    if name in definition.mcp_servers
                }
            else:
                agent_mcp = ctx.mcp_clients
            for client in agent_mcp.values():
                if client not in tools:
                    tools.append(client)

        return tools, agent_mcp

    def _build_instructions(
        self,
        definition: AgentDefinition,
        agent_mcp: dict[str, Any],
    ) -> list[str]:
        """Compose the instructions list: system prompt (if any),
        working-directory hint, and MCP no-retry hint."""
        ctx = self._context
        instructions: list[str] = []
        if definition.system_prompt:
            instructions.append(definition.system_prompt)
        if ctx.base_dir:
            instructions.append(f"Working directory: {ctx.base_dir}")
        if agent_mcp:
            mcp_names = ", ".join(agent_mcp.keys())
            instructions.append(
                f"You have MCP tools from: {mcp_names}. "
                f"Project path: {ctx.base_dir}\n"
                f"If an MCP tool returns empty/no data, do NOT retry with different arguments. "
                f"Report what happened and ask the user."
            )
        return instructions


__all__ = ["AgentBuilder"]
