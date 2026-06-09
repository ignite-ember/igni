"""CodeIndex panel widget — current-commit status + verb actions.

Surfaces three things about the current HEAD:

  * Whether it's indexed locally, syncing on the server (with the
    latest preflight % + step), or stuck on a known failure.
  * The repo's GitHub-App install state.
  * Three keyboard verbs to act on it: ``S`` sync, ``C`` clean,
    ``I`` install. (``clean`` is the selective garbage-collect
    verb that drops stale, non-branch commit indexes — see
    :py:meth:`CodeIndex.clean`.)

The app handler polls ``codeindex_status`` every couple of seconds
while the panel is open so the indexed-state / sync-% display
updates live without the user re-running anything.

Search lives on the slash command (``/codeindex search <query>``)
which renders markdown into chat — search results are
better-suited to chat history than to an ephemeral bottom panel
that closes on Esc.
"""

from __future__ import annotations

import contextlib
import logging

from pydantic import BaseModel
from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

logger = logging.getLogger(__name__)


__all__ = [
    "CodeIndexPanelWidget",
    "CodeIndexStatusInfo",
]


# ── View models ────────────────────────────────────────────────────


class CodeIndexStatusInfo(BaseModel):
    """Panel-side view of the CodeIndex header.

    Mirrors :py:meth:`BackendServer.codeindex_status` exactly — the
    backend builds this dict; the panel reconstructs.
    """

    local_sha: str = ""
    remote_url: str = ""
    last_synced_sha: str = ""
    index_head: str = ""
    head_indexed: bool = False
    sync_in_progress: bool = False
    sync_progress_pct: int | None = None
    sync_step: str = ""
    sync_reason: str = ""
    sync_error: str = ""
    # Local apply-progress counters — populated while a sync is
    # running so the resync busy label can render
    # ``Resyncing N/M · current_item``. Zero/empty when no apply
    # is active.
    apply_done: int = 0
    apply_total: int = 0
    apply_step: str = ""
    install_state: str = "unknown"  # "unknown" | "needs_install" | "installed"
    repository_id: str = ""
    install_url: str = ""


# ── Widget ─────────────────────────────────────────────────────────


