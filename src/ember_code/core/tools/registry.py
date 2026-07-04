"""Tool registry — maps Claude Code tool names to Agno toolkit instances."""

import logging
from collections.abc import Callable
from pathlib import Path

from agno.tools.file import FileTools
from agno.tools.shell import ShellTools

from ember_code.core.config.tool_permissions import ToolPermissions
from ember_code.core.tools.edit import EmberEditTools
from ember_code.core.tools.notebook import NotebookTools
from ember_code.core.tools.schedule import ScheduleTools
from ember_code.core.tools.search import GlobTools, GrepTools
from ember_code.core.tools.shell import EmberShellTools
from ember_code.core.tools.visualize import BroadcastFn, VisualizeTools
from ember_code.core.tools.web import WebTools

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Factory that maps tool names to Agno toolkit instances.

    Uses the same tool names as Claude Code (Read, Write, Edit, Bash, etc.)
    and maps them to Agno toolkit classes.

    Integrates with ToolPermissions to:
    - Skip denied tools entirely
    - Pass requires_confirmation_tools for "ask" tools

    The ``CodeIndex`` toolkit lazy-builds the per-project index on first
    call so registration stays cheap.
    """

    def __init__(
        self,
        base_dir: str | None = None,
        permissions: ToolPermissions | None = None,
        cloud_token: str | None = None,
        cloud_server_url: str | None = None,
        broadcast: BroadcastFn | None = None,
    ):
        self.base_dir = Path(base_dir) if base_dir else Path.cwd()
        self.permissions = permissions or ToolPermissions(project_dir=self.base_dir)
        self._cloud_token = cloud_token
        self._cloud_server_url = cloud_server_url or "https://api.ignite-ember.sh"
        # Session broadcast — only needed by tools that push structured
        # payloads to attached clients (currently only ``Visualize``).
        # ``None`` in headless / test contexts; those tools then no-op
        # on emit instead of raising.
        self._broadcast = broadcast
        self._factories: dict[str, Callable] = {
            "Read": self._make_read,
            "Write": self._make_write,
            "Edit": self._make_edit,
            "Bash": self._make_bash,
            "BashOutput": self._make_bash,
            "Grep": self._make_grep,
            "Glob": self._make_glob,
            "LS": self._make_ls,
            "WebSearch": self._make_web_search,
            "WebFetch": self._make_web_fetch,
            "Python": self._make_python,
            "Schedule": self._make_schedule,
            "NotebookEdit": self._make_notebook,
            "CodeIndex": self._make_codeindex,
            "Visualize": self._make_visualize,
        }

    @property
    def available_tools(self) -> list[str]:
        """List all available tool names."""
        return sorted(self._factories.keys())

    def register(self, name: str, factory: Callable) -> None:
        """Register a custom tool factory."""
        self._factories[name] = factory

    def resolve(self, tool_names: list[str] | str) -> list:
        """Resolve tool names to Agno toolkit instances.

        Denied tools are skipped. Tools with "ask" permission get
        ``requires_confirmation_tools`` set so Agno triggers HITL.

        Args:
            tool_names: Comma-separated string or list of tool names.

        Returns:
            List of Agno toolkit instances.
        """
        if isinstance(tool_names, str):
            tool_names = [name.strip() for name in tool_names.split(",") if name.strip()]

        tools = []
        seen: set[str] = set()

        for name in tool_names:
            if name.startswith("MCP:") or name in ("Orchestrate", "Knowledge"):
                continue

            if self.permissions.is_denied(name):
                logger.info("Tool '%s' is denied by permissions — skipping", name)
                continue

            if name not in self._factories:
                raise ValueError(f"Unknown tool: '{name}'. Available: {self.available_tools}")

            # Deduplicate (Bash and BashOutput map to the same toolkit)
            canonical = "Bash" if name == "BashOutput" else name
            if canonical in seen:
                continue
            seen.add(canonical)

            needs_confirm = self.permissions.needs_confirmation(name)
            toolkit = self._factories[name](confirm=needs_confirm)
            tools.append(toolkit)

        return tools

    # ── Factory methods ───────────────────────────────────────────
    # Each accepts confirm=bool. When True, all functions in the
    # toolkit are marked with requires_confirmation_tools so Agno
    # pauses for HITL before executing them.

    def _make_read(self, confirm: bool = False):
        # Read-only FileTools — search handled by Grep/Glob toolkits
        kwargs: dict = dict(
            base_dir=self.base_dir,
            enable_read_file=True,
            enable_save_file=False,
            enable_list_files=True,
            enable_search_files=False,
            enable_read_file_chunk=True,
            enable_replace_file_chunk=False,
            enable_search_content=False,
        )
        if confirm:
            kwargs["requires_confirmation_tools"] = ["read_file", "list_files"]
        return FileTools(**kwargs)

    def _make_write(self, confirm: bool = False):
        # Write-only FileTools — only save_file (read ops handled by Read toolkit)
        kwargs: dict = dict(
            base_dir=self.base_dir,
            enable_read_file=False,
            enable_save_file=True,
            enable_list_files=False,
            enable_search_files=False,
            enable_read_file_chunk=False,
            enable_replace_file_chunk=False,
            enable_search_content=False,
        )
        if confirm:
            kwargs["requires_confirmation_tools"] = ["save_file"]
        return FileTools(**kwargs)

    def _make_edit(self, confirm: bool = False):
        kwargs: dict = dict(base_dir=str(self.base_dir))
        if confirm:
            kwargs["requires_confirmation_tools"] = [
                "edit_file",
                "edit_file_replace_all",
                "create_file",
            ]
        return EmberEditTools(**kwargs)

    def _make_bash(self, confirm: bool = False):
        kwargs: dict = dict(base_dir=str(self.base_dir))
        if confirm:
            kwargs["requires_confirmation_tools"] = [
                "run_shell_command",
                "stop_process",
            ]
        return EmberShellTools(**kwargs)

    def _make_bash_legacy(self, confirm: bool = False):
        kwargs: dict = {}
        if confirm:
            kwargs["requires_confirmation_tools"] = ["run_shell_command"]
        return ShellTools(**kwargs)

    def _make_ls(self, confirm: bool = False):
        return EmberShellTools(base_dir=str(self.base_dir))

    def _make_grep(self, confirm: bool = False):
        kwargs: dict = dict(base_dir=str(self.base_dir))
        if confirm:
            kwargs["requires_confirmation_tools"] = ["grep", "grep_files", "grep_count"]
        return GrepTools(**kwargs)

    def _make_glob(self, confirm: bool = False):
        kwargs: dict = dict(base_dir=str(self.base_dir))
        if confirm:
            kwargs["requires_confirmation_tools"] = ["glob_files"]
        return GlobTools(**kwargs)

    def _make_web_search(self, confirm: bool = False):
        try:
            from agno.tools.duckduckgo import DuckDuckGoTools

            kwargs: dict = {}
            if confirm:
                kwargs["requires_confirmation_tools"] = ["duckduckgo_search", "duckduckgo_news"]
            return DuckDuckGoTools(**kwargs)
        except ImportError:
            raise ImportError(
                "Web search requires duckduckgo-search. Install: pip install ember-code[web]"
            ) from None

    def _make_web_fetch(self, confirm: bool = False):
        kwargs: dict = {}
        if confirm:
            kwargs["requires_confirmation_tools"] = ["fetch_url", "fetch_json"]
        return WebTools(**kwargs)

    def _make_schedule(self, confirm: bool = False):
        return ScheduleTools(project_dir=str(self.base_dir) if self.base_dir else None)

    def _make_python(self, confirm: bool = False):
        from agno.tools.python import PythonTools

        kwargs: dict = dict(base_dir=str(self.base_dir))
        if confirm:
            kwargs["requires_confirmation_tools"] = ["run_python_code"]
        return PythonTools(**kwargs)

    def _make_notebook(self, confirm: bool = False):
        kwargs: dict = dict(base_dir=str(self.base_dir))
        if confirm:
            kwargs["requires_confirmation_tools"] = [
                "notebook_edit_cell",
                "notebook_add_cell",
                "notebook_remove_cell",
            ]
        return NotebookTools(**kwargs)

    def _make_codeindex(self, confirm: bool = False):
        from ember_code.core.tools.codeindex import CodeIndexTools

        kwargs: dict = dict(project_dir=str(self.base_dir))
        if confirm:
            kwargs["requires_confirmation_tools"] = [
                "codeindex_search",
                "codeindex_item",
                "codeindex_references",
                "codeindex_commits",
            ]
        return CodeIndexTools(**kwargs)

    def _make_visualize(self, confirm: bool = False):
        # ``confirm`` unused — Visualize only sends a one-way UI payload
        # to the FE, there's nothing to gate. The broadcast callable is
        # bound at registry construction so sub-agent builds that don't
        # have session context (headless / tests) get a no-op tool
        # instead of an import-time failure.
        return VisualizeTools(broadcast=self._broadcast)

    def load_custom_tools(
        self,
        project_dir: Path | None = None,
        *,
        plugin_tool_dirs: list[tuple[str, Path]] | None = None,
    ) -> list:
        """Discover custom tools from .ember/tools/ and return as toolkit list.

        Scans directories in priority order:
        1. ~/.ember/tools/ (global user tools)
        2. <project>/.ember/tools/ (project tools)
        3. Plugin tools (``plugin_tool_dirs``, namespaced ``custom_<plugin>_<file>``)
        """
        from ember_code.core.tools.custom_loader import load_custom_tools as _load

        return _load(
            project_dir or self.base_dir,
            plugin_tool_dirs=plugin_tool_dirs,
        )

    @property
    def cloud_connected(self) -> bool:
        """Whether Ember Cloud tools are available."""
        return self._cloud_token is not None
