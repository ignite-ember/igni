"""Main-agent build coordinator.

Six phase methods (``build_tools`` / ``build_prompt`` /
``build_instructions`` / ``build_model`` / ``build_spec`` /
``build``) each own one slice of the main-agent construction. All
Session state is read through the session's public accessors
(``cloud_access_token``, ``codeindex_available``,
``active_output_style``, ``tool_event_hook()``,
``resolve_main_tool_names()``, ``build_agent_catalog()``) so this
sub-package never reaches into ``session._underscore`` attributes.

Constructor injection is required: the caller passes ``agent_cls`` /
``registry_cls`` / ``permissions_cls`` / ``compression_cls`` /
``model_registry_cls`` / ``reasoning_factory`` /
``guardrails_factory`` / ``prompt_loader`` explicitly.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .agent_build_spec import AgentBuildSpec
from .instructions_builder import InstructionsBuilder
from .prompt_builder import PromptBuilder
from .tools_builder import ToolsBuilder

if TYPE_CHECKING:
    from ember_code.core.session.core import Session


class MainAgentBuilder:
    """Coordinator that assembles the main :class:`Agent`.

    Life-cycle: one build per instance — instantiate, call
    :meth:`build`, discard. State lives on the instance so phase
    methods can pass artefacts (``prompt``, ``tools``, ``model``,
    ``spec``) between themselves without argument-thread noise.
    """

    def __init__(
        self,
        session: Session,
        *,
        agent_cls: Any,
        registry_cls: Any,
        permissions_cls: Any,
        compression_cls: Any,
        model_registry_cls: Any,
        reasoning_factory: Callable[[Any], Any],
        guardrails_factory: Callable[[Any], Any],
        prompt_loader: Callable[[str], str],
    ) -> None:
        self._session = session
        self._agent_cls = agent_cls
        self._registry_cls = registry_cls
        self._permissions_cls = permissions_cls
        self._compression_cls = compression_cls
        self._model_registry_cls = model_registry_cls
        self._reasoning_factory = reasoning_factory
        self._guardrails_factory = guardrails_factory
        self._prompt_loader = prompt_loader

    def build_tools(self) -> list[Any]:
        """Assemble the ``tools=[...]`` list via :class:`ToolsBuilder`."""
        s = self._session
        return ToolsBuilder(
            project_dir=s.project_dir,
            settings=s.settings,
            permissions_cls=self._permissions_cls,
            registry_cls=self._registry_cls,
            cloud_token=s.cloud_access_token,
            cloud_server_url=s.cloud_server_url,
            broadcast=s.broadcast,
            plugin_loader=s.plugin_loader,
            disabled_plugins=s.disabled_plugins,
            mcp_manager=s.mcp_manager,
            lsp_manager=s.lsp_manager,
            monitor_manager=s.monitor_manager,
            knowledge=s.knowledge,
            knowledge_mgr=s.knowledge_mgr,
            skill_pool=s.skill_pool,
            pool=s.pool,
            hook_executor=s.hook_executor,
            session_id=s.session_id,
            sub_agent_hitl=s.sub_agent_hitl,
            reasoning_factory=self._reasoning_factory,
            resolve_tool_names=s.resolve_main_tool_names,
            session=s,
        ).assemble()

    def build_prompt(self) -> str:
        """Render the base system prompt via :class:`PromptBuilder`."""
        s = self._session
        return PromptBuilder(
            project_dir=s.project_dir,
            settings=s.settings,
            codeindex_available=s.codeindex_available,
            agent_catalog=s.build_agent_catalog(),
            skill_descriptions=s.skill_pool.describe(),
            prompt_loader=self._prompt_loader,
        ).render()

    def build_instructions(self, prompt: str) -> list[str]:
        """Compose the full ``instructions`` list via
        :class:`InstructionsBuilder`."""
        s = self._session
        return InstructionsBuilder(
            project_dir=s.project_dir,
            project_instructions=s.project_instructions,
            workspace=s.workspace,
            settings=s.settings,
            output_styles=s.output_styles,
            active_output_style_name=s.active_output_style,
            codeindex_available=s.codeindex_available,
        ).assemble(prompt)

    def build_model(self) -> tuple[Any, int]:
        """Load the model + resolve the effective context window.

        Context is capped by ``settings.models.max_context_window``
        so compression remains aggressive on high-capacity models.
        """
        model_registry = self._model_registry_cls(self._session.settings)
        model = model_registry.get_model()
        context_window = min(
            model_registry.get_context_window(),
            self._session.settings.models.max_context_window,
        )
        return model, context_window

    def build_spec(
        self,
        *,
        model: Any,
        tools: list[Any],
        instructions: list[str],
        context_window: int,
        tool_event_hook: Any,
    ) -> AgentBuildSpec:
        """Pack every Agno kwarg into a typed :class:`AgentBuildSpec`."""
        s = self._session
        compression = self._compression_cls(
            model=model,
            compress_tool_results=True,
            compress_token_limit=int(context_window * 0.8),
        )
        return AgentBuildSpec(
            name="ember",
            model=model,
            tools=tools,
            instructions=instructions,
            markdown=True,
            # Retry transient model-API failures (timeouts, 5xx)
            # before bubbling up. Same default as the specialist
            # pool — see ``pool.build_agent``.
            retries=s.settings.models.retries,
            db=s.db,
            session_id=s.session_id,
            user_id=s.user_id,
            # History — keep all turns until 80% compaction triggers.
            add_history_to_context=True,
            num_history_runs=10000,
            # Agentic memory removed; LearningMachine handles learning.
            # Existing memories still loaded into context.
            enable_agentic_memory=False,
            add_memories_to_context=s.settings.memory.add_memories_to_context,
            compress_tool_results=True,
            compression_manager=compression,
            # Session summaries — disabled at init to avoid per-turn
            # LLM calls. ``_compact()`` creates the manager on demand.
            # Existing summaries from prior compaction still injected.
            enable_session_summaries=False,
            add_session_summary_to_context=True,
            stream=True,
            stream_events=True,
            # Agents reach the index via ``KnowledgeTools``, not
            # Agno's built-in ``search_knowledge`` — pass nothing.
            knowledge=None,
            search_knowledge=False,
            pre_hooks=self._guardrails_factory(s.settings),
            learning=s.learning,
            add_learnings_to_context=True,
            tool_hooks=[tool_event_hook],
        )

    def build(self) -> Any:
        """Run every phase in order and hand back the final agent."""
        prompt = self.build_prompt()
        instructions = self.build_instructions(prompt)
        tools = self.build_tools()
        model, context_window = self.build_model()
        # Constructed once per build; ``ToolEventHook`` also refreshes
        # the session's cached ``permission_evaluator`` as a side
        # effect, so the shared reference between tools and Agno
        # hooks must be the same instance.
        tool_event_hook = self._session.tool_event_hook()
        spec = self.build_spec(
            model=model,
            tools=tools,
            instructions=instructions,
            context_window=context_window,
            tool_event_hook=tool_event_hook,
        )
        return spec.instantiate(self._agent_cls)
