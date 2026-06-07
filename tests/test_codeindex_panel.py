"""Tests for ``CodeIndexPanelWidget``.

The panel is a current-commit status display plus three verb keys
(``S`` sync, ``C`` clean, ``I`` install). Search lives on the
slash command and renders to chat — not exercised here.
"""

from __future__ import annotations

from textual.app import App, ComposeResult

from ember_code.frontend.tui.widgets._codeindex_panel import (
    CodeIndexPanelWidget,
    CodeIndexStatusInfo,
)

# ── Test harness ────────────────────────────────────────────────────


class _Host(App):
    def __init__(self, status: CodeIndexStatusInfo | None = None) -> None:
        super().__init__()
        self._status = status or CodeIndexStatusInfo(
            local_sha="abc1234567890def",
            index_head="abc1234567890def",
            head_indexed=True,
            install_state="installed",
            repository_id="owner/repo",
        )
        self.captured: list = []

    def compose(self) -> ComposeResult:
        yield CodeIndexPanelWidget(status=self._status)

    def on_code_index_panel_widget_sync_requested(self, _m) -> None:
        self.captured.append(("sync",))

    def on_code_index_panel_widget_clean_requested(self, _m) -> None:
        self.captured.append(("clean",))

    def on_code_index_panel_widget_install_requested(self, _m) -> None:
        self.captured.append(("install",))

    def on_code_index_panel_widget_panel_closed(self, _m) -> None:
        self.captured.append(("closed",))


# ── Verb actions ────────────────────────────────────────────────────


async def test_S_posts_sync_request() -> None:
    """``S`` posts ``SyncRequested``. Capital letters are used for
    verb actions so they're unambiguous and so a stray Shift
    doesn't fire two things at once."""
    app = _Host()
    async with app.run_test() as pilot:
        panel = app.query_one(CodeIndexPanelWidget)
        panel.focus()
        await pilot.press("S")
        assert ("sync",) in app.captured


async def test_C_posts_clean_request() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        panel = app.query_one(CodeIndexPanelWidget)
        panel.focus()
        await pilot.press("C")
        assert ("clean",) in app.captured


async def test_I_posts_install_request() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        panel = app.query_one(CodeIndexPanelWidget)
        panel.focus()
        await pilot.press("I")
        assert ("install",) in app.captured


# ── Status header ─────────────────────────────────────────────────


async def test_status_renders_head_short_sha_and_indexed() -> None:
    """The header focuses on the current commit: HEAD short SHA +
    indexed badge + install state."""
    status = CodeIndexStatusInfo(
        local_sha="cafebabe1234567890",
        index_head="cafebabe1234567890",
        head_indexed=True,
        install_state="installed",
        repository_id="owner/repo",
    )
    app = _Host(status=status)
    async with app.run_test():
        panel = app.query_one(CodeIndexPanelWidget)
        text = panel._status_text()
        assert "cafebabe1234" in text  # short SHA
        assert "indexed" in text
        assert "owner/repo" in text


async def test_status_shows_syncing_pct_when_in_progress() -> None:
    """While the server is indexing the current commit, the header
    shows the % + the current step so the user has a sense of
    motion. This is the whole point of the 2s background poll."""
    status = CodeIndexStatusInfo(
        local_sha="abc123",
        sync_in_progress=True,
        sync_progress_pct=42,
        sync_step="extracting entities",
        install_state="installed",
    )
    app = _Host(status=status)
    async with app.run_test():
        panel = app.query_one(CodeIndexPanelWidget)
        text = panel._status_text()
        assert "syncing 42%" in text
        assert "extracting entities" in text


async def test_status_shows_syncing_without_pct_when_unknown() -> None:
    """Some preflight responses don't carry a progress % (early
    steps before chunking). Header still says "syncing" so the user
    knows work is happening; no "0%" lie."""
    status = CodeIndexStatusInfo(
        local_sha="abc123",
        sync_in_progress=True,
        sync_progress_pct=None,
        install_state="installed",
    )
    app = _Host(status=status)
    async with app.run_test():
        panel = app.query_one(CodeIndexPanelWidget)
        text = panel._status_text()
        assert "syncing" in text
        # No bogus percentage figure.
        assert "%" not in text


async def test_status_shows_not_indexed_with_reason() -> None:
    """When HEAD isn't indexed *and* the sync was skipped, surface
    the reason inline so the user knows why (e.g. "not authenticated",
    "install the GitHub App")."""
    status = CodeIndexStatusInfo(
        local_sha="abc123",
        head_indexed=False,
        sync_reason="not authenticated with Ember Cloud",
        install_state="installed",
    )
    app = _Host(status=status)
    async with app.run_test():
        panel = app.query_one(CodeIndexPanelWidget)
        text = panel._status_text()
        assert "not indexed" in text
        assert "not authenticated" in text


async def test_status_shows_sync_error_red() -> None:
    status = CodeIndexStatusInfo(
        local_sha="abc123",
        sync_error="changeset fetch timeout",
        install_state="installed",
    )
    app = _Host(status=status)
    async with app.run_test():
        panel = app.query_one(CodeIndexPanelWidget)
        text = panel._status_text()
        assert "[red]" in text
        assert "sync error" in text


async def test_status_marks_needs_install_yellow() -> None:
    status = CodeIndexStatusInfo(install_state="needs_install")
    app = _Host(status=status)
    async with app.run_test():
        panel = app.query_one(CodeIndexPanelWidget)
        text = panel._status_text()
        assert "[yellow]" in text
        assert "needs install" in text


# ── Busy indicator ────────────────────────────────────────────────


async def test_set_busy_swaps_status_text_and_restores() -> None:
    app = _Host()
    async with app.run_test():
        panel = app.query_one(CodeIndexPanelWidget)

        idle = panel._status_text()
        assert "abc1234" in idle

        panel.set_busy("Syncing changeset…")
        busy = panel._status_text()
        assert "Syncing" in busy
        assert "abc1234" not in busy

        panel.set_busy(None)
        assert "abc1234" in panel._status_text()


async def test_set_busy_empty_string_clears() -> None:
    app = _Host()
    async with app.run_test():
        panel = app.query_one(CodeIndexPanelWidget)
        panel.set_busy("Cleaning…")
        assert "Cleaning" in panel._status_text()
        panel.set_busy("")
        assert "Cleaning" not in panel._status_text()


# ── Close ─────────────────────────────────────────────────────────


async def test_unmount_posts_panel_closed() -> None:
    """``PanelClosed`` fires on unmount regardless of removal path.

    Esc-to-close goes through the App's priority binding
    (``action_cancel`` → ``widget.remove()``), not the panel's
    ``on_key`` — so the widget can't rely on a key-handler branch
    to post the close message. Hooking ``on_unmount`` ensures
    the app handler runs (stopping the status-poll interval)
    regardless of who removed the widget.
    """
    app = _Host()
    async with app.run_test() as pilot:
        panel = app.query_one(CodeIndexPanelWidget)
        await panel.remove()
        # PanelClosed is queued via app.post_message from on_unmount;
        # let the message pump flush before asserting.
        await pilot.pause()
        assert ("closed",) in app.captured
