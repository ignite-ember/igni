"""Ember Code TUI — main application.

Thin shell that composes Textual widgets and delegates logic to
``ConversationView``, ``StatusTracker``, ``RunController``,
``HITLHandler``, and ``SessionManager``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from pathlib import Path
from typing import Any

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.css.query import NoMatches
from textual.events import Resize
from textual.timer import Timer
from textual.widgets import Static

from ember_code import __version__
from ember_code.frontend.tui.conversation_view import ConversationView
from ember_code.frontend.tui.file_index import FileIndex
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
from ember_code.protocol.messages import CommandResult
from ember_code.protocol.rpc import RpcMethod

logger = logging.getLogger(__name__)


class EmberApp(App):
    """Ember Code Terminal UI Application."""

    TITLE = "Ember Code"
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
        min-height: 5;
        height: auto;
        width: 100%;
    }

    #prompt-row {
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
        height: auto !important;
        min-height: 1;
        max-height: 8;
        border: none !important;
        background: $background;
        color: $text;
        padding: 0;
    }

    #user-input:focus {
        border: none !important;
    }

    #status-bar {
        /* 3 rows = 1 row for ``border-top`` + 2 content rows. The
           bar renders two lines (identity on top, live activity on
           bottom) so any smaller height clips the second line. */
        height: 3;
        width: 100%;
        border-top: solid ansi_bright_black;
        content-align: center middle;
        text-align: center;
        color: $text-muted;
    }

    #tip-bar {
        dock: bottom;
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
        import subprocess

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
            import pwd

            return pwd.getpwuid(os.getuid()).pw_gecos.split(",")[0] or os.getlogin()
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
            "  [bold]Why Ember Code:[/bold]",
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
            f" [bold]Ember Code[/bold] [dim]v{__version__}[/dim]"
            f"    [dim]/help for commands · {_quit_key} to quit[/dim]",
            id="header-bar",
        )
        yield UpdateBar(id="update-bar")
        yield ScrollableContainer(id="conversation")
        yield QueuePanel(id="queue-panel")
        yield TaskPanel(id="task-panel")
        yield TipBar(id="tip-bar")
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

    async def on_mount(self) -> None:
        # Use ANSI colors so the terminal's own palette is respected
        self.ansi_color = True
        self.theme = "textual-ansi"

        container = self.query_one("#conversation", ScrollableContainer)

        # Show loading indicator while BE starts
        loading = Static("[dim]Starting backend...[/dim]", id="loading-msg")
        await container.mount(loading)

        # Spawn BE as a separate subprocess — no Textual fd restrictions
        from ember_code.frontend.tui.process_manager import BackendProcess

        self._process_mgr = BackendProcess(
            project_dir=self._project_dir,
            resume_session_id=self.resume_session_id,
            additional_dirs=self._additional_dirs,
            settings=self.settings,
            debug=self._debug,
        )
        self._backend = await self._process_mgr.start()

        # Replace loading indicator with welcome content
        await container.remove_children()
        self._conversation = ConversationView(container, display_config=self.settings.display)

        await container.mount(Static(self._build_welcome_content(), id="welcome-box"))
        await container.mount(Static(self._build_capabilities_text(), id="capabilities"))

        self._file_index = FileIndex(self._project_dir)
        self._input_handler = InputHandler(
            self._backend.get_skill_pool(), file_index=self._file_index
        )
        # CommandHandler is now inside BackendServer — commands route through _backend.handle_command()

        # Initialise managers
        self._status = StatusTracker(self)
        self._hitl = HITLHandler(self, self._conversation)
        self._controller = RunController(
            self,
            self._conversation,
            self._status,
            self._hitl,
        )
        self._sessions = SessionManager(
            self,
            self._conversation,
            self._status,
        )

        # Resolve context window for the active model
        # Context window comes from settings — no model registry needed in FE
        self._status.max_context_tokens = self.settings.models.max_context_window

        self._status.update_status_bar()

        # Load previous messages if resuming a session
        if self.resume_session_id:
            await self._sessions._load_history(self.resume_session_id)

        # Show a random tip
        self._start_tip_rotation()

        self.query_one("#user-input", PromptInput).focus()

        # ── Login push handlers (permanent — widget checks if mounted) ──
        self._backend._push_handlers["login_status"] = self._on_login_status_push
        self._backend._push_handlers["login_result"] = self._on_login_result_push

        # ── Scheduler ──────────────────────────────────────────────────
        self._start_scheduler()

        # ── Fire SessionStart hook ────────────────────────────────────
        asyncio.create_task(self._backend.fire_session_start_hook())

        # ── Non-blocking background init ──────────────────────────────
        asyncio.create_task(self._check_for_update())
        asyncio.create_task(self._init_mcp_background())
        asyncio.create_task(self._file_index.ensure_loaded())
        asyncio.create_task(self._auto_sync_knowledge())

        # CodeIndex status-bar slot — eager refresh + recurring
        # poll so the badge reflects current state even before the
        # user opens the ``/codeindex`` panel. Survives panel close
        # (independent from the 2s panel poll which only runs while
        # the panel is mounted).
        asyncio.create_task(self._refresh_codeindex_badge())
        self.set_interval(
            self._CODEINDEX_STATUSBAR_POLL_SECONDS,
            self._refresh_codeindex_badge,
        )

        if self.initial_message:
            task = asyncio.create_task(
                self._controller.process_message(self.initial_message),
            )
            self._controller.set_current_task(task)

    async def _init_mcp_background(self) -> None:
        """Connect user-configured MCP servers in the background."""
        logger.info("FE: MCP background init starting")
        try:
            await self._backend.ensure_mcp()
            statuses = (
                await self._backend._rpc(RpcMethod.GET_MCP_STATUS)
                if hasattr(self._backend, "_rpc")
                else self._backend.get_mcp_status()
            )
            if statuses:
                # Same per-server log as the BE side — gives a
                # complete picture in ``debug.log`` of "connected
                # at T1 (BE)" → "FE saw it at T2".
                connected_names = [n for n, c in statuses if c]
                logger.info(
                    "FE: MCP background init done — %d/%d connected: %s",
                    len(connected_names),
                    len(statuses),
                    connected_names,
                )
                for name, connected in statuses:
                    self._status.set_ide_status(name, connected)
            else:
                logger.info("FE: MCP background init done — no servers configured")
        except Exception as exc:
            # Upgraded from DEBUG to WARNING so this is visible
            # without the user re-running with ``--debug``.
            logger.warning("FE: MCP background init failed: %s", exc, exc_info=True)

    async def _auto_sync_knowledge(self) -> None:
        """Auto-sync knowledge file → DB on startup if enabled."""
        try:
            result = await self._backend.auto_sync_knowledge()
            if result:
                self._conversation.append_info(result)
        except Exception as e:
            logger.warning("Auto knowledge sync failed: %s", e)

    async def on_unmount(self) -> None:
        """Clean up scheduler and BE subprocess on app exit."""
        import os
        import sys

        if self._scheduler_runner:
            self._scheduler_runner.stop()

        # Redirect fd 2 → /dev/null BEFORE stopping BE.
        # MCP stdio cleanup triggers anyio cancel scope errors that
        # print after the TUI exits.
        try:
            sys.stderr.flush()
            devnull_fd = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull_fd, 2)
            os.close(devnull_fd)
        except OSError:
            pass

        if self._process_mgr:
            await self._process_mgr.stop()

    # ── Input events ──────────────────────────────────────────────

    @on(PromptInput.Changed, "#user-input")
    def _on_input_changed(self, event: PromptInput.Changed) -> None:
        text_area = event.text_area
        text = text_area.text

        # ── Mode toggles (/, !, $) ─────────────────────────────
        if not self._shell_mode and not self._command_mode and text in ("/", "!", "$"):
            if text == "/":
                self._command_mode = True
                self._update_command_mode_indicator()
            else:
                self._shell_mode = True
                self._update_shell_mode_indicator()
            text_area.clear()
            return
        if (
            not self._shell_mode
            and not self._command_mode
            and (text.startswith("! ") or text.startswith("$ "))
        ):
            self._shell_mode = True
            text_area.clear()
            text_area.insert(text[2:])
            self._update_shell_mode_indicator()
            return

        # ── @file mention detection ──────────────────────────────
        row, col = text_area.cursor_location
        mention_query = extract_at_mention(row, col, text_area.document.get_line)
        if mention_query is not None and self._input_handler:
            matches = self._input_handler.get_file_completions(mention_query)
            self._show_file_picker(matches)
            # Hide slash autocomplete if visible
            with contextlib.suppress(NoMatches):
                self.query_one("#autocomplete", Static).display = False
            return

        # Hide file picker when not in @-mention
        self._hide_file_picker()

        # ── Slash command autocomplete ───────────────────────────
        try:
            widget = self.query_one("#autocomplete", Static)
        except NoMatches:
            widget = None

        if self._input_handler:
            # In command mode, text has no / prefix — add it for autocomplete
            completion_text = f"/{text}" if self._command_mode else text
            matches = self._input_handler.get_completions(completion_text)
            if self._command_mode:
                # Strip / from suggestions since indicator already shows it
                matches = [m.lstrip("/") for m in matches]
            if matches:
                hint = "  ".join(matches)
                if widget:
                    widget.update(f"[dim]{hint}[/dim]")
                    widget.display = True
                else:
                    self._mount_autocomplete(hint)
                return

        if widget:
            widget.display = False

    def _mount_autocomplete(self, hint: str) -> None:
        try:
            area = self.query_one("#footer", Vertical)
            area.mount(Static(f"[dim]{hint}[/dim]", id="autocomplete"))
        except Exception:
            pass

    # ── File picker helpers ────────────────────────────────────

    def _show_file_picker(self, matches: list[str]) -> None:
        """Show or update the file picker dropdown."""
        input_widget = self.query_one("#user-input", PromptInput)
        input_widget.suppress_submit = True
        try:
            picker = self.query_one(FilePickerDropdown)
            picker.update_matches(matches)
        except NoMatches:
            picker = FilePickerDropdown(matches)
            try:
                footer = self.query_one("#footer", Vertical)
                prompt_row = self.query_one("#prompt-row")
                footer.mount(picker, before=prompt_row)
            except Exception:
                pass

    def _hide_file_picker(self) -> None:
        """Remove the file picker dropdown if present."""
        with contextlib.suppress(NoMatches):
            self.query_one(FilePickerDropdown).remove()
        with contextlib.suppress(NoMatches):
            self.query_one("#user-input", PromptInput).suppress_submit = False

    def _insert_file_mention(self, path: str) -> None:
        """Replace the @query with the selected file path."""
        input_widget = self.query_one("#user-input", PromptInput)
        row, col = input_widget.cursor_location
        line = input_widget.document.get_line(row)

        # Find the @ position by scanning backward
        at_pos = col - 1
        while at_pos >= 0 and line[at_pos] != "@":
            at_pos -= 1

        if at_pos < 0:
            return

        # Rebuild the full text with the replacement
        full_text = input_widget.text
        lines = full_text.split("\n")
        old_line = lines[row]
        # Replace from after @ to cursor position with the full path
        new_line = old_line[: at_pos + 1] + path + " " + old_line[col:]
        lines[row] = new_line
        new_text = "\n".join(lines)

        # Calculate new cursor position (after path + space)
        new_col = at_pos + 1 + len(path) + 1

        input_widget.clear()
        input_widget.insert(new_text)
        input_widget.move_cursor((row, new_col))

    @on(PromptInput.Submitted)
    async def _on_input_submitted(self, event: PromptInput.Submitted) -> None:
        """Handle Enter — PromptInput posts Submitted with the text."""
        input_widget = self.query_one("#user-input", PromptInput)
        if self._input_handler:
            submitted = self._input_handler.on_submit(event.text)
            if submitted:
                input_widget.clear()
                with contextlib.suppress(NoMatches):
                    self.query_one("#autocomplete", Static).display = False

                # Command mode — prepend / and exit
                if self._command_mode:
                    submitted = f"/{submitted}"
                    self._exit_command_mode()

                # Auto-expand a partial slash command when exactly one
                # built-in or skill matches (e.g. `/codei` → `/codeindex`).
                if submitted.startswith("/") and not submitted.startswith("//"):
                    submitted = self._input_handler.expand_unique_command(submitted)

                # Shell mode — run as command, stay in shell mode
                if self._shell_mode:
                    self._shell_task = asyncio.create_task(self._run_shell_inline(submitted))
                    return

                # ! or $ prefix from chat mode — one-off shell command
                if submitted.startswith(("!", "$")) and len(submitted) > 1:
                    self._shell_task = asyncio.create_task(
                        self._run_shell_inline(submitted[1:].strip())
                    )
                    return

                task = asyncio.create_task(
                    self._controller.process_message(submitted),
                )
                if not self._controller.processing:
                    self._controller.set_current_task(task)

    # ── Command mode ─────────────────────────────────────────

    def _update_command_mode_indicator(self) -> None:
        """Update the prompt indicator and placeholder for command mode."""
        try:
            indicator = self.query_one("#prompt-indicator", Static)
            input_widget = self.query_one("#user-input", PromptInput)
            if self._command_mode:
                indicator.update("[bold cyan]/ [/bold cyan]")
                input_widget.placeholder = "Command name (Esc to return to chat)"
            else:
                indicator.update("> ")
                input_widget.placeholder = "Type a message or /help"
        except NoMatches:
            pass

    def _exit_command_mode(self) -> None:
        """Exit command mode and return to chat."""
        self._command_mode = False
        self._update_command_mode_indicator()
        with contextlib.suppress(NoMatches):
            self.query_one("#user-input", PromptInput).clear()

    # ── Shell mode ────────────────────────────────────────────

    def _update_shell_mode_indicator(self) -> None:
        """Update the prompt indicator and placeholder for shell mode."""
        try:
            indicator = self.query_one("#prompt-indicator", Static)
            input_widget = self.query_one("#user-input", PromptInput)
            if self._shell_mode:
                indicator.update("[bold $warning]$ [/bold $warning]")
                input_widget.placeholder = "Shell command (Esc to return to chat)"
            else:
                indicator.update("> ")
                input_widget.placeholder = "Type a message or /help"
        except NoMatches:
            pass

    def _exit_shell_mode(self) -> None:
        """Exit shell mode and return to chat."""
        self._shell_mode = False
        self._update_shell_mode_indicator()
        with contextlib.suppress(NoMatches):
            self.query_one("#user-input", PromptInput).clear()

    # ── Inline shell execution ───────────────────────────────

    async def _run_shell_inline(self, cmd: str) -> None:
        """Run a shell command inline, show output, and add to conversation context.

        The command and output are stored so the AI sees them as context
        in the next message, but no AI response is triggered.
        """
        if not cmd:
            return

        self._conversation.append_user(f"$ {cmd}")

        import os
        import signal

        # Mount a live output widget that updates as lines arrive
        output_widget = Static("[dim]...[/dim]", classes="info-message")
        self._conversation.append(output_widget)
        lines: list[str] = []

        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self._project_dir) if self._project_dir else None,
                start_new_session=True,
            )
            self._shell_proc = proc

            try:
                assert proc.stdout is not None
                while True:
                    try:
                        raw = await asyncio.wait_for(proc.stdout.readline(), timeout=0.5)
                    except asyncio.TimeoutError:
                        if proc.returncode is not None:
                            break
                        continue
                    if not raw:
                        break
                    line = raw.decode(errors="replace").rstrip()
                    lines.append(line)
                    # Show last 50 lines in the live widget (escape Rich markup)
                    from rich.markup import escape

                    visible = escape("\n".join(lines[-50:]))
                    output_widget.update(f"[dim]{visible}[/dim]")
                    # Auto-scroll
                    try:
                        container = self.query_one("#conversation")
                        container.scroll_end(animate=False)
                    except NoMatches:
                        pass
                await proc.wait()
            except asyncio.CancelledError:
                with contextlib.suppress(ProcessLookupError, OSError):
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                with contextlib.suppress(ProcessLookupError, OSError):
                    proc.kill()
                lines.append("(cancelled)")

            exit_code = proc.returncode or 0
        except Exception as e:
            lines.append(f"(error: {e})")
            exit_code = -1
        finally:
            self._shell_proc = None

        # Final update with all output
        from rich.markup import escape

        output = "\n".join(lines)
        if output:
            output_widget.update(f"[dim]{escape(output)}[/dim]")
        else:
            output_widget.update("[dim](no output)[/dim]")
        if exit_code != 0 and exit_code != -1:
            self._conversation.append_info(f"Exit code: {exit_code}")

        # Store for AI context
        self._shell_context.append(f"$ {cmd}\n{output}")

    async def on_key(self, event) -> None:
        try:
            input_widget = self.query_one("#user-input", PromptInput)
        except NoMatches:
            return
        if not input_widget.has_focus:
            return

        # ── Command/shell mode: backspace on empty input exits mode ──
        if event.key == "backspace" and not input_widget.text:
            if self._command_mode:
                event.prevent_default()
                event.stop()
                self._exit_command_mode()
                return
            if self._shell_mode:
                event.prevent_default()
                event.stop()
                self._exit_shell_mode()
                return

        # ── File picker navigation (takes priority) ─────────────
        try:
            picker = self.query_one(FilePickerDropdown)
        except NoMatches:
            picker = None

        if picker and picker.has_matches:
            if event.key == "up":
                event.prevent_default()
                event.stop()
                picker.move_up()
                return
            if event.key == "down":
                event.prevent_default()
                event.stop()
                picker.move_down()
                return
            if event.key in ("tab", "enter"):
                event.prevent_default()
                event.stop()
                selected = picker.get_selected()
                if selected:
                    self._insert_file_mention(selected)
                self._hide_file_picker()
                return
            if event.key == "escape":
                event.prevent_default()
                event.stop()
                self._hide_file_picker()
                return

        # ── Input history navigation ─────────────────────────────
        if event.key == "up" and self._input_handler and input_widget.cursor_location[0] == 0:
            entry = self._input_handler.on_up(input_widget.text)
            if entry is not None:
                event.prevent_default()
                input_widget.clear()
                input_widget.insert(entry)
                return

        if event.key == "down" and self._input_handler:
            # Only history-navigate when cursor is on the last line
            last_line = input_widget.text.count("\n")
            if input_widget.cursor_location[0] >= last_line:
                entry = self._input_handler.on_down()
                if entry is not None:
                    event.prevent_default()
                    input_widget.clear()
                    input_widget.insert(entry)
                    return

    # ── Command result rendering ──────────────────────────────────

    def render_command_result(self, result: CommandResult) -> None:
        if result.action == "quit":
            self.exit()
        elif result.action == "clear":
            self._sessions.clear()
            self._conversation.append_info("Conversation cleared.")
        elif result.action == "sessions":
            asyncio.create_task(self._sessions.show_picker())
        elif result.action == "model":
            self._show_model_picker()
        elif result.action == "login":
            self._show_login()
        elif result.action == "logout":
            if hasattr(self, "_backend"):
                status = self._backend.clear_cloud_credentials()
                self._status.set_cloud_status(status.cloud_connected, status.cloud_org)
            else:
                self._status.set_cloud_status(False)
            self._status.update_status_bar()
            self._conversation.append_info(result.content)
            return
        elif result.action == "help":
            self._show_help_panel()
        elif result.action == "mcp":
            asyncio.create_task(self._show_mcp_panel())
        elif result.action == "agents":
            asyncio.create_task(self._show_agents_panel())
        elif result.action == "skills":
            asyncio.create_task(self._show_skills_panel())
        elif result.action == "knowledge":
            asyncio.create_task(self._show_knowledge_panel())
        elif result.action == "codeindex":
            asyncio.create_task(self._show_codeindex_panel())
        elif result.action == "loop":
            asyncio.create_task(self._show_loop_panel())
        elif result.action == "hooks":
            asyncio.create_task(self._show_hooks_panel())
        elif result.action == "plugins":
            asyncio.create_task(self._show_plugins_panel())
        elif result.action == "schedule":
            asyncio.create_task(self.action_toggle_tasks())
        elif result.action == "run_prompt":
            # Feed the prompt directly into the run loop, bypassing
            # ``process_message``. We skip ``process_message`` because
            # its cancel-on-non-/loop guard would kill an active
            # ``/loop`` we just configured — the loop body itself
            # doesn't start with ``/loop``. Loop iterations 2+
            # already bypass via ``_check_loop_continuation``; iteration
            # 1 (driven by this ``run_prompt`` dispatch) needs the
            # same treatment. Skill-fired prompts are also internal
            # work, not user input, so the same bypass is correct.
            # ``display_content`` is the unwrapped prompt for chat
            # rendering — the loop slash command sets it so the
            # user sees the bare prompt rather than the
            # ``<loop-iteration>`` wrapper.
            display = getattr(result, "display_content", "") or None
            asyncio.create_task(self._controller._run(result.content, display=display))
        elif result.action == "compact":
            self._status.reset()
            self._status.update_context_usage()
            self._status.update_status_bar()
            self._conversation.append_info(result.content or "Context compacted.")
        elif result.kind == "markdown":
            self._conversation.append_markdown(result.content)
        elif result.kind == "info":
            self._conversation.append_info(result.content)
        elif result.kind == "error":
            self._conversation.append_error(result.content)

    # ── Session picker events ─────────────────────────────────────

    @on(SessionPickerWidget.Selected)
    async def _on_session_selected(self, event: SessionPickerWidget.Selected) -> None:
        await self._sessions.switch_to(event.session_id)

    @on(SessionPickerWidget.Cancelled)
    def _on_session_cancelled(self, _event: SessionPickerWidget.Cancelled) -> None:
        self.query_one("#user-input", PromptInput).focus()

    # ── Model picker ────────────────────────────────────────────────

    def _show_model_picker(self) -> None:
        # Show models that have credentials: explicit API key, env var,
        # key command, or Ember Cloud auth (for models hosted on ignite-ember.sh)
        from ember_code.core.auth.credentials import CloudCredentials
        from ember_code.core.config.cloud_models import fetch_cloud_models, merge_into_registry

        cloud_token = CloudCredentials(self.settings.auth.credentials_file).access_token

        # Refresh the cloud catalogue on open. Synchronous + bounded
        # (3s) — opening the picker shouldn't hang the TUI even on a
        # flaky network. ``merge_into_registry`` is no-op-on-duplicate
        # so user-edited entries survive. The Session backend does the
        # same refresh independently on its side; doing it here too
        # keeps the TUI display fresh without an extra RPC round-trip.
        if cloud_token:
            cloud_entries = fetch_cloud_models(self.settings.api_url, cloud_token)
            if cloud_entries:
                merge_into_registry(
                    self.settings.models.registry, cloud_entries, self.settings.api_url
                )

        models = sorted(
            name
            for name, cfg in self.settings.models.registry.items()
            if (cfg.get("api_key") == "cloud_token" and cloud_token)
            or (cfg.get("api_key") and cfg.get("api_key") != "cloud_token")
            or cfg.get("api_key_env")
            or cfg.get("api_key_cmd")
        )
        if not models:
            self._conversation.append_error("No models configured with API keys.")
            return
        current = self.settings.models.default
        picker = ModelPickerWidget(models=models, current_model=current)
        self.mount(picker)
        picker.focus()

    @on(ModelPickerWidget.Selected)
    def _on_model_selected(self, event: ModelPickerWidget.Selected) -> None:
        if hasattr(self, "_backend"):
            self._backend.switch_model(event.model_name)
        self.settings.models.default = event.model_name
        self._status.update_status_bar()
        self._conversation.append_info(f"Switched to model: {event.model_name}")
        self.query_one("#user-input", PromptInput).focus()

    @on(ModelPickerWidget.Cancelled)
    def _on_model_cancelled(self, _event: ModelPickerWidget.Cancelled) -> None:
        self.query_one("#user-input", PromptInput).focus()

    # ── Login ────────────────────────────────────────────────────────

    def _show_login(self) -> None:
        # Remove any existing login widget
        try:
            old = self.query_one(LoginWidget)
            old.cancel()
        except NoMatches:
            pass
        widget = LoginWidget(backend=self._backend)
        self.mount(widget)
        widget.focus()
        # Tell BE to start the login flow
        asyncio.create_task(self._backend.start_login())

    @on(LoginWidget.LoggedIn)
    def _on_logged_in(self, event: LoginWidget.LoggedIn) -> None:
        # Reload cloud credentials via backend
        if hasattr(self, "_backend"):
            status = self._backend.reload_cloud_credentials()
            self._status.set_cloud_status(status.cloud_connected, status.cloud_org)
            self._status.update_status_bar()

        self._conversation.append_info(f"Logged in as {event.email}")
        self.query_one("#user-input", PromptInput).focus()

    @on(LoginWidget.Cancelled)
    def _on_login_cancelled(self, _event: LoginWidget.Cancelled) -> None:
        self.query_one("#user-input", PromptInput).focus()

    def _on_login_status_push(self, payload: dict) -> None:
        """Handle login_status push — forward to LoginWidget if mounted."""
        try:
            widget = self.query_one(LoginWidget)
            widget.update_status(payload.get("text", ""))
        except NoMatches:
            pass

    def _on_login_result_push(self, payload: dict) -> None:
        """Handle login_result push — forward to LoginWidget if mounted."""
        try:
            widget = self.query_one(LoginWidget)
            widget.show_result(payload.get("success", False), payload.get("result", ""))
        except NoMatches:
            pass

    # ── MCP panel ───────────────────────────────────────────────────

    def _show_help_panel(self) -> None:
        """Mount the interactive help panel."""
        panel = HelpPanelWidget()
        self.mount(panel)
        panel.focus()

    @on(HelpPanelWidget.PanelClosed)
    def _on_help_panel_closed(self, _event: HelpPanelWidget.PanelClosed) -> None:
        self.query_one("#user-input", PromptInput).focus()

    async def _show_mcp_panel(self) -> None:
        """Gather MCP server info and mount the panel."""
        servers = await self._build_mcp_server_list()
        panel = MCPPanelWidget(servers=servers)
        self.mount(panel)
        panel.focus()

    async def _build_mcp_server_list(self) -> list[MCPServerInfo]:
        servers: list[MCPServerInfo] = []
        details = (
            await self._backend._rpc(RpcMethod.GET_MCP_SERVER_DETAILS)
            if hasattr(self._backend, "_rpc")
            else self._backend.get_mcp_server_details()
        )
        for info in details or []:
            servers.append(
                MCPServerInfo(
                    name=info["name"],
                    connected=info["connected"],
                    transport=info["transport"],
                    tool_names=info["tool_names"],
                    tool_descriptions=info["tool_descriptions"],
                    error=info["error"],
                    policy_blocked=info["policy_blocked"],
                )
            )
        return servers

    @on(MCPPanelWidget.ServerToggleRequested)
    async def _on_mcp_toggle(self, event: MCPPanelWidget.ServerToggleRequested) -> None:
        asyncio.create_task(self._toggle_mcp(event.name, event.enable))

    async def _toggle_mcp(self, name: str, enable: bool) -> None:
        """Toggle MCP server in background — doesn't block the TUI."""
        if enable:
            self._conversation.append_info(f"MCP '{name}': connecting...")
            try:
                result = await self._backend.mcp_connect(name)
                self._conversation.append_info(
                    result.text if hasattr(result, "text") else str(result)
                )
            except Exception as exc:
                self._conversation.append_info(f"MCP '{name}': failed: {exc}")
        else:
            try:
                result = await self._backend.mcp_disconnect(name)
                self._conversation.append_info(
                    result.text if hasattr(result, "text") else str(result)
                )
            except Exception as exc:
                logger.debug("MCP disconnect error: %s", exc)
        # Refresh status and panel
        try:
            statuses = (
                await self._backend._rpc(RpcMethod.GET_MCP_STATUS)
                if hasattr(self._backend, "_rpc")
                else self._backend.get_mcp_status()
            )
            for sname, connected in statuses or []:
                self._status.set_ide_status(sname, connected)
            panel = self.query_one(MCPPanelWidget)
            panel.refresh_servers(await self._build_mcp_server_list())
        except Exception:
            pass

    @on(MCPPanelWidget.PanelClosed)
    def _on_mcp_panel_closed(self, _event: MCPPanelWidget.PanelClosed) -> None:
        self.query_one("#user-input", PromptInput).focus()

    # ── Agents panel ──────────────────────────────────────────────

    async def _show_agents_panel(self) -> None:
        """Fetch agent details and mount the panel."""
        agents = await self._build_agent_list()
        panel = AgentsPanelWidget(agents=agents)
        self.mount(panel)
        panel.focus()

    async def _build_agent_list(self) -> list[AgentInfo]:
        details = await self._backend.get_agent_details()
        return [AgentInfo(**d) for d in details]

    async def _refresh_agents_panel(self) -> None:
        try:
            panel = self.query_one(AgentsPanelWidget)
        except Exception:
            return
        panel.refresh_agents(await self._build_agent_list())

    @on(AgentsPanelWidget.PromoteRequested)
    async def _on_agent_promote(
        self,
        event: AgentsPanelWidget.PromoteRequested,
    ) -> None:
        result = await self._backend.promote_ephemeral_agent(event.name)
        self._conversation.append_info(result.text)
        await self._refresh_agents_panel()

    @on(AgentsPanelWidget.DiscardRequested)
    async def _on_agent_discard(
        self,
        event: AgentsPanelWidget.DiscardRequested,
    ) -> None:
        result = await self._backend.discard_ephemeral_agent(event.name)
        self._conversation.append_info(result.text)
        await self._refresh_agents_panel()

    @on(AgentsPanelWidget.PanelClosed)
    def _on_agents_panel_closed(
        self,
        _event: AgentsPanelWidget.PanelClosed,
    ) -> None:
        self.query_one("#user-input", PromptInput).focus()

    # ── Skills panel ──────────────────────────────────────────────

    async def _show_skills_panel(self) -> None:
        skills = await self._build_skill_list()
        panel = SkillsPanelWidget(skills=skills)
        self.mount(panel)
        panel.focus()

    async def _build_skill_list(self) -> list[SkillInfo]:
        details = await self._backend.get_skill_details()
        return [SkillInfo(**d) for d in details]

    @on(SkillsPanelWidget.RunRequested)
    async def _on_skill_run(
        self,
        event: SkillsPanelWidget.RunRequested,
    ) -> None:
        # Close the panel before firing the skill so its output streams
        # into the conversation without being visually shadowed.
        try:
            panel = self.query_one(SkillsPanelWidget)
            panel.remove()
        except Exception:
            pass
        # Route through the controller so the skill renders the same
        # way a typed ``/skill-name`` slash command would.
        await self._controller.process_message(f"/{event.name}")

    @on(SkillsPanelWidget.PanelClosed)
    def _on_skills_panel_closed(
        self,
        _event: SkillsPanelWidget.PanelClosed,
    ) -> None:
        self.query_one("#user-input", PromptInput).focus()

    # ── Knowledge panel ──────────────────────────────────────────

    async def _show_knowledge_panel(self) -> None:
        status_dict = await self._backend.get_knowledge_status()
        status = KnowledgeStatusInfo(**status_dict)
        panel = KnowledgePanelWidget(status=status)
        self.mount(panel)
        # Focus the search input so the user can type immediately —
        # the panel IS the search UI; opening it puts the cursor where
        # the next keystroke is meaningful.
        try:
            panel.query_one("#kb-input").focus()
        except Exception:
            panel.focus()

    @on(KnowledgePanelWidget.SearchRequested)
    async def _on_knowledge_search(
        self,
        event: KnowledgePanelWidget.SearchRequested,
    ) -> None:
        try:
            panel = self.query_one(KnowledgePanelWidget)
        except Exception:
            return
        # Flip the status line to "Searching…" so the panel doesn't
        # look frozen during the embed+ANN round-trip. try/finally so
        # an RPC failure still restores the static status.
        preview = event.query if len(event.query) <= 40 else event.query[:40] + "…"
        panel.set_busy(f"Searching for '{preview}'…")
        try:
            raw = await self._backend.knowledge_search(event.query)
            hits = [KnowledgeSearchHit(**r) for r in raw]
            panel.set_results(hits)
        finally:
            panel.set_busy(None)

    @on(KnowledgePanelWidget.AddRequested)
    async def _on_knowledge_add(
        self,
        event: KnowledgePanelWidget.AddRequested,
    ) -> None:
        try:
            panel = self.query_one(KnowledgePanelWidget)
        except Exception:
            panel = None

        # Ingest can take seconds (URL fetch + chunk + embed). Flip the
        # status line so the user sees activity. URLs and long paths
        # are trimmed in the label.
        preview = event.source if len(event.source) <= 50 else event.source[:50] + "…"
        if panel is not None:
            panel.set_busy(f"Ingesting {preview}…")
        try:
            result = await self._backend.knowledge_add(event.source)
            self._conversation.append_info(result.text)
            # Refresh status header — doc count likely changed.
            if panel is not None:
                try:
                    status_dict = await self._backend.get_knowledge_status()
                    panel.set_status(KnowledgeStatusInfo(**status_dict))
                    # Clear the input for the next add.
                    from textual.widgets import Input as _Input

                    panel.query_one("#kb-input", _Input).value = ""
                except Exception:
                    pass
        finally:
            if panel is not None:
                panel.set_busy(None)

    @on(KnowledgePanelWidget.PanelClosed)
    def _on_knowledge_panel_closed(
        self,
        _event: KnowledgePanelWidget.PanelClosed,
    ) -> None:
        self.query_one("#user-input", PromptInput).focus()

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
        """Open the CodeIndex panel. One status RPC populates the
        header; the 2s background poll keeps the indexed-state /
        sync-% display fresh while the panel is mounted. There is
        no implicit search RPC on open — the panel is a status
        display, not a query surface."""
        status_dict = await self._backend.codeindex_status()
        status = CodeIndexStatusInfo(**status_dict)
        panel = CodeIndexPanelWidget(status=status)
        self.mount(panel)
        # The widget self-focuses in ``on_mount`` — it has no
        # focusable children (no Input), so a parent-side
        # ``panel.focus()`` here would race with the async mount.
        # Start the live status poll. Stored on ``self`` so the
        # ``PanelClosed`` handler can stop it — leaking the interval
        # would keep pinging the backend after the panel disappears.
        self._codeindex_status_poll = self.set_interval(
            self._CODEINDEX_STATUS_POLL_SECONDS,
            self._poll_codeindex_status,
        )

    async def _poll_codeindex_status(self) -> None:
        """Live-refresh the panel header.

        Skipped when a busy indicator is up — overwriting the busy
        label mid-RPC would erase the spinner the user is watching.
        Also skipped when the panel has been unmounted (race with
        ``PanelClosed``).
        """
        try:
            panel = self.query_one(CodeIndexPanelWidget)
        except Exception:
            return
        if panel._busy_label:
            return
        try:
            status_dict = await self._backend.codeindex_status()
        except Exception:
            # Transport hiccup mid-poll shouldn't kill the interval —
            # next tick will retry. Logged at debug; not surfaced to
            # the user since this is a background refresh.
            logger.debug("codeindex status poll failed", exc_info=True)
            return
        status = CodeIndexStatusInfo(**status_dict)
        panel.set_status(status)
        # While the panel is open the panel-poll is tighter than
        # the always-on status-bar poll (2s vs 5s) — feed the same
        # snapshot through so the badge updates at the panel's
        # cadence instead of waiting on its own slower tick.
        self._status.set_codeindex_status(status)

    @on(CodeIndexPanelWidget.SyncRequested)
    async def _on_codeindex_sync(
        self,
        _event: CodeIndexPanelWidget.SyncRequested,
    ) -> None:
        try:
            panel = self.query_one(CodeIndexPanelWidget)
        except Exception:
            return
        panel.set_busy("Syncing changeset…")
        try:
            result = await self._backend.codeindex_sync(None)
            # Surface outcome to the conversation so the user has a
            # durable log line of every sync — the panel header
            # refresh below only reflects the new state.
            if result.get("link_start_url"):
                import webbrowser as _wb

                _wb.open(result["link_start_url"])
                self._conversation.append_info(
                    f"CodeIndex needs setup. Opened {result['link_start_url']} in your browser. "
                    "Re-run sync after finishing the install."
                )
            elif result.get("error"):
                self._conversation.append_error(f"Sync failed: {result['error']}")
            elif result.get("skipped"):
                self._conversation.append_info(
                    f"Sync skipped: {result.get('reason', '')}".rstrip(": ")
                )
            else:
                sha = (result.get("commit_sha") or "")[:8]
                self._conversation.append_info(
                    f"Synced {sha}: {result.get('items_upserted', 0)} upserts, "
                    f"{result.get('items_deleted', 0)} deletes, "
                    f"{result.get('references_upserted', 0)} refs."
                )
            # Refresh header — head + commit count likely moved.
            status_dict = await self._backend.codeindex_status()
            status = CodeIndexStatusInfo(**status_dict)
            panel.set_status(status)
            # Push the same snapshot through to the always-on
            # status-bar slot so the user doesn't wait up to 5s for
            # the next background tick to reflect the new state.
            self._status.set_codeindex_status(status)
        finally:
            panel.set_busy(None)

    @on(CodeIndexPanelWidget.CleanRequested)
    async def _on_codeindex_clean(
        self,
        _event: CodeIndexPanelWidget.CleanRequested,
    ) -> None:
        try:
            panel = self.query_one(CodeIndexPanelWidget)
        except Exception:
            return
        panel.set_busy("Cleaning…")
        try:
            result = await self._backend.codeindex_clean()
            dropped = result.get("dropped") or []
            if dropped:
                self._conversation.append_info(
                    f"Dropped {len(dropped)} commit(s): {', '.join(dropped)}"
                )
            else:
                self._conversation.append_info("Nothing to clean.")
            status_dict = await self._backend.codeindex_status()
            status = CodeIndexStatusInfo(**status_dict)
            panel.set_status(status)
            self._status.set_codeindex_status(status)
        finally:
            panel.set_busy(None)

    @on(CodeIndexPanelWidget.InstallRequested)
    async def _on_codeindex_install(
        self,
        _event: CodeIndexPanelWidget.InstallRequested,
    ) -> None:
        """Open the Ember portal's repositories page in the
        browser.

        The portal page is the canonical entry point for adding a
        repo to CodeIndex — its ``Add repository`` button drives
        the GitHub-App install flow. No status refresh after the
        open; the 2s background poll picks up any state change
        once the user finishes the portal flow.
        """
        try:
            panel = self.query_one(CodeIndexPanelWidget)
        except Exception:
            return
        result = await self._backend.codeindex_install()
        url = result.get("install_url") or ""
        if not url:
            self._conversation.append_error("No portal URL available.")
            return
        import webbrowser as _wb

        _wb.open(url)
        self._conversation.append_info(f"Opening {url}")
        # Best-effort refresh of the header — the poll would catch
        # this in ~2s anyway, but doing it now keeps the install
        # state column from looking stale right after the click.
        try:
            status_dict = await self._backend.codeindex_status()
            status = CodeIndexStatusInfo(**status_dict)
            panel.set_status(status)
            self._status.set_codeindex_status(status)
        except Exception:
            pass

    @on(CodeIndexPanelWidget.PanelClosed)
    def _on_codeindex_panel_closed(
        self,
        _event: CodeIndexPanelWidget.PanelClosed,
    ) -> None:
        # Stop the background status poll started in ``_show_codeindex_panel``
        # — otherwise it'd keep firing ``codeindex_status`` RPCs against a
        # panel that's no longer mounted.
        timer = getattr(self, "_codeindex_status_poll", None)
        if timer is not None:
            with contextlib.suppress(Exception):
                timer.stop()
            self._codeindex_status_poll = None
        self.query_one("#user-input", PromptInput).focus()

    # ── Loop panel ────────────────────────────────────────────────

    # Tighter poll than the CodeIndex panel (2s) — ``/loop`` iterations
    # fire on idle, and a counter that ticks within a second feels
    # live; at 2s the user notices the lag.
    _LOOP_STATUS_POLL_SECONDS = 1.0

    async def _show_loop_panel(self) -> None:
        """Open the Loop panel. One status RPC populates the header;
        the 1s background poll keeps the iteration counter and
        active/inactive state fresh while the panel is mounted."""
        status_dict = await self._backend.loop_status()
        status = LoopStatusInfo(**status_dict)
        panel = LoopPanelWidget(status=status)
        self.mount(panel)
        # Widget self-focuses in ``on_mount`` — see LoopPanelWidget
        # docstring for why a parent-side ``.focus()`` would race
        # with the async mount.
        self._loop_status_poll = self.set_interval(
            self._LOOP_STATUS_POLL_SECONDS,
            self._poll_loop_status,
        )

    async def _poll_loop_status(self) -> None:
        """Live-refresh the panel header.

        Skipped when a busy indicator is up (would overwrite the
        spinner mid-RPC) or when the panel has been unmounted
        (race with ``PanelClosed``). Transport hiccups are
        swallowed silently — next tick will retry.
        """
        try:
            panel = self.query_one(LoopPanelWidget)
        except Exception:
            return
        if panel._busy_label:
            return
        try:
            status_dict = await self._backend.loop_status()
        except Exception:
            logger.debug("loop status poll failed", exc_info=True)
            return
        panel.set_status(LoopStatusInfo(**status_dict))

    @on(LoopPanelWidget.ResumeRequested)
    async def _on_loop_resume(
        self,
        _event: LoopPanelWidget.ResumeRequested,
    ) -> None:
        """Panel ``R`` key — unpause and re-fire the interrupted
        iteration. Mirrors what ``/loop resume`` does from chat:
        flips the paused flag on the backend, then fires
        ``_run(prompt)`` directly to bypass the cancel guard."""
        try:
            panel = self.query_one(LoopPanelWidget)
        except Exception:
            return
        panel.set_busy("Resuming loop…")
        try:
            prompt = await self._backend.loop_resume()
        finally:
            panel.set_busy(None)
        if not prompt:
            self._conversation.append_info("Nothing to resume.")
            return
        # Refresh the header so the badge flips from paused→running
        # before iteration K starts streaming.
        try:
            status_dict = await self._backend.loop_status()
            panel.set_status(LoopStatusInfo(**status_dict))
        except Exception:
            pass
        # Fire the interrupted iteration on the FE directly — same
        # path the run_prompt action dispatch takes, so the cancel
        # guard never sees the prompt and the loop continues
        # naturally after this iteration completes.
        asyncio.create_task(self._controller._run(prompt))

    @on(LoopPanelWidget.CancelRequested)
    async def _on_loop_cancel(
        self,
        _event: LoopPanelWidget.CancelRequested,
    ) -> None:
        try:
            panel = self.query_one(LoopPanelWidget)
        except Exception:
            return
        panel.set_busy("Cancelling loop…")
        try:
            cancelled = await self._backend.cancel_pending_loop()
            if cancelled:
                self._conversation.append_info("Loop cancelled.")
            # Refresh the header — even when nothing was cancelled
            # (race: loop completed between the user pressing X
            # and the RPC reaching the backend), we want the
            # post-cancel state visible.
            status_dict = await self._backend.loop_status()
            panel.set_status(LoopStatusInfo(**status_dict))
        finally:
            panel.set_busy(None)

    # ── Hooks panel ──────────────────────────────────────────────

    async def _show_hooks_panel(self) -> None:
        """Open the hooks panel. One RPC pulls the flat hook list;
        the widget groups by event on the client side."""
        rows = await self._backend.get_hooks_details()
        hooks = [HookInfo(**r) for r in rows]
        panel = HooksPanelWidget(hooks=hooks)
        self.mount(panel)
        # Widget self-focuses in ``on_mount`` so we don't need a
        # parent-side ``.focus()`` (which would race with the
        # async mount).

    @on(HooksPanelWidget.ReloadRequested)
    async def _on_hooks_reload(
        self,
        _event: HooksPanelWidget.ReloadRequested,
    ) -> None:
        try:
            panel = self.query_one(HooksPanelWidget)
        except Exception:
            return
        panel.set_busy("Reloading hooks…")
        try:
            result = await self._backend.reload_hooks()
            self._conversation.append_info(result.text)
            rows = await self._backend.get_hooks_details()
            panel.set_hooks([HookInfo(**r) for r in rows])
        finally:
            panel.set_busy(None)

    @on(HooksPanelWidget.PanelClosed)
    def _on_hooks_panel_closed(
        self,
        _event: HooksPanelWidget.PanelClosed,
    ) -> None:
        self.query_one("#user-input", PromptInput).focus()

    @on(LoopPanelWidget.PanelClosed)
    def _on_loop_panel_closed(
        self,
        _event: LoopPanelWidget.PanelClosed,
    ) -> None:
        # Stop the background status poll — same lifecycle as the
        # CodeIndex panel. Without this the 1s interval keeps
        # firing ``loop_status`` RPCs after the widget is gone.
        timer = getattr(self, "_loop_status_poll", None)
        if timer is not None:
            with contextlib.suppress(Exception):
                timer.stop()
            self._loop_status_poll = None
        self.query_one("#user-input", PromptInput).focus()

    # ── Plugins panel ─────────────────────────────────────────────

    async def _show_plugins_panel(self) -> None:
        """Build initial plugin + marketplace lists, mount the panel.

        Wrapped in ``try/except`` because the parent dispatch fires
        this in an ``asyncio.create_task`` that swallows any error
        — without the wrap a bad catalog row makes ``/plugins`` look
        like nothing happens, which is exactly the bug a stricter
        wire schema caused (the silent ValidationError that hid
        for a session before we found it).
        """
        try:
            installed, marketplaces = await self._build_plugin_state()
        except Exception as e:
            logger.exception("Failed to build plugin panel state")
            self._conversation.append_error(f"Could not open the plugins panel: {e}")
            return
        panel = PluginsPanelWidget(installed=installed, marketplaces=marketplaces)
        self.mount(panel)
        panel.focus()

    async def _build_plugin_state(
        self,
    ) -> tuple[list[PluginInfo], list[MarketplaceInfo]]:
        details = await self._backend.get_plugin_details()
        installed = [PluginInfo(**d) for d in details]
        mkt_payload = await self._backend.get_marketplaces()
        marketplaces = [
            MarketplaceInfo(
                name=m["name"],
                url=m["url"],
                last_fetched=m["last_fetched"],
                plugins=[MarketplacePluginInfo(**p) for p in m["plugins"]],
            )
            for m in mkt_payload
        ]
        return installed, marketplaces

    async def _refresh_plugins_panel(self) -> None:
        try:
            panel = self.query_one(PluginsPanelWidget)
        except Exception:
            return
        installed, marketplaces = await self._build_plugin_state()
        panel.refresh_data(installed=installed, marketplaces=marketplaces)

    @on(PluginsPanelWidget.PluginToggleRequested)
    async def _on_plugin_toggle(
        self,
        event: PluginsPanelWidget.PluginToggleRequested,
    ) -> None:
        result = await self._backend.set_plugin_enabled(
            event.name,
            event.enable,
        )
        self._conversation.append_info(result.text)
        await self._refresh_plugins_panel()

    @on(PluginsPanelWidget.PluginInstallRequested)
    async def _on_plugin_install(
        self,
        event: PluginsPanelWidget.PluginInstallRequested,
    ) -> None:
        self._conversation.append_info(f"Installing {event.ref}…")
        result = await self._backend.install_plugin(
            event.ref,
            event.install_ref,
        )
        self._conversation.append_info(result.text)
        await self._refresh_plugins_panel()

    @on(PluginsPanelWidget.PluginUpdateRequested)
    async def _on_plugin_update(
        self,
        event: PluginsPanelWidget.PluginUpdateRequested,
    ) -> None:
        self._conversation.append_info(f"Updating {event.name}…")
        result = await self._backend.update_plugin(event.name)
        self._conversation.append_info(result.text)
        await self._refresh_plugins_panel()

    @on(PluginsPanelWidget.PluginRemoveRequested)
    async def _on_plugin_remove(
        self,
        event: PluginsPanelWidget.PluginRemoveRequested,
    ) -> None:
        result = await self._backend.remove_plugin(event.name)
        self._conversation.append_info(result.text)
        await self._refresh_plugins_panel()

    @on(PluginsPanelWidget.MarketplaceRefreshRequested)
    async def _on_marketplace_refresh(
        self,
        _event: PluginsPanelWidget.MarketplaceRefreshRequested,
    ) -> None:
        result = await self._backend.refresh_marketplaces()
        self._conversation.append_info(result.text)
        await self._refresh_plugins_panel()

    @on(PluginsPanelWidget.PanelClosed)
    def _on_plugins_panel_closed(
        self,
        _event: PluginsPanelWidget.PanelClosed,
    ) -> None:
        self.query_one("#user-input", PromptInput).focus()

    # ── Queue panel events ─────────────────────────────────────────

    @on(QueuePanel.ItemDeleted)
    def _on_queue_item_deleted(self, event: QueuePanel.ItemDeleted) -> None:
        removed = self._controller.dequeue_at(event.index)
        if removed:
            short = removed if len(removed) <= 40 else removed[:37] + "..."
            self._conversation.append_info(f"Removed from queue: {short}")

    @on(QueuePanel.ItemEditRequested)
    def _on_queue_item_edit(self, event: QueuePanel.ItemEditRequested) -> None:
        # Remove the item from the queue and put its text into the input box
        self._controller.dequeue_at(event.index)
        input_widget = self.query_one("#user-input", PromptInput)
        input_widget.clear()
        input_widget.insert(event.text)
        input_widget.focus()

    @on(QueuePanel.PanelClosed)
    def _on_queue_panel_closed(self, _event: QueuePanel.PanelClosed) -> None:
        with contextlib.suppress(NoMatches):
            self.query_one("#queue-panel", QueuePanel).add_class("-hidden")
        self.query_one("#user-input", PromptInput).focus()

    # ── Task panel events ──────────────────────────────────────────

    @on(TaskPanel.TaskCancelled)
    async def _on_task_cancelled(self, event: TaskPanel.TaskCancelled) -> None:
        result = await self._backend.cancel_scheduled_task(event.task_id)
        self._conversation.append_info(result.text)
        await self._refresh_task_panel()

    @on(TaskPanel.PanelClosed)
    def _on_task_panel_closed(self, _event: TaskPanel.PanelClosed) -> None:
        with contextlib.suppress(NoMatches):
            self.query_one("#task-panel", TaskPanel).add_class("-hidden")
        if hasattr(self, "_task_refresh_timer") and self._task_refresh_timer:
            self._task_refresh_timer.stop()
            self._task_refresh_timer = None
        self.query_one("#user-input", PromptInput).focus()

    # ── Scheduler ────────────────────────────────────────────────

    def _start_scheduler(self) -> None:
        """Start the background scheduler via backend."""
        self._scheduler_runner = self._backend.start_scheduler(
            on_task_started=self._on_scheduled_task_started,
            on_task_completed=self._on_scheduled_task_completed,
        )

    async def _execute_scheduled_task(self, description: str) -> str:
        """Execute a scheduled task through the backend."""
        try:
            return await self._backend.execute_scheduled_task(description)
        except Exception as exc:
            return f"Error: {exc}"

    def _on_scheduled_task_started(self, task_id: str, description: str) -> None:
        short = description[:50] + ("..." if len(description) > 50 else "")
        self._conversation.append_info(f"⚡ Running scheduled task `{task_id}`: {short}")
        self.notify(f"Task {task_id} started: {short}", title="Scheduler", timeout=5)
        asyncio.create_task(self._refresh_task_panel())

    def _on_scheduled_task_completed(self, task_id: str, description: str, success: bool) -> None:
        short = description[:50] + ("..." if len(description) > 50 else "")
        if success:
            self._conversation.append(
                Static(
                    f"[green]✓[/green] Task `{task_id}` completed: {short}"
                    f"  [dim]→ /schedule show {task_id}[/dim]",
                    classes="task-event",
                )
            )
            self.notify(
                f"Task {task_id} completed: {short}",
                title="Scheduler",
                severity="information",
                timeout=8,
            )
        else:
            self._conversation.append(
                Static(
                    f"[red]✗[/red] Task `{task_id}` failed: {short}"
                    f"  [dim]→ /schedule show {task_id}[/dim]",
                    classes="task-event",
                )
            )
            self.notify(
                f"Task {task_id} failed: {short}",
                title="Scheduler",
                severity="error",
                timeout=10,
            )
        asyncio.create_task(self._refresh_task_panel())

    async def _refresh_task_panel(self) -> None:
        """Refresh the task panel with current tasks via backend."""
        try:
            tasks = await self._backend.get_scheduled_tasks(include_done=True)
            panel = self.query_one("#task-panel", TaskPanel)
            panel.refresh_tasks(tasks)
        except Exception:
            pass

    # ── Actions (Textual keybindings) ─────────────────────────────

    def action_clear_screen(self) -> None:
        self._sessions.clear()

    def action_toggle_expand_all(self) -> None:
        container = self._conversation.container
        widgets = container.query(MessageWidget)
        long_widgets = [w for w in widgets if w.is_long]
        if not long_widgets:
            return
        any_collapsed = any(not w.expanded for w in long_widgets)
        for w in long_widgets:
            w.set_expanded(any_collapsed)

    def action_toggle_queue(self) -> None:
        """Toggle queue panel visibility and focus."""
        try:
            panel = self.query_one("#queue-panel", QueuePanel)
            if panel.has_class("-hidden") and self._controller.queue_size > 0:
                panel.remove_class("-hidden")
                panel.focus()
            else:
                panel.add_class("-hidden")
                self.query_one("#user-input", PromptInput).focus()
        except Exception:
            pass

    async def action_toggle_tasks(self) -> None:
        """Toggle task panel visibility."""
        try:
            panel = self.query_one("#task-panel", TaskPanel)
            if panel.has_class("-hidden"):
                await self._refresh_task_panel()
                panel.remove_class("-hidden")
                panel.focus()
                # Start auto-refresh while panel is open
                if not hasattr(self, "_task_refresh_timer") or self._task_refresh_timer is None:
                    self._task_refresh_timer = self.set_interval(1.0, self._auto_refresh_tasks)
            else:
                panel.add_class("-hidden")
                if hasattr(self, "_task_refresh_timer") and self._task_refresh_timer:
                    self._task_refresh_timer.stop()
                    self._task_refresh_timer = None
                self.query_one("#user-input", PromptInput).focus()
        except Exception:
            pass

    async def _auto_refresh_tasks(self) -> None:
        """Periodic refresh of the task panel while it's visible."""
        try:
            panel = self.query_one("#task-panel", TaskPanel)
            if panel.has_class("-hidden"):
                if hasattr(self, "_task_refresh_timer") and self._task_refresh_timer:
                    self._task_refresh_timer.stop()
                    self._task_refresh_timer = None
                return
            await self._refresh_task_panel()
        except Exception:
            pass

    def action_toggle_verbose(self) -> None:
        verbose = self._backend.toggle_verbose()
        state = "on" if verbose else "off"
        self._conversation.append_info(f"Verbose mode: {state}")

    async def _check_for_update(self) -> None:
        """Check for a newer CLI version via BE RPC."""
        try:
            result = await self._backend._rpc(RpcMethod.CHECK_FOR_UPDATE)
            logger.debug("Update check result: %s", result)
            if result and result.get("available"):
                bar = self.query_one("#update-bar", UpdateBar)
                bar.show_update(
                    current=result.get("current_version", ""),
                    latest=result.get("latest_version", ""),
                    url=result.get("download_url", ""),
                    pkg_name=result.get("pkg_name", ""),
                )
        except Exception as e:
            logger.debug("Update check error: %s", e)

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
        "/schedule add <task> at <time> — schedule deferred tasks",
        "/mcp — manage MCP server connections",
        "Ctrl+T — toggle the task panel",
    ]

    def _start_tip_rotation(self) -> None:
        import random

        try:
            tip_bar = self.query_one("#tip-bar", TipBar)
            tip_bar.set_tip(random.choice(self._TIPS))
            self.set_interval(30, self._rotate_tip)
        except Exception:
            pass

    def _rotate_tip(self) -> None:
        import random

        try:
            tip_bar = self.query_one("#tip-bar", TipBar)
            tip_bar.set_tip(random.choice(self._TIPS))
        except Exception:
            pass

    async def on_resize(self, event: Resize) -> None:
        """Remove and remount the welcome box so CSS border redraws cleanly."""
        try:
            old_box = self.query_one("#welcome-box", Static)
        except NoMatches:
            return

        await old_box.remove()

        container = self.query_one("#conversation", ScrollableContainer)
        new_box = Static(self._build_welcome_content(), id="welcome-box")
        try:
            caps = self.query_one("#capabilities", Static)
            await container.mount(new_box, before=caps)
        except NoMatches:
            await container.mount(new_box, before=0)

        self.screen.refresh(layout=True)

    def action_cancel(self) -> None:
        import os
        import signal

        # Close any open dialog/panel first
        # Close visible task panel first (always mounted, toggled via -hidden)
        try:
            task_panel = self.query_one("#task-panel", TaskPanel)
            if not task_panel.has_class("-hidden"):
                task_panel.add_class("-hidden")
                with contextlib.suppress(NoMatches):
                    self.query_one("#user-input", PromptInput).focus()
                return
        except NoMatches:
            pass

        _DIALOG_TYPES = (
            LoginWidget,
            HelpPanelWidget,
            ModelPickerWidget,
            SessionPickerWidget,
            MCPPanelWidget,
            AgentsPanelWidget,
            SkillsPanelWidget,
            KnowledgePanelWidget,
            CodeIndexPanelWidget,
            HooksPanelWidget,
            LoopPanelWidget,
            PluginsPanelWidget,
        )
        for widget_cls in _DIALOG_TYPES:
            try:
                widget = self.query_one(widget_cls)
                if isinstance(widget, LoginWidget):
                    widget.cancel()
                else:
                    widget.remove()
                with contextlib.suppress(NoMatches):
                    self.query_one("#user-input", PromptInput).focus()
                return
            except NoMatches:
                continue

        # Kill running inline shell command first
        if self._shell_proc is not None:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(os.getpgid(self._shell_proc.pid), signal.SIGTERM)
            with contextlib.suppress(ProcessLookupError, OSError):
                self._shell_proc.kill()
            self._shell_proc = None
            return

        # Exit command mode
        if self._command_mode:
            self._exit_command_mode()
            return

        # Exit shell mode
        if self._shell_mode:
            self._exit_shell_mode()
            return

        # Cancel AI run
        self._controller.cancel()
