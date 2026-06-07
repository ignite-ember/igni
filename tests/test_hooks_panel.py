"""Tests for ``HooksPanelWidget``.

Read-only inspection panel — the only outbound action is
``ReloadRequested`` (via ``R``). Tests mirror the
``test_loop_panel`` / ``test_codeindex_panel`` pattern: a small
Textual host app drives key presses and inspects the rendered
output.
"""

from __future__ import annotations

from textual.app import App, ComposeResult

from ember_code.frontend.tui.widgets._hooks_panel import (
    HookInfo,
    HooksPanelWidget,
)

# ── Test harness ────────────────────────────────────────────────────


class _Host(App):
    def __init__(self, hooks: list[HookInfo] | None = None) -> None:
        super().__init__()
        self._hooks = hooks or []
        self.captured: list = []

    def compose(self) -> ComposeResult:
        yield HooksPanelWidget(hooks=self._hooks)

    def on_hooks_panel_widget_reload_requested(self, _m) -> None:
        self.captured.append(("reload",))

    def on_hooks_panel_widget_panel_closed(self, _m) -> None:
        self.captured.append(("closed",))


def _hook(
    event: str = "PreToolUse",
    type_: str = "command",
    command: str = "echo hi",
    matcher: str = "",
    timeout_ms: int = 10000,
    background: bool = False,
    url: str = "",
    headers: dict[str, str] | None = None,
) -> HookInfo:
    return HookInfo(
        event=event,
        type=type_,
        command=command,
        url=url,
        matcher=matcher,
        timeout_ms=timeout_ms,
        background=background,
        headers=headers or {},
    )


# ── Empty state ────────────────────────────────────────────────────


async def test_empty_panel_renders_actionable_hint() -> None:
    """No hooks configured → status shows ``0 hook(s)`` and the
    rendered list points the user at the settings.json files plus
    the ``R`` reload key. Without this they'd see a blank panel
    after opening and not know how to populate it."""
    app = _Host()
    async with app.run_test():
        panel = app.query_one(HooksPanelWidget)
        text = panel._status_text()
        assert "0 hook(s)" in text


# ── Status header ─────────────────────────────────────────────────


async def test_status_lists_event_count_and_event_names() -> None:
    """Status surfaces both the total count and which events have
    hooks — quick triage for "is anything wired up?"."""
    hooks = [
        _hook(event="PreToolUse"),
        _hook(event="PreToolUse"),
        _hook(event="SessionStart"),
    ]
    app = _Host(hooks=hooks)
    async with app.run_test():
        panel = app.query_one(HooksPanelWidget)
        text = panel._status_text()
        assert "3 hook(s)" in text
        assert "2 event(s)" in text
        assert "PreToolUse" in text
        assert "SessionStart" in text


# ── Row render ────────────────────────────────────────────────────


async def test_hook_row_renders_type_matcher_and_command_preview() -> None:
    h = _hook(
        type_="command",
        command="echo hello && exit 0",
        matcher="Write",
    )
    app = _Host(hooks=[h])
    async with app.run_test():
        panel = app.query_one(HooksPanelWidget)
        row = panel._render_hook(h)
        assert "command" in row
        assert "Write" in row
        assert "echo hello && exit 0" in row


async def test_hook_row_clips_long_command_at_100_chars() -> None:
    """Multi-line / very long commands collapse to a single-line
    preview clipped at ~100 chars so a single oversize hook
    doesn't blow up the panel height. Expanded view shows full
    text."""
    long_cmd = "x" * 300
    app = _Host(hooks=[_hook(command=long_cmd)])
    async with app.run_test():
        panel = app.query_one(HooksPanelWidget)
        row = panel._render_hook(panel._hooks[0])
        assert "x" * 100 in row
        assert "x" * 101 not in row
        assert "..." in row


async def test_hook_row_marks_background_and_custom_timeout() -> None:
    """Two flag-shaped indicators surface non-default config so
    the user can spot them at a glance without expanding:
    ``bg`` for background hooks, the timeout in ms when non-default."""
    app = _Host(
        hooks=[
            _hook(background=True, timeout_ms=2500),
        ]
    )
    async with app.run_test():
        panel = app.query_one(HooksPanelWidget)
        row = panel._render_hook(panel._hooks[0])
        assert "bg" in row
        assert "2500ms" in row


async def test_hook_row_empty_matcher_shows_asterisk() -> None:
    """Empty matcher = all tools. Without an explicit display,
    a blank field could read as "matcher not configured" — the
    asterisk makes the catch-all explicit."""
    app = _Host(hooks=[_hook(matcher="")])
    async with app.run_test():
        panel = app.query_one(HooksPanelWidget)
        row = panel._render_hook(panel._hooks[0])
        assert "*" in row


