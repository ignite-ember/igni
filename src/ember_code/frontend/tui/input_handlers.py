"""Prompt input handling — changed / submitted / file mention.

Extracted from ``tui/app.py``. Same pattern as other TUI
handler modules.

Free functions taking ``app: EmberApp`` as first arg:

* :func:`on_input_changed` — every-keystroke handler. Mirrors
  the draft to other views, detects mode toggles (`/`, `!`,
  `$`), handles @file mentions + slash-command autocomplete.
* :func:`mount_autocomplete` — hint bar under the prompt.
* :func:`show_file_picker` / :func:`hide_file_picker` — the
  @-mention dropdown.
* :func:`insert_file_mention` — replace the @query with the
  selected file path.
* :func:`on_input_submitted` — Enter key. Routes to command
  mode, shell mode, or normal message dispatch.

Rule 2 clean — all imports at module top.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widgets import Static

from ember_code.frontend.tui.input_handler import extract_at_mention
from ember_code.frontend.tui.widgets import (
    FilePickerDropdown,
    PromptInput,
)

if TYPE_CHECKING:
    from ember_code.frontend.tui.app import EmberApp

logger = logging.getLogger(__name__)


def on_input_changed(app: "EmberApp", event) -> None:
    """Every-keystroke input handler.

    Fires on every character typed into the prompt. Handles:

    1. Draft mirroring to other views (web tabs on the same BE).
    2. Mode toggles when the input is exactly ``/``, ``!``, or
       ``$``: switch to command mode or shell mode respectively.
    3. ``! foo`` / ``$ foo`` prefix from chat: strip the prefix
       and switch to shell mode with the rest as the initial
       command.
    4. ``@file`` mention detection: show the file picker.
    5. Slash-command autocomplete: mount / update the hint bar
       under the prompt.

    All conditional branches early-return so the "regular
    keystroke in a long conversation" path is cheapest.
    """
    text_area = event.text_area
    text = text_area.text

    # ── Mirroring: broadcast the live draft to other views ───
    # (web tabs attached to the same BE). Throttled inside the
    # client; fire-and-forget. getattr: keystrokes can arrive
    # before the backend finishes starting.
    backend = getattr(app, "_backend", None)
    if backend is not None:
        with contextlib.suppress(Exception):
            backend.notify_typing(text)

    # ── Mode toggles (/, !, $) ─────────────────────────────
    if not app._shell_mode and not app._command_mode and text in ("/", "!", "$"):
        if text == "/":
            app._command_mode = True
            app._update_command_mode_indicator()
        else:
            app._shell_mode = True
            app._update_shell_mode_indicator()
        text_area.clear()
        return
    if (
        not app._shell_mode
        and not app._command_mode
        and (text.startswith("! ") or text.startswith("$ "))
    ):
        app._shell_mode = True
        text_area.clear()
        text_area.insert(text[2:])
        app._update_shell_mode_indicator()
        return

    # ── @file mention detection ──────────────────────────────
    row, col = text_area.cursor_location
    mention_query = extract_at_mention(row, col, text_area.document.get_line)
    if mention_query is not None and app._input_handler:
        matches = app._input_handler.get_file_completions(mention_query)
        show_file_picker(app, matches)
        # Hide slash autocomplete if it happens to be visible.
        if app._autocomplete_mounted:
            with contextlib.suppress(NoMatches):
                app.query_one("#autocomplete", Static).display = False
        return

    # Hide file picker only when it's actually mounted — saves
    # the query_one tree walk on every regular keystroke.
    if app._file_picker_mounted:
        hide_file_picker(app)

    # ── Slash command autocomplete ───────────────────────────
    # Same guard: only walk the tree if the widget is mounted.
    widget: Static | None = None
    if app._autocomplete_mounted:
        try:
            widget = app.query_one("#autocomplete", Static)
        except NoMatches:
            widget = None
            app._autocomplete_mounted = False

    if app._input_handler:
        # In command mode, text has no ``/`` prefix — add it for
        # autocomplete.
        completion_text = f"/{text}" if app._command_mode else text
        matches = app._input_handler.get_completions(completion_text)
        if app._command_mode:
            # Strip ``/`` from suggestions since indicator already
            # shows it.
            matches = [m.lstrip("/") for m in matches]
        if matches:
            hint = "  ".join(matches)
            if widget:
                widget.update(f"[dim]{hint}[/dim]")
                widget.display = True
            else:
                mount_autocomplete(app, hint)
            return

    if widget:
        widget.display = False


def mount_autocomplete(app: "EmberApp", hint: str) -> None:
    """Mount the slash-command hint bar under the prompt."""
    try:
        area = app.query_one("#footer", Vertical)
        area.mount(Static(f"[dim]{hint}[/dim]", id="autocomplete"))
        app._autocomplete_mounted = True
    except Exception:
        pass


def show_file_picker(app: "EmberApp", matches: list[str]) -> None:
    """Show or update the file picker dropdown."""
    input_widget = app.query_one("#user-input", PromptInput)
    input_widget.suppress_submit = True
    if app._file_picker_mounted:
        try:
            picker = app.query_one(FilePickerDropdown)
            picker.update_matches(matches)
            return
        except NoMatches:
            app._file_picker_mounted = False
    picker = FilePickerDropdown(matches)
    try:
        footer = app.query_one("#footer", Vertical)
        prompt_row = app.query_one("#prompt-row")
        footer.mount(picker, before=prompt_row)
        app._file_picker_mounted = True
    except Exception:
        pass


def hide_file_picker(app: "EmberApp") -> None:
    """Remove the file picker dropdown if present.

    Guarded by ``_file_picker_mounted`` at the only hot caller
    (:func:`on_input_changed`), so the bare ``query_one`` calls
    here only fire when the picker was actually mounted —
    never on a regular keystroke in a long conversation.
    """
    with contextlib.suppress(NoMatches):
        app.query_one(FilePickerDropdown).remove()
    with contextlib.suppress(NoMatches):
        app.query_one("#user-input", PromptInput).suppress_submit = False
    app._file_picker_mounted = False


def insert_file_mention(app: "EmberApp", path: str) -> None:
    """Replace the ``@query`` with the selected file path."""
    input_widget = app.query_one("#user-input", PromptInput)
    row, col = input_widget.cursor_location
    line = input_widget.document.get_line(row)

    # Find the ``@`` position by scanning backward.
    at_pos = col - 1
    while at_pos >= 0 and line[at_pos] != "@":
        at_pos -= 1

    if at_pos < 0:
        return

    # Rebuild the full text with the replacement.
    full_text = input_widget.text
    lines = full_text.split("\n")
    old_line = lines[row]
    # Replace from after ``@`` to cursor position with the full
    # path.
    new_line = old_line[: at_pos + 1] + path + " " + old_line[col:]
    lines[row] = new_line
    new_text = "\n".join(lines)

    # Calculate new cursor position (after path + space).
    new_col = at_pos + 1 + len(path) + 1

    input_widget.clear()
    input_widget.insert(new_text)
    input_widget.move_cursor((row, new_col))


async def on_input_submitted(app: "EmberApp", event) -> None:
    """Handle Enter — PromptInput posts Submitted with the text."""
    input_widget = app.query_one("#user-input", PromptInput)
    if app._input_handler:
        submitted = app._input_handler.on_submit(event.text)
        if submitted:
            input_widget.clear()
            with contextlib.suppress(NoMatches):
                app.query_one("#autocomplete", Static).display = False

            # Command mode — prepend ``/`` and exit.
            if app._command_mode:
                submitted = f"/{submitted}"
                app._exit_command_mode()

            # Auto-expand a partial slash command when exactly
            # one built-in or skill matches (e.g. ``/codei`` →
            # ``/codeindex``).
            if submitted.startswith("/") and not submitted.startswith("//"):
                submitted = app._input_handler.expand_unique_command(submitted)

            # Shell mode — run as command, stay in shell mode.
            if app._shell_mode:
                app._shell_task = asyncio.create_task(app._run_shell_inline(submitted))
                return

            # ``!`` or ``$`` prefix from chat mode — one-off shell command.
            if submitted.startswith(("!", "$")) and len(submitted) > 1:
                app._shell_task = asyncio.create_task(
                    app._run_shell_inline(submitted[1:].strip())
                )
                return

            task = asyncio.create_task(
                app._controller.process_message(submitted),
            )
            if not app._controller.processing:
                app._controller.set_current_task(task)
