"""Keybinding + action handler bodies for :class:`EmberApp`.

Extracted from ``tui/app.py``. Textual routes actions to
``action_*`` methods and keys to ``on_key`` — those must stay
on the class. Bodies live here as free functions taking
``app: EmberApp`` as arg.

Free functions:

* :func:`on_key` — every-keypress handler. File-picker
  navigation, mode-exit on backspace, input history up/down.
* :func:`render_command_result` — dispatch a
  :class:`CommandResult` from the BE to the right FE action
  (open panel, run prompt, refresh status, etc.).
* :func:`action_cancel` — Ctrl+C. Priority order: close open
  panel → kill inline shell → exit command/shell mode → cancel
  AI run.
* :func:`action_clear_screen`, :func:`action_toggle_expand_all`,
  :func:`action_toggle_queue`, :func:`action_toggle_tasks`,
  :func:`auto_refresh_tasks`, :func:`action_toggle_verbose` —
  smaller keybinding-triggered actions.

Rule 2 clean — all imports at module top.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from typing import TYPE_CHECKING

from textual.css.query import NoMatches

from ember_code.frontend.tui.widgets import (
    AgentsPanelWidget,
    CodeIndexPanelWidget,
    FilePickerDropdown,
    HelpPanelWidget,
    HooksPanelWidget,
    KnowledgePanelWidget,
    LoginWidget,
    LoopPanelWidget,
    MCPPanelWidget,
    MessageWidget,
    ModelPickerWidget,
    PluginsPanelWidget,
    PromptInput,
    QueuePanel,
    SessionPickerWidget,
    SkillsPanelWidget,
    TaskPanel,
)
from ember_code.protocol.messages import (
    CommandAction,
    CommandResult,
    CommandResultKind,
)

if TYPE_CHECKING:
    from ember_code.frontend.tui.app import EmberApp

logger = logging.getLogger(__name__)


# Every panel/dialog that ``action_cancel`` may need to close.
# In precedence order: matches show up top-down.
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


async def on_key(app: "EmberApp", event) -> None:
    """Every-keystroke input handler for prompt-adjacent
    keybindings.

    Cached input widget — every keypress used to do a fresh
    ``query_one`` here, which on a long conversation walks the
    whole widget tree. Combined with the picker lookup below
    and the autocomplete lookups in ``on_input_changed``, the
    per-keystroke tree walks compounded into seconds of lag.
    """
    input_widget = app._user_input_widget
    if input_widget is None:
        try:
            input_widget = app.query_one("#user-input", PromptInput)
        except NoMatches:
            return
        app._user_input_widget = input_widget
    if not input_widget.has_focus:
        return

    # ── Command/shell mode: backspace on empty input exits mode ──
    if event.key == "backspace" and not input_widget.text:
        if app._command_mode:
            event.prevent_default()
            event.stop()
            app._exit_command_mode()
            return
        if app._shell_mode:
            event.prevent_default()
            event.stop()
            app._exit_shell_mode()
            return

    # ── File picker navigation (takes priority) ─────────────
    # Same guard as ``on_input_changed``: only walk the tree if
    # we know the picker is mounted.
    picker: FilePickerDropdown | None = None
    if app._file_picker_mounted:
        try:
            picker = app.query_one(FilePickerDropdown)
        except NoMatches:
            app._file_picker_mounted = False

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
                app._insert_file_mention(selected)
            app._hide_file_picker()
            return
        if event.key == "escape":
            event.prevent_default()
            event.stop()
            app._hide_file_picker()
            return

    # ── Input history navigation ─────────────────────────────
    if event.key == "up" and app._input_handler and input_widget.cursor_location[0] == 0:
        entry = app._input_handler.on_up(input_widget.text)
        if entry is not None:
            event.prevent_default()
            input_widget.clear()
            input_widget.insert(entry)
            return

    if event.key == "down" and app._input_handler:
        # Only history-navigate when cursor is on the last line.
        last_line = input_widget.text.count("\n")
        if input_widget.cursor_location[0] >= last_line:
            entry = app._input_handler.on_down()
            if entry is not None:
                event.prevent_default()
                input_widget.clear()
                input_widget.insert(entry)


def render_command_result(app: "EmberApp", result: CommandResult) -> None:
    """Dispatch a :class:`CommandResult` from the BE to the right
    FE action (open panel, run prompt, refresh status, etc.)."""
    action = result.action
    if action == CommandAction.QUIT:
        app.exit()
    elif action == CommandAction.CLEAR:
        app._sessions.clear()
        app._conversation.append_info("Conversation cleared.")
    elif action == CommandAction.SESSIONS:
        asyncio.create_task(app._sessions.show_picker())
    elif action == CommandAction.MODEL:
        app._show_model_picker()
    elif action == CommandAction.MODEL_SWITCHED:
        # ``/model <name>`` direct switch — the BE already
        # rebuilt the team; just refresh the status-bar so the
        # footer model slot matches the chat info line.
        app._status.update_status_bar()
        app._conversation.append_info(result.content)
        return
    elif action == CommandAction.LOGIN:
        app._show_login()
    elif action == CommandAction.LOGOUT:
        if hasattr(app, "_backend"):
            status = app._backend.clear_cloud_credentials()
            app._status.set_cloud_status(status.cloud_connected, status.cloud_org)
        else:
            app._status.set_cloud_status(False)
        app._status.update_status_bar()
        app._conversation.append_info(result.content)
        return
    elif action == CommandAction.HELP:
        app._show_help_panel()
    elif action == CommandAction.MCP:
        asyncio.create_task(app._show_mcp_panel())
    elif action == CommandAction.AGENTS:
        asyncio.create_task(app._show_agents_panel())
    elif action == CommandAction.SKILLS:
        asyncio.create_task(app._show_skills_panel())
    elif action == CommandAction.KNOWLEDGE:
        asyncio.create_task(app._show_knowledge_panel())
    elif action == CommandAction.CODEINDEX:
        asyncio.create_task(app._show_codeindex_panel())
    elif action == CommandAction.LOOP:
        asyncio.create_task(app._show_loop_panel())
    elif action == CommandAction.HOOKS:
        asyncio.create_task(app._show_hooks_panel())
    elif action == CommandAction.PLUGINS:
        asyncio.create_task(app._show_plugins_panel())
    elif action == CommandAction.SCHEDULE:
        asyncio.create_task(app.action_toggle_tasks())
    elif action == CommandAction.RUN_PROMPT:
        # Feed the prompt directly into the run loop, bypassing
        # ``process_message``. We skip ``process_message``
        # because its cancel-on-non-/loop guard would kill an
        # active ``/loop`` we just configured — the loop body
        # itself doesn't start with ``/loop``. Loop iterations
        # 2+ already bypass via ``_check_loop_continuation``;
        # iteration 1 (driven by this ``run_prompt`` dispatch)
        # needs the same treatment. Skill-fired prompts are
        # also internal work, not user input, so the same
        # bypass is correct. ``display_content`` is the
        # unwrapped prompt for chat rendering — the loop slash
        # command sets it so the user sees the bare prompt
        # rather than the ``<loop-iteration>`` wrapper.
        display = getattr(result, "display_content", "") or None
        asyncio.create_task(app._controller._run(result.content, display=display))
    elif action == CommandAction.COMPACT:
        app._status.reset()
        app._status.update_context_usage()
        app._status.update_status_bar()
        app._conversation.append_info(result.content or "Context compacted.")
    elif result.kind == CommandResultKind.MARKDOWN:
        app._conversation.append_markdown(result.content)
    elif result.kind == CommandResultKind.INFO:
        app._conversation.append_info(result.content)
    elif result.kind == CommandResultKind.ERROR:
        app._conversation.append_error(result.content)


def action_clear_screen(app: "EmberApp") -> None:
    """Clear the conversation buffer."""
    app._sessions.clear()


def action_toggle_expand_all(app: "EmberApp") -> None:
    """Expand / collapse every long message widget at once."""
    container = app._conversation.container
    widgets = container.query(MessageWidget)
    long_widgets = [w for w in widgets if w.is_long]
    if not long_widgets:
        return
    any_collapsed = any(not w.expanded for w in long_widgets)
    for w in long_widgets:
        w.set_expanded(any_collapsed)


def action_toggle_queue(app: "EmberApp") -> None:
    """Toggle queue panel visibility and focus."""
    try:
        panel = app.query_one("#queue-panel", QueuePanel)
        if panel.has_class("-hidden") and app._controller.queue_size > 0:
            panel.remove_class("-hidden")
            panel.focus()
        else:
            panel.add_class("-hidden")
            app.query_one("#user-input", PromptInput).focus()
    except Exception:
        pass


async def action_toggle_tasks(app: "EmberApp") -> None:
    """Toggle task panel visibility. Starts / stops the 1s
    auto-refresh interval too."""
    try:
        panel = app.query_one("#task-panel", TaskPanel)
        if panel.has_class("-hidden"):
            await app._refresh_task_panel()
            panel.remove_class("-hidden")
            panel.focus()
            # Start auto-refresh while panel is open.
            if not hasattr(app, "_task_refresh_timer") or app._task_refresh_timer is None:
                app._task_refresh_timer = app.set_interval(1.0, app._auto_refresh_tasks)
        else:
            panel.add_class("-hidden")
            if hasattr(app, "_task_refresh_timer") and app._task_refresh_timer:
                app._task_refresh_timer.stop()
                app._task_refresh_timer = None
            app.query_one("#user-input", PromptInput).focus()
    except Exception:
        pass


async def auto_refresh_tasks(app: "EmberApp") -> None:
    """Periodic refresh of the task panel while it's visible.
    Self-cancelling when the panel is closed."""
    try:
        panel = app.query_one("#task-panel", TaskPanel)
        if panel.has_class("-hidden"):
            if hasattr(app, "_task_refresh_timer") and app._task_refresh_timer:
                app._task_refresh_timer.stop()
                app._task_refresh_timer = None
            return
        await app._refresh_task_panel()
    except Exception:
        pass


def action_toggle_verbose(app: "EmberApp") -> None:
    """Flip the backend's verbose flag and echo the new state
    to the conversation."""
    verbose = app._backend.toggle_verbose()
    state = "on" if verbose else "off"
    app._conversation.append_info(f"Verbose mode: {state}")


def action_cancel(app: "EmberApp") -> None:
    """Ctrl+C — priority-ordered cancel.

    1. Close visible task panel first (always mounted, toggled
       via ``-hidden`` class).
    2. Close any open dialog / panel (see ``_DIALOG_TYPES``).
    3. Kill running inline shell command.
    4. Exit command mode.
    5. Exit shell mode.
    6. Cancel the current AI run.
    """
    # Close visible task panel first.
    try:
        task_panel = app.query_one("#task-panel", TaskPanel)
        if not task_panel.has_class("-hidden"):
            task_panel.add_class("-hidden")
            with contextlib.suppress(NoMatches):
                app.query_one("#user-input", PromptInput).focus()
            return
    except NoMatches:
        pass

    for widget_cls in _DIALOG_TYPES:
        try:
            widget = app.query_one(widget_cls)
            if isinstance(widget, LoginWidget):
                widget.cancel()
            else:
                widget.remove()
            with contextlib.suppress(NoMatches):
                app.query_one("#user-input", PromptInput).focus()
            return
        except NoMatches:
            continue

    # Kill running inline shell command first.
    if app._shell_proc is not None:
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(os.getpgid(app._shell_proc.pid), signal.SIGTERM)
        with contextlib.suppress(ProcessLookupError, OSError):
            app._shell_proc.kill()
        app._shell_proc = None
        return

    # Exit command mode.
    if app._command_mode:
        app._exit_command_mode()
        return

    # Exit shell mode.
    if app._shell_mode:
        app._exit_shell_mode()
        return

    # Cancel AI run.
    app._controller.cancel()
