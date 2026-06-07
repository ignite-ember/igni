"""Hooks panel widget — inspect every active hook.

Read-only by design. Hooks don't have a per-hook enable/disable
state today; toggling one would require editing
``settings.json`` (or its three sibling files) which is too risky
to do from a one-key toggle. The panel surfaces what's currently
loaded and exposes ``R`` to reload from disk after the user has
edited the underlying settings files.

Layout mirrors :class:`McpPanelWidget` — single-column list with
section headers per :class:`HookEvent`. Each row shows the
matcher, type, and a clipped command/URL preview; ``Enter``
expands a row to show the full command/headers.
"""

from __future__ import annotations

import contextlib
import logging

from pydantic import BaseModel
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

logger = logging.getLogger(__name__)


__all__ = [
    "HookInfo",
    "HooksPanelWidget",
]


# ── View model ─────────────────────────────────────────────────────


class HookInfo(BaseModel):
    """Panel-side view of one hook.

    Mirrors :py:meth:`BackendServer.get_hooks_details`'s dict shape
    — flat (no nested ``HookDefinition``) because the panel is
    display-only and the wire dict is already flat.
    """

    event: str = ""
    type: str = ""  # "command" | "http"
    command: str = ""
    url: str = ""
    matcher: str = ""
    timeout_ms: int = 0
    background: bool = False
    headers: dict[str, str] = {}


# ── Widget ─────────────────────────────────────────────────────────