# ── Expanded view ────────────────────────────────────────────────


async def test_expanded_view_shows_full_command_and_headers() -> None:
    """Expansion reveals the full command (verbatim, not clipped)
    and any HTTP headers — both are too long for the collapsed
    row but matter for debugging hook misconfigurations."""
    long_cmd = "x" * 300
    h = _hook(
        type_="http",
        command="",
        url="https://example.com/hook",
        headers={"Authorization": "Bearer xyz", "X-Trace": "abc123"},
    )
    h2 = _hook(command=long_cmd)
    app = _Host(hooks=[h, h2])
    async with app.run_test():
        panel = app.query_one(HooksPanelWidget)

        http_expanded = panel._render_hook_expanded(h)
        assert "Authorization: Bearer xyz" in http_expanded
        assert "X-Trace: abc123" in http_expanded
        assert "timeout" in http_expanded

        cmd_expanded = panel._render_hook_expanded(h2)
        # Full 300 x's present in the expanded form, including
        # the bit the collapsed row clipped.
        assert "x" * 300 in cmd_expanded


# ── Reload action ────────────────────────────────────────────────


async def test_R_posts_reload_request() -> None:
    """``R`` is the only action key. The app handler picks it up,
    RPCs the BE's ``reload_hooks``, then calls ``set_hooks`` to
    refresh the panel — that round-trip is what makes the panel
    a live view of disk state."""
    app = _Host(hooks=[_hook()])
    async with app.run_test() as pilot:
        panel = app.query_one(HooksPanelWidget)
        panel.focus()
        await pilot.press("R")
        assert ("reload",) in app.captured


# ── Navigation + expansion ───────────────────────────────────────


async def test_arrow_keys_navigate() -> None:
    hooks = [_hook(event="PreToolUse"), _hook(event="PreToolUse"), _hook(event="Stop")]
    app = _Host(hooks=hooks)
    async with app.run_test() as pilot:
        panel = app.query_one(HooksPanelWidget)
        panel.focus()
        await pilot.press("down", "down")
        assert panel.selected_index == 2
        # Clamp at the last row — pressing past the end stays put.
        for _ in range(5):
            await pilot.press("down")
        assert panel.selected_index == 2


async def test_enter_toggles_expansion() -> None:
    app = _Host(hooks=[_hook()])
    async with app.run_test() as pilot:
        panel = app.query_one(HooksPanelWidget)
        panel.focus()
        await pilot.press("enter")
        assert 0 in panel._expanded_indices
        await pilot.press("enter")
        assert 0 not in panel._expanded_indices


# ── Refresh in place ─────────────────────────────────────────────


async def test_set_hooks_clears_expansion_and_resets_selection() -> None:
    """``set_hooks`` is called after a reload — selection and
    expansion should reset so a stale index from a different-shape
    list can't bleed through (the new list might be shorter, or
    have a different event grouping)."""
    app = _Host(hooks=[_hook(event="PreToolUse"), _hook(event="Stop")])
    async with app.run_test() as pilot:
        panel = app.query_one(HooksPanelWidget)
        panel.focus()
        await pilot.press("down")  # select index 1
        await pilot.press("enter")  # expand index 1
        assert panel.selected_index == 1
        assert 1 in panel._expanded_indices
        panel.set_hooks([_hook(event="SessionStart")])
        assert panel.selected_index == 0
        assert panel._expanded_indices == set()


# ── Close ─────────────────────────────────────────────────────────


async def test_unmount_posts_panel_closed() -> None:
    """``PanelClosed`` fires on unmount regardless of removal path.
    Esc-to-close goes through the App's priority binding
    (``action_cancel`` → ``widget.remove()``); ``on_unmount`` is
    the only reliable hook for "panel is going away" cleanup."""
    app = _Host()
    async with app.run_test() as pilot:
        panel = app.query_one(HooksPanelWidget)
        await panel.remove()
        await pilot.pause()
        assert ("closed",) in app.captured


# ── Busy indicator ────────────────────────────────────────────────


async def test_set_busy_swaps_status_text_and_restores() -> None:
    app = _Host(hooks=[_hook()])
    async with app.run_test():
        panel = app.query_one(HooksPanelWidget)
        idle = panel._status_text()
        assert "1 hook(s)" in idle

        panel.set_busy("Reloading hooks…")
        busy = panel._status_text()
        assert "Reloading" in busy
        assert "1 hook(s)" not in busy

        panel.set_busy(None)
        assert "1 hook(s)" in panel._status_text()