class CodeIndexPanelWidget(Widget):
    """Bottom-docked panel — current-commit status + verb actions."""

    can_focus = True

    DEFAULT_CSS = """
    CodeIndexPanelWidget {
        layer: dialog;
        dock: bottom;
        width: 100%;
        height: auto;
        background: $surface-darken-1;
        border-top: heavy $accent;
        padding: 0 2;
    }

    CodeIndexPanelWidget .ci-title {
        text-style: bold;
        color: $accent;
    }

    CodeIndexPanelWidget .ci-status {
        color: $text-muted;
        margin-bottom: 1;
    }

    CodeIndexPanelWidget .hint {
        color: $text-muted;
        margin-top: 1;
    }
    """

    # ── Outbound messages ────────────────────────────────────────────

    class SyncRequested(Message):
        pass

    class ResyncRequested(Message):
        pass

    class CleanRequested(Message):
        pass

    class InstallRequested(Message):
        pass

    class PanelClosed(Message):
        pass

    def __init__(self, status: CodeIndexStatusInfo):
        super().__init__()
        self._status = status
        self._busy_label: str | None = None

    def on_mount(self) -> None:
        """Self-focus after mount so ``on_key`` reliably receives
        S / C / I.

        The panel has no focusable children (status display only),
        so unlike the knowledge / mcp / plugins panels we can't
        focus an Input descendant from the app handler — by the
        time the parent calls ``panel.focus()`` the widget may not
        be in the DOM yet. Self-focusing in ``on_mount`` is what
        Textual fires after the widget is fully mounted, so focus
        always lands.

        Note: Esc is consumed by the App's ``priority=True``
        binding (``action_cancel``) before reaching ``on_key``;
        the widget is removed from there, not from our own escape
        branch. Cleanup goes through ``on_unmount`` below so the
        status-poll interval gets cancelled regardless of path.
        """
        self.focus()

    def on_unmount(self) -> None:
        """Post ``PanelClosed`` whenever the widget leaves the DOM.

        The App's ``action_cancel`` calls ``widget.remove()``
        directly (no PanelClosed) when closing dialogs on Esc, so
        relying on the on_key escape branch to post the message
        would skip cleanup. Hooking ``on_unmount`` makes the
        message fire regardless of removal path — Esc, in-widget
        escape, or any future programmatic remove.

        Posting via ``self.app`` rather than ``self`` — by the
        time ``on_unmount`` fires the widget is mid-detach and
        bubbling from its own message pump no longer reaches the
        App. ``app.post_message`` injects the message directly
        into the App's queue so the handler runs reliably.
        """
        with contextlib.suppress(Exception):
            self.app.post_message(self.PanelClosed())

    # ── Layout ──────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Static("[bold $accent]CodeIndex[/bold $accent]", classes="ci-title")
        yield Static(self._status_text(), classes="ci-status")
        yield Static(self._hint_text(), classes="hint")

    # ── Render helpers ──────────────────────────────────────────────

    def _status_text(self) -> str:
        if self._busy_label:
            return f"[bold $accent]{self._busy_label}[/bold $accent]"
        s = self._status
        # Short-SHA for the header.
        head = (s.local_sha or "—")[:12]
        # Indexed state: highest-priority signal — the user opened the
        # panel to confirm whether the current commit is searchable.
        if s.sync_error:
            indexed = f"[red]sync error[/red] [dim]{s.sync_error[:60]}[/dim]"
        elif s.sync_in_progress:
            if s.sync_progress_pct is not None:
                step = f" [dim]· {s.sync_step}[/dim]" if s.sync_step else ""
                indexed = f"[yellow]syncing {s.sync_progress_pct}%[/yellow]{step}"
            else:
                indexed = "[yellow]syncing…[/yellow]"
        elif s.head_indexed:
            indexed = "[green]indexed[/green]"
        elif s.sync_reason:
            indexed = f"[yellow]not indexed[/yellow] [dim]· {s.sync_reason[:60]}[/dim]"
        else:
            indexed = "[yellow]not indexed[/yellow]"
        install = {
            "installed": f"[green]installed[/green] ({s.repository_id or '—'})",
            "needs_install": "[yellow]needs install[/yellow]",
            "unknown": "[dim]install state unknown[/dim]",
        }.get(s.install_state, "[dim]—[/dim]")
        return f"[dim]HEAD:[/dim] {head}  [dim]·[/dim] {indexed}  [dim]·[/dim] {install}"

    @staticmethod
    def _hint_text() -> str:
        return "[dim]S sync · R resync · C clean · I install · Esc close[/dim]"

    # ── Refresh / rebuild ─────────────────────────────────────────

    def set_status(self, status: CodeIndexStatusInfo) -> None:
        """Replace the header. Called by the app's poll tick and
        after sync/clean/install RPCs so the user sees the indexed
        state and sync % update without manual refresh."""
        self._status = status
        with contextlib.suppress(Exception):
            self.query_one(".ci-status", Static).update(self._status_text())

    def set_busy(self, label: str | None) -> None:
        """Flip the status line to a busy indicator. Pair in a
        try/finally around the RPC so a failure doesn't leave the
        panel stuck spinning."""
        self._busy_label = label or None
        with contextlib.suppress(Exception):
            self.query_one(".ci-status", Static).update(self._status_text())

    # ── Input ─────────────────────────────────────────────────────

    def on_key(self, event) -> None:
        # Esc is intentionally not handled here — the App's
        # ``priority=True`` escape binding fires first and routes
        # through ``action_cancel`` (which removes the widget by
        # type). Re-handling it here would double-fire
        # ``PanelClosed`` (once from this branch, once from
        # ``on_unmount`` during the removal).
        if event.key == "S":
            event.stop()
            event.prevent_default()
            self.post_message(self.SyncRequested())
            return
        if event.key == "R":
            event.stop()
            event.prevent_default()
            self.post_message(self.ResyncRequested())
            return
        if event.key == "C":
            event.stop()
            event.prevent_default()
            self.post_message(self.CleanRequested())
            return
        if event.key == "I":
            event.stop()
            event.prevent_default()
            self.post_message(self.InstallRequested())