class HooksPanelWidget(Widget):
    """Bottom-docked panel — inspect every active hook."""

    can_focus = True

    DEFAULT_CSS = """
    HooksPanelWidget {
        layer: dialog;
        dock: bottom;
        width: 100%;
        height: auto;
        max-height: 30;
        background: $surface-darken-1;
        border-top: heavy $accent;
        padding: 0 2;
    }

    HooksPanelWidget .hooks-title {
        text-style: bold;
        color: $accent;
    }

    HooksPanelWidget .hooks-status {
        color: $text-muted;
        margin-bottom: 1;
    }

    HooksPanelWidget .hooks-list {
        height: auto;
        max-height: 20;
        overflow-y: auto;
    }

    HooksPanelWidget .hooks-entry {
        padding: 0 1;
        height: auto;
    }

    HooksPanelWidget .hooks-entry.-selected {
        background: $accent;
        color: $text;
        text-style: bold;
    }

    HooksPanelWidget .hooks-event {
        color: $accent;
        text-style: bold;
        padding: 0 1;
    }

    HooksPanelWidget .hooks-empty {
        color: $text-muted;
        padding: 1 0;
    }

    HooksPanelWidget .hint {
        color: $text-muted;
        margin-top: 1;
    }
    """

    # ── Outbound messages ────────────────────────────────────────────

    class ReloadRequested(Message):
        pass

    class PanelClosed(Message):
        pass

    # ── Reactive state ──────────────────────────────────────────────

    selected_index = reactive(0)

    def __init__(self, hooks: list[HookInfo] | None = None):
        super().__init__()
        self._hooks: list[HookInfo] = hooks or []
        self._expanded_indices: set[int] = set()
        self._busy_label: str | None = None

    def on_mount(self) -> None:
        """Self-focus after mount so ``on_key`` reliably receives
        ``R`` / arrow keys. See ``CodeIndexPanelWidget.on_mount``
        for the broader rationale (no Input child, Esc consumed
        by the App-level priority binding)."""
        self.focus()

    def on_unmount(self) -> None:
        """Post ``PanelClosed`` on any removal path — Esc via the
        App's ``action_cancel`` removes the widget directly, so
        relying on a key handler would miss the cleanup hook.

        Posted via ``self.app`` — once the widget is mid-detach
        its own pump no longer bubbles to the App. See the
        matching note in CodeIndex panel."""
        with contextlib.suppress(Exception):
            self.app.post_message(self.PanelClosed())

    # ── Layout ──────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Static("[bold $accent]Hooks[/bold $accent]", classes="hooks-title")
        yield Static(self._status_text(), classes="hooks-status")
        with Vertical(classes="hooks-list"):
            yield from self._render_rows()
        yield Static(self._hint_text(), classes="hint")

    # ── Render helpers ──────────────────────────────────────────────

    def _status_text(self) -> str:
        if self._busy_label:
            return f"[bold $accent]{self._busy_label}[/bold $accent]"
        n = len(self._hooks)
        events = sorted({h.event for h in self._hooks})
        return (
            f"[dim]{n} hook(s) across {len(events)} event(s): "
            f"{', '.join(events) if events else '—'}[/dim]"
        )

    @staticmethod
    def _hint_text() -> str:
        return "[dim]↑/↓ navigate · Enter expand · R reload from disk · Esc close[/dim]"

    def _render_rows(self) -> list[Static]:
        if not self._hooks:
            return [
                Static(
                    (
                        "No hooks active. Define them in "
                        "[bold].ember/settings.json[/bold] (or the "
                        "global / .local variants) and press "
                        "[bold]R[/bold] to reload."
                    ),
                    classes="hooks-empty",
                )
            ]
        # Group by event, preserving the order events first appear
        # in ``self._hooks`` so the display matches the user's
        # mental model of the settings file rather than alphabet.
        rendered: list[Static] = []
        last_event: str | None = None
        # Stable indexing — i is the flat row index used for
        # selection / expansion. Event headers count as
        # non-selectable separators (we only build them once per
        # group, not per hook).
        for i, hook in enumerate(self._hooks):
            if hook.event != last_event:
                rendered.append(
                    Static(
                        f"[bold]{hook.event}[/bold]",
                        classes="hooks-event",
                    )
                )
                last_event = hook.event
            classes = ["hooks-entry"]
            if i == self.selected_index:
                classes.append("-selected")
            content = (
                self._render_hook_expanded(hook)
                if i in self._expanded_indices
                else self._render_hook(hook)
            )
            # No per-row id — rebuild does a remove+mount which
            # would race ``id`` uniqueness checks (remove() is async,
            # mount() is sync). Watchers look up entries by their
            # position in the children list filtered by the
            # ``hooks-entry`` class instead.
            rendered.append(Static(content, classes=" ".join(classes)))
        return rendered

    @staticmethod
    def _render_hook(hook: HookInfo) -> str:
        target = hook.command or hook.url or "[dim italic](empty)[/dim italic]"
        # Collapse and clip the target so a multi-line shell command
        # doesn't blow up the row height; the expanded view shows
        # the full text.
        target = " ".join(target.split())
        if len(target) > 100:
            target = target[:100] + "..."
        matcher = (
            f"[dim]matcher:[/dim] [bold]{hook.matcher}[/bold]"
            if hook.matcher
            else "[dim]matcher: *[/dim]"
        )
        flags = []
        if hook.background:
            flags.append("[yellow]bg[/yellow]")
        if hook.timeout_ms and hook.timeout_ms != 10000:
            flags.append(f"[dim]{hook.timeout_ms}ms[/dim]")
        flag_str = "  " + " ".join(flags) if flags else ""
        return f"  [bold]{hook.type or '?'}[/bold]  {matcher}{flag_str}\n      [dim]{target}[/dim]"

    def _render_hook_expanded(self, hook: HookInfo) -> str:
        lines = [self._render_hook(hook)]
        full = hook.command or hook.url or ""
        if full and len(" ".join(full.split())) > 100:
            # Show the full body verbatim (preserving multi-line
            # shell structure) only when the clipped preview hid
            # something — otherwise the expanded view is the same
            # as the row and just adds noise.
            indented = "\n".join(f"      {line}" for line in full.rstrip().split("\n"))
            lines.append(f"      [dim]full:[/dim]\n{indented}")
        if hook.headers:
            header_lines = "\n".join(f"        {k}: {v}" for k, v in hook.headers.items())
            lines.append(f"      [dim]headers:[/dim]\n{header_lines}")
        # Timeout always shown in expanded view since it's a
        # safety-relevant config knob (especially blocking hooks).
        lines.append(
            f"      [dim]timeout:[/dim] {hook.timeout_ms}ms  "
            f"[dim]background:[/dim] {'yes' if hook.background else 'no'}"
        )
        return "\n".join(lines)

    # ── Refresh / rebuild ─────────────────────────────────────────

    def set_hooks(self, hooks: list[HookInfo]) -> None:
        """Replace the displayed hook set. Called by the app after
        a reload RPC returns. Selection resets to 0 so a stale
        index from a different-shape list doesn't bleed through."""
        self._hooks = hooks
        self._expanded_indices.clear()
        self.selected_index = 0
        self._rebuild()

    def set_busy(self, label: str | None) -> None:
        """Flip the status line to a busy indicator. Pair in a
        try/finally around the RPC so a failure doesn't leave the
        panel stuck spinning."""
        self._busy_label = label or None
        with contextlib.suppress(Exception):
            self.query_one(".hooks-status", Static).update(self._status_text())

    def _rebuild(self) -> None:
        # Simpler than the in-place update pattern from the other
        # panels because hooks rows can shift event groupings on
        # reload — easier to rebuild than to track row→event
        # transitions. Performance is fine; the typical hook count
        # is single digits.
        try:
            container = self.query_one(".hooks-list", Vertical)
        except Exception:
            return
        for child in list(container.children):
            child.remove()
        for widget in self._render_rows():
            container.mount(widget)
        with contextlib.suppress(Exception):
            self.query_one(".hooks-status", Static).update(self._status_text())

    # ── Watchers ──────────────────────────────────────────────────

    def _entries(self) -> list[Static]:
        """List of mounted hook rows in display order (excludes
        the event-group headers). Returns ``[]`` if the list
        container hasn't mounted yet — callers should treat the
        absence as a no-op rather than an error."""
        try:
            container = self.query_one(".hooks-list", Vertical)
        except Exception:
            return []
        return [
            c
            for c in container.children
            if isinstance(c, Static) and "hooks-entry" in (c.classes or set())
        ]

    def watch_selected_index(self, old: int, new: int) -> None:
        entries = self._entries()
        if 0 <= old < len(entries):
            entries[old].remove_class("-selected")
        if 0 <= new < len(entries):
            entries[new].add_class("-selected")
            # Keep the highlighted row inside the viewport — without
            # this, navigating past the visible window leaves the
            # selection off-screen.
            with contextlib.suppress(Exception):
                entries[new].scroll_visible(animate=False)

    # ── Input ─────────────────────────────────────────────────────

    def on_key(self, event) -> None:
        # Esc is handled by the App's priority binding (see
        # CodeIndexPanelWidget for the broader rationale).
        if event.key == "R":
            event.stop()
            event.prevent_default()
            self.post_message(self.ReloadRequested())
            return
        if event.key == "up" and self._hooks:
            event.stop()
            event.prevent_default()
            self.selected_index = max(0, self.selected_index - 1)
        elif event.key == "down" and self._hooks:
            event.stop()
            event.prevent_default()
            self.selected_index = min(len(self._hooks) - 1, self.selected_index + 1)
        elif event.key == "enter" and self._hooks:
            event.stop()
            event.prevent_default()
            self._toggle_expand_selected()

    def _toggle_expand_selected(self) -> None:
        if not (0 <= self.selected_index < len(self._hooks)):
            return
        if self.selected_index in self._expanded_indices:
            self._expanded_indices.discard(self.selected_index)
        else:
            self._expanded_indices.add(self.selected_index)
        entries = self._entries()
        if not (0 <= self.selected_index < len(entries)):
            return
        hook = self._hooks[self.selected_index]
        content = (
            self._render_hook_expanded(hook)
            if self.selected_index in self._expanded_indices
            else self._render_hook(hook)
        )
        with contextlib.suppress(Exception):
            entries[self.selected_index].update(content)
