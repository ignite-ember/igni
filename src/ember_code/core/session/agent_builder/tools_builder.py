"""Toolkit-assembly stage of the main-agent build.

Owns everything that ends up in the ``tools=[...]`` kwarg passed
to ``Agent(...)``. One method per toolkit family so each family is
testable in isolation and the assembly order is a single readable
sequence in :meth:`ToolsBuilder.assemble`.

Constructor takes named typed collaborators — the coordinator
harvests everything from the session's public accessors before
handing them here. The one exception is the ``session`` param,
which is passed through to :class:`LoopTools` / :class:`TodoTools` /
:class:`PlanTool` / :class:`SlashCommandTool` because those
toolkits reach back into the session's stores and broadcast paths;
their Session-shaped API is upstream of this builder.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ember_code.core.agents import AgentPool
from ember_code.core.config.settings import Settings
from ember_code.core.config.tool_permissions import ToolPermissions
from ember_code.core.hooks.executor import HookExecutor
from ember_code.core.knowledge.index import KnowledgeIndex
from ember_code.core.lsp import LspServerManager
from ember_code.core.mcp.client import MCPClientManager
from ember_code.core.monitors import MonitorManager
from ember_code.core.plugins import PluginLoader
from ember_code.core.session.knowledge_ops import SessionKnowledgeManager
from ember_code.core.skills.loader import SkillPool
from ember_code.core.sub_agent_hitl import SubAgentHITLCoordinator
from ember_code.core.tools.knowledge import KnowledgeTools
from ember_code.core.tools.loop import LoopTools
from ember_code.core.tools.loop_progress import LoopProgressTool
from ember_code.core.tools.lsp import LspTools
from ember_code.core.tools.monitors import MonitorTools
from ember_code.core.tools.orchestrate import OrchestrateTools
from ember_code.core.tools.plan import PlanTool
from ember_code.core.tools.registry import ToolRegistry
from ember_code.core.tools.slash import SlashCommandTool
from ember_code.core.tools.todo import TodoTools

if TYPE_CHECKING:
    from ember_code.core.session.core import Session


class ToolsBuilder:
    """Assemble the main agent's ``tools`` list.

    Each ``.<family>()`` method returns the toolkits it owns
    (usually 0-2 instances). :meth:`assemble` composes them in
    the same order the pre-refactor procedural version used —
    the ordering carried semantic meaning (custom tools last, so
    they can shadow built-ins if the plugin author wants; MCP
    clients after core toolkits so their tool names don't collide
    with same-named built-ins).
    """

    def __init__(
        self,
        *,
        project_dir: Path,
        settings: Settings,
        permissions_cls: type[ToolPermissions],
        registry_cls: type[ToolRegistry],
        cloud_token: str | None,
        cloud_server_url: str | None,
        broadcast: Callable[[str, dict], None],
        plugin_loader: PluginLoader,
        disabled_plugins: set[str],
        mcp_manager: MCPClientManager,
        lsp_manager: LspServerManager | None,
        monitor_manager: MonitorManager | None,
        knowledge: KnowledgeIndex | None,
        knowledge_mgr: SessionKnowledgeManager,
        skill_pool: SkillPool,
        pool: AgentPool,
        hook_executor: HookExecutor,
        session_id: str,
        sub_agent_hitl: SubAgentHITLCoordinator,
        reasoning_factory: Callable[[Settings], Any],
        resolve_tool_names: Callable[[ToolRegistry], list[str]],
        session: Session,
    ) -> None:
        self._project_dir = project_dir
        self._settings = settings
        self._permissions_cls = permissions_cls
        self._registry_cls = registry_cls
        self._cloud_token = cloud_token
        self._cloud_server_url = cloud_server_url
        self._broadcast = broadcast
        self._plugin_loader = plugin_loader
        self._disabled_plugins = disabled_plugins
        self._mcp_manager = mcp_manager
        self._lsp_manager = lsp_manager
        self._monitor_manager = monitor_manager
        self._knowledge = knowledge
        self._knowledge_mgr = knowledge_mgr
        self._skill_pool = skill_pool
        self._pool = pool
        self._hook_executor = hook_executor
        self._session_id = session_id
        self._sub_agent_hitl = sub_agent_hitl
        self._reasoning_factory = reasoning_factory
        self._resolve_tool_names = resolve_tool_names
        # LoopTools / TodoTools / PlanTool / SlashCommandTool need a
        # Session-shaped object because they call back into the
        # session's stores / broadcast paths. Threaded here so the
        # rest of the builder stays session-free.
        self._session = session
        self._registry: ToolRegistry | None = None

    def _build_registry(self) -> ToolRegistry:
        """Construct the tool registry once and cache it.

        The registry is shared between :meth:`core_tools` (name-
        based resolution) and :meth:`custom` (custom-toolkit
        loading), so we build it lazily on first access.
        """
        if self._registry is None:
            self._registry = self._registry_cls(
                base_dir=str(self._project_dir),
                permissions=self._permissions_cls(
                    project_dir=self._project_dir,
                    settings_permissions=self._settings.permissions,
                ),
                cloud_token=self._cloud_token,
                cloud_server_url=self._cloud_server_url,
                broadcast=self._broadcast,
            )
        return self._registry

    def core_tools(self) -> list[Any]:
        """Registry-resolved built-ins (Write / Edit / Bash / etc.)."""
        registry = self._build_registry()
        tool_names = self._resolve_tool_names(registry)
        return registry.resolve(tool_names)

    def orchestrate(self) -> list[Any]:
        """Delegation surface — spawn_agent / spawn_team."""
        return [
            OrchestrateTools(
                pool=self._pool,
                settings=self._settings,
                current_depth=0,
                hook_executor=self._hook_executor,
                session_id=self._session_id,
                hitl_coordinator=self._sub_agent_hitl,
                project_dir=self._project_dir,
            )
        ]

    def reasoning(self) -> list[Any]:
        """Optional Agno ReasoningTools — enabled by settings."""
        rt = self._reasoning_factory(self._settings)
        return [rt] if rt else []

    def knowledge(self) -> list[Any]:
        """Chroma-backed knowledge query tools when configured."""
        if self._knowledge is None:
            return []
        return [KnowledgeTools(self._knowledge_mgr)]

    def loop(self) -> list[Any]:
        """/loop-driven iteration control + per-iter scratchpad."""
        return [
            LoopTools(self._session),
            LoopProgressTool(self._session),
        ]

    def todo(self) -> list[Any]:
        """Agent-facing TodoWrite (CC parity)."""
        return [TodoTools(self._session)]

    def plan(self) -> list[Any]:
        """Agent-facing exit_plan_mode (row 50)."""
        return [PlanTool(self._session)]

    def slash(self) -> list[Any]:
        """Agent-facing re-entrant slash-command dispatch."""
        return [SlashCommandTool(self._session)]

    def lsp(self) -> list[Any]:
        """LSP query toolkit — only when at least one server exists."""
        if self._lsp_manager is None or not self._lsp_manager.list_servers():
            return []
        return [LspTools(self._lsp_manager)]

    def monitors(self) -> list[Any]:
        """Monitor inspection toolkit — only when configured."""
        if self._monitor_manager is None or not self._monitor_manager.list_names():
            return []
        return [MonitorTools(self._monitor_manager)]

    def mcp(self, tools_so_far: list[Any]) -> list[Any]:
        """Connected MCP-server clients, deduped against ``tools_so_far``.

        The original code appended each client only if not already
        in the outbound list — preserved here so a client that
        also implements ``AgnoToolkit`` semantics and got pulled
        in earlier isn't attached twice.
        """
        extras: list[Any] = []
        for name in self._mcp_manager.list_connected():
            client = self._mcp_manager.get_client(name)
            if client and client not in tools_so_far and client not in extras:
                extras.append(client)
        return extras

    def custom(self) -> list[Any]:
        """Custom toolkits loaded from ``.ember/tools/`` (+ plugin dirs)."""
        registry = self._build_registry()
        plugin_tool_dirs = self._plugin_loader.collect_tool_dirs(
            disabled=self._disabled_plugins,
        )
        loaded = registry.load_custom_tools(
            self._project_dir,
            plugin_tool_dirs=plugin_tool_dirs,
        )
        return list(loaded) if loaded else []

    def assemble(self) -> list[Any]:
        """Compose all toolkit families in the canonical order.

        Order preserved from the pre-refactor procedural version:
        core → orchestrate → reasoning → knowledge → loop → todo
        → plan → slash → lsp → monitors → mcp (dedup) → custom.
        """
        tools: list[Any] = []
        tools.extend(self.core_tools())
        tools.extend(self.orchestrate())
        tools.extend(self.reasoning())
        tools.extend(self.knowledge())
        tools.extend(self.loop())
        tools.extend(self.todo())
        tools.extend(self.plan())
        tools.extend(self.slash())
        tools.extend(self.lsp())
        tools.extend(self.monitors())
        tools.extend(self.mcp(tools))
        tools.extend(self.custom())
        return tools
