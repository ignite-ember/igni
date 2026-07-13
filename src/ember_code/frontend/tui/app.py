"""igni TUI — main application.

Thin shell that composes Textual widgets and delegates logic to
``ConversationView``, ``StatusTracker``, ``RunController``,
``HITLHandler``, and ``SessionManager``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import signal
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any

from rich.markup import escape
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.css.query import NoMatches
from textual.timer import Timer
from textual.widgets import Static

# ``pwd`` is POSIX-only; on Windows the import fails and the module
# stays ``None``. Callers guard with ``if pwd is not None`` before
# touching it — same pattern Python's own stdlib uses (see
# ``os.path.expanduser`` in cpython/Modules/posixmodule.c).
try:
    import pwd
except ImportError:  # pragma: no cover — Windows only
    pwd = None  # type: ignore[assignment]

from ember_code import __version__
from ember_code.core.utils.file_index import FileIndex
from ember_code.frontend.tui import (
    agent_handlers,
    codeindex_handlers,
    input_handlers,
    keybinding_handlers,
    knowledge_handlers,
    lifecycle_handlers,
    loop_handlers,
    mcp_handlers,
    mode_handlers,
    picker_handlers,
    plugin_handlers,
    scheduler_handlers,
)
from ember_code.frontend.tui.conversation_view import ConversationView
from ember_code.frontend.tui.hitl_handler import HITLHandler
from ember_code.frontend.tui.input_handler import InputHandler, extract_at_mention, shortcut_label
from ember_code.frontend.tui.run_controller import RunController
from ember_code.frontend.tui.session_manager import SessionManager
from ember_code.frontend.tui.status_tracker import StatusTracker
from ember_code.frontend.tui.widgets import (
    AgentInfo,
    AgentsPanelWidget,
    CodeIndexPanelWidget,
    CodeIndexStatusInfo,
    FilePickerDropdown,
    HelpPanelWidget,
    HookInfo,
    HooksPanelWidget,
    KnowledgePanelWidget,
    KnowledgeSearchHit,
    KnowledgeStatusInfo,
    LoginWidget,
    LoopPanelWidget,
    LoopStatusInfo,
    MarketplaceInfo,
    MarketplacePluginInfo,
    MCPPanelWidget,
    MCPServerInfo,
    MessageWidget,
    ModelPickerWidget,
    PluginInfo,
    PluginsPanelWidget,
    PromptInput,
    QueuePanel,
    SessionPickerWidget,
    SkillInfo,
    SkillsPanelWidget,
    StatusBar,
    TaskPanel,
    TipBar,
    UpdateBar,
)
from ember_code.protocol import messages as pmsg
from ember_code.protocol.messages import CommandAction, CommandResult, CommandResultKind
from ember_code.protocol.rpc import RpcMethod

logger = logging.getLogger(__name__)


class EmberApp(App):
    """igni Terminal UI Application."""

    TITLE = "igni"
    SUB_TITLE = f"v{__version__}"
    ALLOW_SELECT = True

    CSS = """
    * {
        scrollbar-size: 1 1;
        scrollbar-background: $background;
        scrollbar-color: $text-muted;
    }

    Screen {
        overflow-y: hidden;
        layers: default dialog;
    }

    Markdown .code_inline {
        background: ansi_bright_black;
        color: ansi_green;
    }

    MarkdownFence {
        background: #2b2b2b;
        color: #a9b7c6;
        margin: 1 0;
        padding: 0;
        border: round #323232;
    }

    #header-bar {
        dock: top;
        height: 2;
        width: 100%;
        padding: 1 2 0 2;
        color: $text-muted;
    }

    #conversation {
        height: 1fr;
        overflow-y: auto;
        padding: 1 2;
        scrollbar-size: 1 1;
    }

    #welcome-box {
        height: auto;
        width: 1fr;
        text-align: center;
        margin: 0 4;
        border: round ansi_yellow;
        padding: 0 1;
    }

    #capabilities {
        height: auto;
        width: 1fr;
        margin: 0 2;
        color: $text-muted;
    }

    #footer {
        dock: bottom;
        /* Auto-height with ``dock: bottom`` keeps the BOTTOM edge
           anchored to the screen bottom; when prompt-row grows
           (e.g. soft-wrapped input on a narrow terminal) the top
           rises upward into the conversation area instead of the
           children overflowing the container downward past the
           screen. A previous fixed ``height: 6`` made the children
           overflow when the input stretched to 2 rows — that
           overflow rendered below the screen edge, hiding status-
           bar + tip-bar. ``min-height`` is the floor: even if
           everything inside reports 0 rows we still claim 5 rows
           so the dock area never collapses. */
        height: auto;
        min-height: 5;
        width: 100%;
    }

    #prompt-row {
        /* Auto-height so it can absorb the input's natural row
           count. Combined with ``#footer { height: auto }`` above,
           the whole bottom chrome grows upward into the
           conversation area when needed — no overflow downward. */
        height: auto;
        width: 100%;
        padding: 0 2;
        border-top: solid ansi_bright_black;
    }

    #prompt-indicator {
        width: 2;
        height: 1;
        color: $accent;
    }

    #user-input {
        width: 1fr;
        /* Auto-grow up to 10 visible rows; the footer is
           ``dock: bottom; height: auto`` so growth pushes the chrome
           upward into the conversation area rather than overflowing
           the screen. Beyond 10 rows TextArea's internal ScrollView
           keeps the cursor in view. ``!important`` to win over
           ``PromptInput.DEFAULT_CSS`` if it ever disagrees. */
        height: auto !important;
        min-height: 1 !important;
        max-height: 10 !important;
        border: none !important;
        background: $background;
        color: $text;
        padding: 0 !important;
        scrollbar-size: 1 0;
    }

    #user-input:focus {
        border: none !important;
    }

    #status-bar {
        /* 3 rows = 1 row for ``border-top`` + 2 content rows. */
        height: 3;
        width: 100%;
        border-top: solid ansi_bright_black;
        content-align: center middle;
        text-align: center;
        color: $text-muted;
    }

    #tip-bar {
        /* Lives inside ``#footer`` now (no dock). ``TipBar``'s class
           default also drops its old ``dock: bottom`` — otherwise it
           would dock to the Screen ancestor and overlap the footer. */
        height: 1;
        width: 100%;
    }

    #update-bar {
        dock: top;
        height: 1;
        width: 100%;
    }

    .agent-dispatch {
        height: 1;
        margin: 0 0 0 2;
    }

    .task-event {
        height: 1;
        margin: 0 0 0 2;
    }

    .run-error {
        height: auto;
        margin: 0 0 0 2;
    }

    #queue-panel {
        dock: bottom;
        height: auto;
        max-height: 10;
    }

    #task-panel {
        dock: bottom;
        height: auto;
        max-height: 12;
    }
    """

    _IS_MACOS = sys.platform == "darwin"

    BINDINGS = [
        Binding("ctrl+d", "quit", "Quit", show=False, priority=True),
        Binding("ctrl+l", "clear_screen", "Clear", show=False, priority=True),
        Binding("ctrl+o", "toggle_expand_all", "Expand", show=False, priority=True),
        Binding("ctrl+v", "toggle_verbose", "Verbose", show=False, priority=True),
        Binding("ctrl+q", "toggle_queue", "Queue", show=False, priority=True),
        Binding("ctrl+t", "toggle_tasks", "Tasks", show=False, priority=True),
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
    ]

    def __init__(
        self,
        settings: object | None = None,
        resume_session_id: str | None = None,
        initial_message: str | None = None,
        project_dir: Path | None = None,
        additional_dirs: list[Path] | None = None,
        debug: bool = False,
    ):
        super().__init__()
        self.settings = settings  # passed from cli.py (Settings object)
        self.resume_session_id = resume_session_id
        self.initial_message = initial_message
        self._project_dir = project_dir
        self._additional_dirs = additional_dirs
        self._debug = debug

        # Backend created in on_mount via BackendProcess (separate subprocess)
        self._backend: Any = None
        self._process_mgr: Any = None
        self._task_refresh_timer: Any = None
        # Panel status-poll intervals — set when the matching panel is
        # opened, cleared back to None when it closes. Declared Optional
        # so the close path's ``= None`` reset type-checks.
        self._codeindex_status_poll: Timer | None = None
        self._loop_status_poll: Timer | None = None

        self._conversation: ConversationView | None = None
        self._shell_context: list[str] = []  # accumulated shell results for AI context
        self._shell_mode: bool = False  # True when prompt is in $ shell mode
        self._command_mode: bool = False  # True when prompt is in / command mode
        self._shell_proc: Any = None  # active inline shell subprocess
        self._shell_task: Any = None  # asyncio task for _run_shell_inline
        self._input_handler: InputHandler | None = None
        # Visibility flags for the per-keystroke autocomplete/file-picker
        # paths. ``_on_input_changed`` used to ``query_one`` for the
        # widgets every keystroke just to find out whether they were
        # mounted; on a long conversation that tree walk was O(N) and
        # made typing visibly laggy ("each keystroke takes seconds").
        # The flags let the hot path skip the lookup when nothing is
        # mounted.
        self._autocomplete_mounted: bool = False
        self._file_picker_mounted: bool = False
        # Cached PromptInput reference — ``on_key`` resolved it via
        # ``query_one`` on every keypress, which is O(N) over the
        # widget tree. The input widget is mounted once at startup
        # and never moves, so caching is safe.
        self._user_input_widget: PromptInput | None = None

        # Managers initialised in on_mount once widgets exist
        self._status: StatusTracker | None = None
        self._controller: RunController | None = None
        self._hitl: HITLHandler | None = None
        self._sessions: SessionManager | None = None
        self._scheduler_runner = None

    # ── Public accessors ────────────────────────────────────────────

    @property
    def backend(self):
        """Public accessor for the backend server."""
        return self._backend

    # ── Compose / Mount ───────────────────────────────────────────

    @staticmethod
    def _get_full_name() -> str:
        """Get the user's full name from the system."""
        try:
            if sys.platform == "darwin":
                result = subprocess.run(
                    ["id", "-F"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
            if pwd is not None:
                return pwd.getpwuid(os.getuid()).pw_gecos.split(",")[0] or os.getlogin()
            return os.getlogin()
        except Exception:
            try:
                return os.getlogin()
            except Exception:
                return ""

    def _build_welcome_content(self) -> str:
        """Build the welcome banner content (border is CSS)."""
        name = self._get_full_name()
        model = self.settings.models.default
        cwd = os.getcwd().replace(os.path.expanduser("~"), "~")

        greeting = f"[bold]Welcome back {name}![/bold]" if name else "[bold]Welcome![/bold]"

        logo_lines = [
            "[bold ansi_bright_red]▐▛███▜▌[/bold ansi_bright_red]",
            "[bold ansi_bright_red]▝▜█████▛▘[/bold ansi_bright_red]",
            "[bold ansi_bright_red] ▘▘ ▝▝ [/bold ansi_bright_red]",
        ]

        info = f"[bold]{model}[/bold]  [dim]·[/dim]  [dim]{cwd}[/dim]"

        lines = ["", greeting, ""] + logo_lines + ["", info, ""]
        return "\n".join(lines)

    @staticmethod
    def _build_capabilities_text() -> str:
        """Capabilities pitch shown below the welcome box.

        Curated to surface the *differentiating* features users won't
        find in a vanilla coding assistant. Each bullet is one short
        line so it doesn't wrap on an 80-col terminal — the previous
        "feature — long description (/cmd)" form orphaned the slash
        command on the next line whenever the description had a comma.
        """
        lines = [
            "",
            "  [bold]Why igni:[/bold]",
            "",
            "    [dim]●[/dim]  [bold]/agents[/bold] — dispatch to a specialist (architect, debugger, ...)",
            "    [dim]●[/dim]  [bold]/skills[/bold] — slash-command workflows (/commit, /resolve-issues, ...)",
            "    [dim]●[/dim]  [bold]/codeindex[/bold] — semantic search across your repo",
            "    [dim]●[/dim]  [bold]/schedule[/bold] — background tasks that report back",
            "    [dim]●[/dim]  [bold]/loop[/bold] — repeat a prompt across a batch until done",
            "    [dim]●[/dim]  [bold]/evals[/bold] — benchmark agents on scripted scenarios",
            "    [dim]●[/dim]  [bold]/mcp[/bold] — plug in external tools and data sources",
            "    [dim]●[/dim]  [bold]/plugins[/bold] — install skills, agents, hooks from marketplaces",
            "",
            "  [dim]Enter to send · \\ + Enter for new line · /help for commands[/dim]",
            "",
        ]
        return "\n".join(lines)

    def compose(self) -> ComposeResult:
        _quit_key = shortcut_label("Ctrl+D")
        yield Static(
            f" [bold]igni[/bold] [dim]v{__version__}[/dim]"
            f"    [dim]/help for commands · {_quit_key} to quit[/dim]",
            id="header-bar",
        )
        yield UpdateBar(id="update-bar")
        yield ScrollableContainer(id="conversation")
        yield QueuePanel(id="queue-panel")
        yield TaskPanel(id="task-panel")
        # All bottom chrome collapsed into ONE dock-bottom container.
        # Multiple ``dock: bottom`` siblings in Textual don't stack —
        # they overlap at the same y coordinate, with mount order
        # winning z-order. Having tip-bar and footer both docked-bottom
        # at the Screen meant they fought over the same rows, and
        # mid-session resizes could leave the footer extending past
        # the viewport's bottom edge because Textual computed its
        # auto-height from intrinsic content size without clamping to
        # the available region. With a single fixed-height container
        # holding tip-bar + prompt-row + status-bar, the bottom edge
        # is hard-anchored to the screen bottom and the contents can't
        # outgrow their slot.
        with Vertical(id="footer"):
            with Horizontal(id="prompt-row"):
                yield Static("> ", id="prompt-indicator")
                yield PromptInput(
                    "",
                    id="user-input",
                    compact=True,
                    language=None,
                    soft_wrap=True,
                    show_line_numbers=False,
                    highlight_cursor_line=False,
                    placeholder="Type a message or /help",
                )
            yield StatusBar(id="status-bar")
            yield TipBar(id="tip-bar")

    async def on_mount(self) -> None:
        try:
            await self._on_mount_inner()
        except Exception:
            # Textual silently swallows ``on_mount`` exceptions and
            # tears the app down with exit code 0 — no traceback to
            # stderr, no entry in the debug log. The v0.5.2 incident
            # (renamed theme ``textual-ansi`` → ``ansi-dark`` after
            # a Textual upgrade) shipped as a silent shell-returns-
            # immediately bug for that reason. Surface anything that
            # crashes the mount via the logger so future regressions
            # are at least visible in ``~/.ember/debug.log``.
            logger.exception("TUI on_mount raised — app will exit")
            raise

    async def _on_mount_inner(self) -> None:
        """See :func:`tui.lifecycle_handlers.on_mount_inner`."""
        await lifecycle_handlers.on_mount_inner(self)

    async def _init_mcp_background(self) -> None:
        """See :func:`tui.lifecycle_handlers.init_mcp_background`."""
        await lifecycle_handlers.init_mcp_background(self)

    async def _refresh_cloud_models_on_startup(self) -> None:
        """See :func:`tui.lifecycle_handlers.refresh_cloud_models_on_startup`."""
        await lifecycle_handlers.refresh_cloud_models_on_startup(self)

    async def _auto_sync_knowledge(self) -> None:
        """See :func:`tui.lifecycle_handlers.auto_sync_knowledge`."""
        await lifecycle_handlers.auto_sync_knowledge(self)

    async def on_unmount(self) -> None:
        """See :func:`tui.lifecycle_handlers.on_unmount`."""
        await lifecycle_handlers.on_unmount(self)

    # ── Input events ──────────────────────────────────────────────

    @on(PromptInput.Changed, "#user-input")
    def _on_input_changed(self, event: PromptInput.Changed) -> None:
        """See :func:`tui.input_handlers.on_input_changed`."""
        input_handlers.on_input_changed(self, event)

    def _mount_autocomplete(self, hint: str) -> None:
        """See :func:`tui.input_handlers.mount_autocomplete`."""
        input_handlers.mount_autocomplete(self, hint)

    # ── File picker helpers ────────────────────────────────────

    def _show_file_picker(self, matches: list[str]) -> None:
        """See :func:`tui.input_handlers.show_file_picker`."""
        input_handlers.show_file_picker(self, matches)

    def _hide_file_picker(self) -> None:
        """See :func:`tui.input_handlers.hide_file_picker`."""
        input_handlers.hide_file_picker(self)

    def _insert_file_mention(self, path: str) -> None:
        """See :func:`tui.input_handlers.insert_file_mention`."""
        input_handlers.insert_file_mention(self, path)

    @on(PromptInput.Submitted)
    async def _on_input_submitted(self, event: PromptInput.Submitted) -> None:
        """See :func:`tui.input_handlers.on_input_submitted`."""
        await input_handlers.on_input_submitted(self, event)

    # ── Command mode ─────────────────────────────────────────

    def _update_command_mode_indicator(self) -> None:
        """See :func:`tui.mode_handlers.update_command_mode_indicator`."""
        mode_handlers.update_command_mode_indicator(self)

    def _exit_command_mode(self) -> None:
        """See :func:`tui.mode_handlers.exit_command_mode`."""
        mode_handlers.exit_command_mode(self)

    # ── Shell mode ────────────────────────────────────────────

    def _update_shell_mode_indicator(self) -> None:
        """See :func:`tui.mode_handlers.update_shell_mode_indicator`."""
        mode_handlers.update_shell_mode_indicator(self)

    def _exit_shell_mode(self) -> None:
        """See :func:`tui.mode_handlers.exit_shell_mode`."""
        mode_handlers.exit_shell_mode(self)

    # ── Inline shell execution ───────────────────────────────

    async def _run_shell_inline(self, cmd: str) -> None:
        """See :func:`tui.mode_handlers.run_shell_inline`."""
        await mode_handlers.run_shell_inline(self, cmd)

    async def on_key(self, event) -> None:
        """See :func:`tui.keybinding_handlers.on_key`."""
        await keybinding_handlers.on_key(self, event)

    # ── Command result rendering ──────────────────────────────────

    def render_command_result(self, result: CommandResult) -> None:
        """See :func:`tui.keybinding_handlers.render_command_result`."""
        keybinding_handlers.render_command_result(self, result)

    # ── Session picker events ─────────────────────────────────────

    @on(SessionPickerWidget.Selected)
    async def _on_session_selected(self, event: SessionPickerWidget.Selected) -> None:
        """See :func:`tui.picker_handlers.on_session_selected`."""
        await picker_handlers.on_session_selected(self, event.session_id)

    @on(SessionPickerWidget.Cancelled)
    def _on_session_cancelled(self, _event: SessionPickerWidget.Cancelled) -> None:
        """See :func:`tui.picker_handlers.on_session_cancelled`."""
        picker_handlers.on_session_cancelled(self)

    # ── Model picker ────────────────────────────────────────────────

    def _show_model_picker(self) -> None:
        """See :func:`tui.picker_handlers.show_model_picker`."""
        picker_handlers.show_model_picker(self)

    @on(ModelPickerWidget.Selected)
    async def _on_model_selected(self, event: ModelPickerWidget.Selected) -> None:
        """See :func:`tui.picker_handlers.on_model_selected`."""
        await picker_handlers.on_model_selected(self, event.model_name)

    @on(ModelPickerWidget.Cancelled)
    def _on_model_cancelled(self, _event: ModelPickerWidget.Cancelled) -> None:
        """See :func:`tui.picker_handlers.on_model_cancelled`."""
        picker_handlers.on_model_cancelled(self)

    # ── Login ────────────────────────────────────────────────────────

    def _show_login(self) -> None:
        """See :func:`tui.picker_handlers.show_login`."""
        picker_handlers.show_login(self)

    @on(LoginWidget.LoggedIn)
    def _on_logged_in(self, event: LoginWidget.LoggedIn) -> None:
        """See :func:`tui.picker_handlers.on_logged_in`."""
        picker_handlers.on_logged_in(self, event.email)

    @on(LoginWidget.Cancelled)
    def _on_login_cancelled(self, _event: LoginWidget.Cancelled) -> None:
        """See :func:`tui.picker_handlers.on_login_cancelled`."""
        picker_handlers.on_login_cancelled(self)

    def _on_login_status_push(self, payload: dict) -> None:
        """See :func:`tui.picker_handlers.on_login_status_push`."""
        picker_handlers.on_login_status_push(self, payload)

    def _on_login_result_push(self, payload: dict) -> None:
        """See :func:`tui.picker_handlers.on_login_result_push`."""
        picker_handlers.on_login_result_push(self, payload)

    # ── Help panel ───────────────────────────────────────────────────

    def _show_help_panel(self) -> None:
        """See :func:`tui.picker_handlers.show_help_panel`."""
        picker_handlers.show_help_panel(self)

    @on(HelpPanelWidget.PanelClosed)
    def _on_help_panel_closed(self, _event: HelpPanelWidget.PanelClosed) -> None:
        """See :func:`tui.picker_handlers.on_help_panel_closed`."""
        picker_handlers.on_help_panel_closed(self)

    # ── MCP panel ───────────────────────────────────────────────────

    async def _show_mcp_panel(self) -> None:
        """See :func:`tui.mcp_handlers.show_mcp_panel`."""
        await mcp_handlers.show_mcp_panel(self)

    async def _build_mcp_server_list(self) -> list[MCPServerInfo]:
        """See :func:`tui.mcp_handlers.build_mcp_server_list`."""
        return await mcp_handlers.build_mcp_server_list(self)

    @on(MCPPanelWidget.ServerToggleRequested)
    async def _on_mcp_toggle(self, event: MCPPanelWidget.ServerToggleRequested) -> None:
        asyncio.create_task(self._toggle_mcp(event.name, event.enable))

    async def _toggle_mcp(self, name: str, enable: bool) -> None:
        """See :func:`tui.mcp_handlers.toggle_mcp`."""
        await mcp_handlers.toggle_mcp(self, name, enable)

    @on(MCPPanelWidget.PanelClosed)
    def _on_mcp_panel_closed(self, _event: MCPPanelWidget.PanelClosed) -> None:
        """See :func:`tui.mcp_handlers.on_mcp_panel_closed`."""
        mcp_handlers.on_mcp_panel_closed(self)

    # ── Agents panel ──────────────────────────────────────────────

    async def _show_agents_panel(self) -> None:
        """See :func:`tui.agent_handlers.show_agents_panel`."""
        await agent_handlers.show_agents_panel(self)

    async def _build_agent_list(self) -> list[AgentInfo]:
        """See :func:`tui.agent_handlers.build_agent_list`."""
        return await agent_handlers.build_agent_list(self)

    async def _refresh_agents_panel(self) -> None:
        """See :func:`tui.agent_handlers.refresh_agents_panel`."""
        await agent_handlers.refresh_agents_panel(self)

    @on(AgentsPanelWidget.PromoteRequested)
    async def _on_agent_promote(
        self,
        event: AgentsPanelWidget.PromoteRequested,
    ) -> None:
        """See :func:`tui.agent_handlers.on_agent_promote`."""
        await agent_handlers.on_agent_promote(self, event.name)

    @on(AgentsPanelWidget.DiscardRequested)
    async def _on_agent_discard(
        self,
        event: AgentsPanelWidget.DiscardRequested,
    ) -> None:
        """See :func:`tui.agent_handlers.on_agent_discard`."""
        await agent_handlers.on_agent_discard(self, event.name)

    @on(AgentsPanelWidget.PanelClosed)
    def _on_agents_panel_closed(
        self,
        _event: AgentsPanelWidget.PanelClosed,
    ) -> None:
        """See :func:`tui.agent_handlers.on_agents_panel_closed`."""
        agent_handlers.on_agents_panel_closed(self)

    # ── Skills panel ──────────────────────────────────────────────

    async def _show_skills_panel(self) -> None:
        """See :func:`tui.agent_handlers.show_skills_panel`."""
        await agent_handlers.show_skills_panel(self)

    async def _build_skill_list(self) -> list[SkillInfo]:
        """See :func:`tui.agent_handlers.build_skill_list`."""
        return await agent_handlers.build_skill_list(self)

    @on(SkillsPanelWidget.RunRequested)
    async def _on_skill_run(
        self,
        event: SkillsPanelWidget.RunRequested,
    ) -> None:
        """See :func:`tui.agent_handlers.on_skill_run`."""
        await agent_handlers.on_skill_run(self, event.name)

    @on(SkillsPanelWidget.PanelClosed)
    def _on_skills_panel_closed(
        self,
        _event: SkillsPanelWidget.PanelClosed,
    ) -> None:
        """See :func:`tui.agent_handlers.on_skills_panel_closed`."""
        agent_handlers.on_skills_panel_closed(self)

    # ── Knowledge panel ──────────────────────────────────────────

    async def _show_knowledge_panel(self) -> None:
        """See :func:`tui.knowledge_handlers.show_knowledge_panel`."""
        await knowledge_handlers.show_knowledge_panel(self)

    @on(KnowledgePanelWidget.SearchRequested)
    async def _on_knowledge_search(
        self,
        event: KnowledgePanelWidget.SearchRequested,
    ) -> None:
        """See :func:`tui.knowledge_handlers.on_knowledge_search`."""
        await knowledge_handlers.on_knowledge_search(self, event.query)

    @on(KnowledgePanelWidget.AddRequested)
    async def _on_knowledge_add(
        self,
        event: KnowledgePanelWidget.AddRequested,
    ) -> None:
        """See :func:`tui.knowledge_handlers.on_knowledge_add`."""
        await knowledge_handlers.on_knowledge_add(self, event.source)

    @on(KnowledgePanelWidget.PanelClosed)
    def _on_knowledge_panel_closed(
        self,
        _event: KnowledgePanelWidget.PanelClosed,
    ) -> None:
        """See :func:`tui.knowledge_handlers.on_knowledge_panel_closed`."""
        knowledge_handlers.on_knowledge_panel_closed(self)

    # ── CodeIndex status bar (always-on) ─────────────────────────

    # Cadence for the always-on CodeIndex status-bar refresh. Slower
    # than the panel poll (2s) because the bar shows a coarse signal
    # (indexed / syncing / uninstalled / error) — extra precision
    # wouldn't change anything the user sees. Independent of the
    # panel so the badge stays current even when the panel is shut.
    _CODEINDEX_STATUSBAR_POLL_SECONDS = 5.0

    async def _refresh_codeindex_badge(self) -> None:
        """Refresh the CodeIndex status-bar slot.

        Best-effort: a transport hiccup on a background poll
        shouldn't surface anywhere — the next tick retries, and
        ``set_codeindex_status(None)`` keeps the previous render
        rather than blanking the badge. Also called eagerly after
        sync/clean/install RPCs to avoid the user staring at a
        stale badge until the next 5s tick.
        """
        backend = getattr(self, "_backend", None)
        if backend is None:
            return
        try:
            status_dict = await backend.codeindex_status()
        except Exception:
            logger.debug("codeindex status-bar refresh failed", exc_info=True)
            return
        try:
            self._status.set_codeindex_status(CodeIndexStatusInfo(**status_dict))
        except Exception:
            logger.debug("codeindex status-bar update failed", exc_info=True)

    # ── CodeIndex panel ──────────────────────────────────────────

    # Period for the codeindex-panel status poll (seconds). Tight
    # enough that a syncing-% indicator updates smoothly without
    # blasting the backend with RPCs; loose enough that the poll
    # doesn't drown out the user's own actions (which also refresh
    # the status as a side-effect).
    _CODEINDEX_STATUS_POLL_SECONDS = 2.0

    async def _show_codeindex_panel(self) -> None:
        """See :func:`tui.codeindex_handlers.show_codeindex_panel`."""
        await codeindex_handlers.show_codeindex_panel(self)

    async def _poll_codeindex_status(self) -> None:
        """See :func:`tui.codeindex_handlers.poll_codeindex_status`."""
        await codeindex_handlers.poll_codeindex_status(self)

    @on(CodeIndexPanelWidget.SyncRequested)
    async def _on_codeindex_sync(
        self,
        _event: CodeIndexPanelWidget.SyncRequested,
    ) -> None:
        """See :func:`tui.codeindex_handlers.on_codeindex_sync`."""
        await codeindex_handlers.on_codeindex_sync(self)

    @on(CodeIndexPanelWidget.ResyncRequested)
    async def _on_codeindex_resync(
        self,
        _event: CodeIndexPanelWidget.ResyncRequested,
    ) -> None:
        """See :func:`tui.codeindex_handlers.on_codeindex_resync`."""
        await codeindex_handlers.on_codeindex_resync(self)

    @on(CodeIndexPanelWidget.CleanRequested)
    async def _on_codeindex_clean(
        self,
        _event: CodeIndexPanelWidget.CleanRequested,
    ) -> None:
        """See :func:`tui.codeindex_handlers.on_codeindex_clean`."""
        await codeindex_handlers.on_codeindex_clean(self)

    @on(CodeIndexPanelWidget.InstallRequested)
    async def _on_codeindex_install(
        self,
        _event: CodeIndexPanelWidget.InstallRequested,
    ) -> None:
        """See :func:`tui.codeindex_handlers.on_codeindex_install`."""
        await codeindex_handlers.on_codeindex_install(self)

    @on(CodeIndexPanelWidget.PanelClosed)
    def _on_codeindex_panel_closed(
        self,
        _event: CodeIndexPanelWidget.PanelClosed,
    ) -> None:
        """See :func:`tui.codeindex_handlers.on_codeindex_panel_closed`."""
        codeindex_handlers.on_codeindex_panel_closed(self)

    # ── Loop panel ────────────────────────────────────────────────

    # Tighter poll than the CodeIndex panel (2s) — ``/loop`` iterations
    # fire on idle, and a counter that ticks within a second feels
    # live; at 2s the user notices the lag.
    _LOOP_STATUS_POLL_SECONDS = 1.0

    async def _show_loop_panel(self) -> None:
        """See :func:`tui.loop_handlers.show_loop_panel`."""
        await loop_handlers.show_loop_panel(self)

    async def _poll_loop_status(self) -> None:
        """See :func:`tui.loop_handlers.poll_loop_status`."""
        await loop_handlers.poll_loop_status(self)

    @on(LoopPanelWidget.ResumeRequested)
    async def _on_loop_resume(
        self,
        _event: LoopPanelWidget.ResumeRequested,
    ) -> None:
        """See :func:`tui.loop_handlers.on_loop_resume`."""
        await loop_handlers.on_loop_resume(self)

    @on(LoopPanelWidget.CancelRequested)
    async def _on_loop_cancel(
        self,
        _event: LoopPanelWidget.CancelRequested,
    ) -> None:
        """See :func:`tui.loop_handlers.on_loop_cancel`."""
        await loop_handlers.on_loop_cancel(self)

    # ── Hooks panel ──────────────────────────────────────────────

    async def _show_hooks_panel(self) -> None:
        """See :func:`tui.agent_handlers.show_hooks_panel`."""
        await agent_handlers.show_hooks_panel(self)

    @on(HooksPanelWidget.ReloadRequested)
    async def _on_hooks_reload(
        self,
        _event: HooksPanelWidget.ReloadRequested,
    ) -> None:
        """See :func:`tui.agent_handlers.on_hooks_reload`."""
        await agent_handlers.on_hooks_reload(self)

    @on(HooksPanelWidget.PanelClosed)
    def _on_hooks_panel_closed(
        self,
        _event: HooksPanelWidget.PanelClosed,
    ) -> None:
        """See :func:`tui.agent_handlers.on_hooks_panel_closed`."""
        agent_handlers.on_hooks_panel_closed(self)

    @on(LoopPanelWidget.PanelClosed)
    def _on_loop_panel_closed(
        self,
        _event: LoopPanelWidget.PanelClosed,
    ) -> None:
        """See :func:`tui.loop_handlers.on_loop_panel_closed`."""
        loop_handlers.on_loop_panel_closed(self)

    # ── Plugins panel ─────────────────────────────────────────────

    async def _show_plugins_panel(self) -> None:
        """See :func:`tui.plugin_handlers.show_plugins_panel`."""
        await plugin_handlers.show_plugins_panel(self)

    async def _build_plugin_state(
        self,
    ) -> tuple[list[PluginInfo], list[MarketplaceInfo]]:
        """See :func:`tui.plugin_handlers.build_plugin_state`."""
        return await plugin_handlers.build_plugin_state(self)

    async def _refresh_plugins_panel(self) -> None:
        """See :func:`tui.plugin_handlers.refresh_plugins_panel`."""
        await plugin_handlers.refresh_plugins_panel(self)

    @on(PluginsPanelWidget.PluginToggleRequested)
    async def _on_plugin_toggle(
        self,
        event: PluginsPanelWidget.PluginToggleRequested,
    ) -> None:
        """See :func:`tui.plugin_handlers.on_plugin_toggle`."""
        await plugin_handlers.on_plugin_toggle(self, event.name, event.enable)

    @on(PluginsPanelWidget.PluginInstallRequested)
    async def _on_plugin_install(
        self,
        event: PluginsPanelWidget.PluginInstallRequested,
    ) -> None:
        """See :func:`tui.plugin_handlers.on_plugin_install`."""
        await plugin_handlers.on_plugin_install(self, event.ref, event.install_ref)

    @on(PluginsPanelWidget.PluginUpdateRequested)
    async def _on_plugin_update(
        self,
        event: PluginsPanelWidget.PluginUpdateRequested,
    ) -> None:
        """See :func:`tui.plugin_handlers.on_plugin_update`."""
        await plugin_handlers.on_plugin_update(self, event.name)

    @on(PluginsPanelWidget.PluginRemoveRequested)
    async def _on_plugin_remove(
        self,
        event: PluginsPanelWidget.PluginRemoveRequested,
    ) -> None:
        """See :func:`tui.plugin_handlers.on_plugin_remove`."""
        await plugin_handlers.on_plugin_remove(self, event.name)

    @on(PluginsPanelWidget.MarketplaceRefreshRequested)
    async def _on_marketplace_refresh(
        self,
        _event: PluginsPanelWidget.MarketplaceRefreshRequested,
    ) -> None:
        """See :func:`tui.plugin_handlers.on_marketplace_refresh`."""
        await plugin_handlers.on_marketplace_refresh(self)

    @on(PluginsPanelWidget.PanelClosed)
    def _on_plugins_panel_closed(
        self,
        _event: PluginsPanelWidget.PanelClosed,
    ) -> None:
        """See :func:`tui.plugin_handlers.on_plugins_panel_closed`."""
        plugin_handlers.on_plugins_panel_closed(self)

    # ── Queue panel events ─────────────────────────────────────────

    @on(QueuePanel.ItemDeleted)
    def _on_queue_item_deleted(self, event: QueuePanel.ItemDeleted) -> None:
        """See :func:`tui.scheduler_handlers.on_queue_item_deleted`."""
        scheduler_handlers.on_queue_item_deleted(self, event.index)

    @on(QueuePanel.ItemEditRequested)
    def _on_queue_item_edit(self, event: QueuePanel.ItemEditRequested) -> None:
        """See :func:`tui.scheduler_handlers.on_queue_item_edit`."""
        scheduler_handlers.on_queue_item_edit(self, event.index, event.text)

    @on(QueuePanel.PanelClosed)
    def _on_queue_panel_closed(self, _event: QueuePanel.PanelClosed) -> None:
        """See :func:`tui.scheduler_handlers.on_queue_panel_closed`."""
        scheduler_handlers.on_queue_panel_closed(self)

    # ── Task panel events ──────────────────────────────────────────

    @on(TaskPanel.TaskCancelled)
    async def _on_task_cancelled(self, event: TaskPanel.TaskCancelled) -> None:
        """See :func:`tui.scheduler_handlers.on_task_cancelled`."""
        await scheduler_handlers.on_task_cancelled(self, event.task_id)

    @on(TaskPanel.PanelClosed)
    def _on_task_panel_closed(self, _event: TaskPanel.PanelClosed) -> None:
        """See :func:`tui.scheduler_handlers.on_task_panel_closed`."""
        scheduler_handlers.on_task_panel_closed(self)

    # ── Scheduler ────────────────────────────────────────────────

    def _start_scheduler(self) -> None:
        """See :func:`tui.scheduler_handlers.start_scheduler`."""
        scheduler_handlers.start_scheduler(self)

    async def _execute_scheduled_task(self, description: str) -> str:
        """See :func:`tui.scheduler_handlers.execute_scheduled_task`."""
        return await scheduler_handlers.execute_scheduled_task(self, description)

    def _on_scheduled_task_started(self, task_id: str, description: str) -> None:
        """See :func:`tui.scheduler_handlers.on_scheduled_task_started`."""
        scheduler_handlers.on_scheduled_task_started(self, task_id, description)

    def _on_scheduled_task_completed(self, task_id: str, description: str, success: bool) -> None:
        """See :func:`tui.scheduler_handlers.on_scheduled_task_completed`."""
        scheduler_handlers.on_scheduled_task_completed(self, task_id, description, success)

    async def _refresh_task_panel(self) -> None:
        """See :func:`tui.scheduler_handlers.refresh_task_panel`."""
        await scheduler_handlers.refresh_task_panel(self)

    # ── Actions (Textual keybindings) ─────────────────────────────

    def action_clear_screen(self) -> None:
        """See :func:`tui.keybinding_handlers.action_clear_screen`."""
        keybinding_handlers.action_clear_screen(self)

    def action_toggle_expand_all(self) -> None:
        """See :func:`tui.keybinding_handlers.action_toggle_expand_all`."""
        keybinding_handlers.action_toggle_expand_all(self)

    def action_toggle_queue(self) -> None:
        """See :func:`tui.keybinding_handlers.action_toggle_queue`."""
        keybinding_handlers.action_toggle_queue(self)

    async def action_toggle_tasks(self) -> None:
        """See :func:`tui.keybinding_handlers.action_toggle_tasks`."""
        await keybinding_handlers.action_toggle_tasks(self)

    async def _auto_refresh_tasks(self) -> None:
        """See :func:`tui.keybinding_handlers.auto_refresh_tasks`."""
        await keybinding_handlers.auto_refresh_tasks(self)

    def action_toggle_verbose(self) -> None:
        """See :func:`tui.keybinding_handlers.action_toggle_verbose`."""
        keybinding_handlers.action_toggle_verbose(self)

    async def _check_for_update(self) -> None:
        """See :func:`tui.lifecycle_handlers.check_for_update`."""
        await lifecycle_handlers.check_for_update(self)

    # ── Tips ───────────────────────────────────────────────────────

    _TIPS = [
        "/model — switch the active model",
        "/help — list all commands and shortcuts",
        "/sessions — browse and resume past sessions",
        "/clear — reset conversation context",
        "\\ + Enter inserts a newline",
        "/agents — list loaded agents and their tools",
        "/skills — list available skills",
        "/config — show current settings",
        "/schedule <task> at <time> — schedule deferred tasks",
        "/mcp — manage MCP server connections",
        "Ctrl+T — toggle the task panel",
    ]

    def _start_tip_rotation(self) -> None:
        try:
            tip_bar = self.query_one("#tip-bar", TipBar)
            tip_bar.set_tip(random.choice(self._TIPS))
            self.set_interval(30, self._rotate_tip)
        except Exception:
            pass

    def _rotate_tip(self) -> None:
        try:
            tip_bar = self.query_one("#tip-bar", TipBar)
            tip_bar.set_tip(random.choice(self._TIPS))
        except Exception:
            pass

    def _on_mirror_event(self, message: pmsg.Message) -> None:
        """Render mirroring events from other views on the same BE.

        Called from the BackendClient reader task — UI mutations are
        scheduled via ``call_later`` onto Textual's message pump.
        """
        if isinstance(message, pmsg.Typing) and message.client_id != "tui":

            def _update_tip(text: str = message.text) -> None:
                with contextlib.suppress(Exception):
                    tip = self.query_one("#tip-bar", TipBar)
                    if text:
                        tip.set_tip(f"✎ another window: {text}")
                    else:
                        tip.set_tip(random.choice(self._TIPS))

            self.call_later(_update_tip)
        elif isinstance(message, pmsg.UserMessageReceived) and message.client_id != "tui":
            # A message submitted from another view — show it so the
            # TUI's conversation matches what the agent sees.
            conv = self._conversation
            if conv is not None and message.text:
                label = "queued from another window" if message.queued else "another window"
                self.call_later(conv.append_info, f"[{label}] {message.text}")

    def on_resize(self) -> None:
        """Clear the compositor + force a full repaint on every resize.

        Empirically the best of the approaches tried:

        * No handler at all → narrowing leaves widgets sized for
          the previous (wider) geometry; visible content doesn't
          shrink to fit.
        * ``self.refresh(repaint=True)`` only → repaints inside
          new regions but doesn't clear cells outside, leaving
          ghost right-edges of boxes.
        * Debounced refresh → final state is clean but
          intermediate frames during the drag show stale content.

        Immediate ``Compositor.clear()`` discards the cached
        widget-to-region map; the screen refresh that follows
        rewrites every visible cell against the new size. The
        delayed second pass catches the final geometry after a
        drag — sometimes the last resize event fires before
        Textual has fully settled the size, and a tiny grace
        period gives it time to land.
        """
        with contextlib.suppress(Exception):
            self.screen._compositor.clear()
        self.screen.refresh(layout=True, repaint=True)
        # One more pass shortly after — covers the final state of
        # a drag-resize where the last event fires mid-settle.
        self.set_timer(0.08, self._post_resize_repaint)

    def _post_resize_repaint(self) -> None:
        with contextlib.suppress(Exception):
            self.screen._compositor.clear()
        self.screen.refresh(layout=True, repaint=True)

    def action_cancel(self) -> None:
        """See :func:`tui.keybinding_handlers.action_cancel`."""
        keybinding_handlers.action_cancel(self)
