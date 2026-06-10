"""App chrome widgets: banners, bars, spinner, queue panel."""

import logging

from rich.text import Text
from textual.message import Message
from textual.reactive import reactive
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static

from ember_code import __version__
from ember_code.frontend.tui.widgets._codeindex_panel import CodeIndexStatusInfo
from ember_code.frontend.tui.widgets._constants import SPINNER_FRAMES
from ember_code.frontend.tui.widgets._formatting import format_elapsed_time, format_token_count

logger = logging.getLogger(__name__)

_QUIT_KEY = "Ctrl+D"


class WelcomeBanner(Static):
    """Welcome banner shown at startup — minimal Claude Code style."""

    DEFAULT_CSS = """
    WelcomeBanner {
        padding: 1 0 0 0;
        margin: 0 0 1 0;
    }
    """

    def __init__(self):
        banner = (
            f"  [bold]Ember Code[/bold] [dim]v{__version__}[/dim]\n"
            f"  [dim]/help for commands · {_QUIT_KEY} to quit[/dim]"
        )
        super().__init__(banner)


class SpinnerWidget(Static):
    """Claude Code-style activity indicator.

    Keeps it simple — just a label with animated dots.
    All token/time stats live in the footer StatusBar.
    """

    DEFAULT_CSS = """
    SpinnerWidget {
        height: 1;
        margin: 0 0 0 2;
    }
    """

    def __init__(self, label: str = "Thinking"):
        self._label = label
        self._frame = 0
        self._tokens: int = 0
        self._timer: Timer | None = None
        super().__init__(self._format())

    def on_mount(self) -> None:
        self._timer = self.set_interval(1 / 12, self._tick)

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(SPINNER_FRAMES)
        self.update(self._format())

    def render_text(self) -> str:
        """Plain-text render used by tests and direct inspection."""
        frame = SPINNER_FRAMES[self._frame]
        text = f"{frame} {self._label}..."
        if self._tokens > 0:
            text += f"  {format_token_count(self._tokens)} tokens"
        return text

    def _format(self) -> str:
        frame = SPINNER_FRAMES[self._frame]
        if self._label == "Thinking":
            return f"[dim]{frame} Thinking...[/dim]"
        return f"[bold $accent]{frame} {self._label}...[/bold $accent]"

    def set_label(self, label: str) -> None:
        self._label = label
        self.update(self._format())

    def set_tokens(self, tokens: int) -> None:
        self._tokens = tokens

    def stop(self) -> None:
        if self._timer:
            self._timer.stop()
            self._timer = None


class StatusBar(Widget):
    """Two-line status display at the footer.

    Top row: identity — model name, session id, cloud connection.
    Bottom row: live activity — run timer, context window usage,
    CodeIndex slot. Split by cadence (identity rarely changes,
    activity ticks every render) so the eye has a stable anchor
    on the top row while the bottom updates.

    Uses a reactive ``_tick`` counter so Textual re-renders
    automatically; avoids the ``Static.update()`` clearing issue.
    The host app sizes the widget to ``height: 3`` (1 row for the
    top border + 2 content rows) so both lines fit.
    """

    _tick = reactive(0)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._model_name: str = ""
        self._run_elapsed: float = 0.0
        self._context_tokens: int = 0
        self._max_context: int = 128_000
        self._running: bool = False
        self._run_timer: Timer | None = None
        self._last_elapsed: float = 0.0
        self._cloud_connected: bool = False
        self._cloud_org: str = ""
        # Short session identifier (8-char uuid prefix). Rendered on
        # the right of the model name so the user can match the
        # current run to ``~/.ember/sessions.db`` or ``/session``
        # picker entries. Empty until the FE has pulled the BE's
        # cached value.
        self._session_id: str = ""
        # CodeIndex state — always rendered (even with no data, so
        # the user knows the feature exists). Defaults to "unknown"
        # which shows as ``CodeIndex offline`` until the first poll
        # response arrives.
        self._codeindex_status: CodeIndexStatusInfo | None = None

    @property
    def context_used_pct(self) -> float:
        if self._max_context <= 0:
            return 0.0
        return self._context_tokens / self._max_context * 100

    def update_model(self, model: str) -> None:
        self._model_name = model
        self._tick += 1

    def set_session_id(self, session_id: str) -> None:
        """Update the short session identifier shown next to the model."""
        self._session_id = session_id or ""
        self._tick += 1

    def set_context_usage(self, context_tokens: int, max_context: int) -> None:
        self._context_tokens = context_tokens
        self._max_context = max_context
        self._tick += 1

    def set_ide_status(self, name: str, connected: bool) -> None:
        """No-op shim.

        Historically this surfaced the IDE-MCP connection in the
        status bar. The bar is tight horizontal space and a single
        slot couldn't honestly represent N MCP servers — when
        multiple were configured the last call's name overwrote
        the previous one, and disconnects didn't update at all.
        State now lives exclusively in the ``/mcp`` panel.

        Kept as a no-op so existing call sites (FE init,
        ``/mcp`` panel refresh) don't need surgery on every
        caller; they're harmless invocations now.
        """
        return

    def set_cloud_status(self, connected: bool, org_name: str = "") -> None:
        """Update the Ember Cloud connection indicator."""
        self._cloud_connected = connected
        self._cloud_org = org_name
        self._tick += 1

    def set_codeindex_status(self, status: CodeIndexStatusInfo | None) -> None:
        """Update the CodeIndex slot. Passing ``None`` keeps the
        previous value so a transient poll failure doesn't blank the
        badge; the slot is always rendered so callers don't need to
        worry about hide/show toggling."""
        if status is not None:
            self._codeindex_status = status
            self._tick += 1

    def start_run(self) -> None:
        self._running = True
        self._run_elapsed = 0.0
        if self._run_timer:
            self._run_timer.stop()
        self._run_timer = self.set_interval(0.1, self._tick_elapsed)
        self._tick += 1

    def end_run(self) -> None:
        self._running = False
        if self._run_timer:
            self._run_timer.stop()
            self._run_timer = None
        self._last_elapsed = self._run_elapsed
        self._tick += 1

    def _tick_elapsed(self) -> None:
        if not self._running:
            return
        self._run_elapsed += 0.1
        self._tick += 1

    def _codeindex_badge(self) -> str:
        """One-token CodeIndex state for the status bar.

        Wording priority — top wins. Each label is meant to read
        as a self-explanatory state on its own, with no jargon
        like "offline" that could be misread as a network outage.

        1. **No data yet** (``self._codeindex_status is None``) —
           ``checking…`` (dim). The eager refresh from ``on_mount``
           replaces this within a second; only visible during the
           brief warm-up window at session start.
        2. ``sync_error`` — ``!err`` (red). User needs to act —
           open the panel to read the full error.
        3. ``head_indexed`` — ``✓`` (green). Current commit is
           fully indexed and searchable. Outranks the install-state
           signals below: search runs against the LOCAL chroma, so
           a cold resolver doesn't make the index any less usable.
           This ordering is load-bearing since the sync
           short-circuit — when the target sha is already indexed,
           startup never calls ``resolver.resolve()``, leaving
           ``install_state == "unknown"`` for the whole session
           even though search works fine. The badge used to show
           ``inactive`` in that state while the panel said
           ``indexed``.
        4. ``install_state == "needs_install"`` — ``uninstalled``
           (yellow). The GitHub App isn't installed for this repo;
           ``I`` in the panel opens the install flow.
        5. ``install_state == "unknown"`` — ``inactive`` (dim).
           Resolver hasn't initialised — no cloud auth, no git
           remote, or feature disabled at the session level. Avoids
           "offline" because the cause isn't connectivity.
        6. ``sync_in_progress`` — ``syncing`` (yellow). BE is
           indexing HEAD. No ``%`` rendered here even though the BE
           sends one — the bar is tight and the panel owns progress
           detail.
        7. Fall-through — ``not indexed`` (yellow). HEAD exists,
           install is fine, no sync is running, but the current
           commit hasn't been synced yet — ``S`` in the panel
           kicks one off.

        The slot is always rendered — no hide path — so the user
        knows CodeIndex exists even when they haven't opened the
        panel.
        """
        s = self._codeindex_status
        if s is None:
            return "CodeIndex [dim]checking\u2026[/dim]"
        if s.sync_error:
            return "CodeIndex [red]!err[/red]"
        if s.head_indexed:
            return "CodeIndex [green]\u2713[/green]"
        if s.install_state == "needs_install":
            return "CodeIndex [yellow]uninstalled[/yellow]"
        if s.install_state == "unknown":
            return "CodeIndex [dim]inactive[/dim]"
        if s.sync_in_progress:
            return "CodeIndex [yellow]syncing[/yellow]"
        return "CodeIndex [yellow]not indexed[/yellow]"

    @staticmethod
    def _fmt(n: int) -> str:
        return format_token_count(n)

    @staticmethod
    def _fmt_time(seconds: float) -> str:
        return format_elapsed_time(seconds)

    def render(self) -> Text:
        """Build the status display. Two lines: identity on top, live
        activity on the bottom.

        Layout was a single ``|``-joined line until we accumulated
        too many slots (model, session, cloud, timer, context,
        CodeIndex) — a single 80-col row truncated on most terminals
        and the slots all blurred into each other. Splitting by
        *cadence* — slow-changing identity vs. ticking activity —
        also gives the eye a stable anchor on the top row while
        the bottom row updates on every render tick.

        The ``#status-bar`` widget is sized to ``height: 2`` in the
        app stylesheet so both lines fit.
        """
        _ = self._tick  # access reactive to register dependency

        # ── Line 1: identity (rarely changes mid-session) ──
        top_parts: list[str] = []
        if self._model_name:
            top_parts.append(f"[bold]{self._model_name}[/bold]")
        if self._session_id:
            top_parts.append(f"session [bold]{self._session_id}[/bold]")
        if self._cloud_connected:
            top_parts.append(f"[cyan]\u2601[/cyan] {self._cloud_org}")

        # ── Line 2: live activity (run timer, context, CodeIndex) ──
        bottom_parts: list[str] = []
        if self._running:
            bottom_parts.append(self._fmt_time(self._run_elapsed))
        if self._context_tokens:
            pct = self._context_tokens / max(self._max_context, 1) * 100
            color = ""
            if pct >= 80:
                color = "[red]"
            elif pct >= 60:
                color = "[yellow]"
            close = "[/]" if color else ""
            bottom_parts.append(
                f"Context: {color}{self._fmt(self._context_tokens)} ({pct:.1f}%){close}"
            )
        # CodeIndex slot is always rendered (no hide path) so the
        # user sees the feature exists even before they open the
        # ``/codeindex`` panel. MCP used to live next to it but was
        # removed — a single slot can't honestly represent N
        # servers, and disconnects never updated cleanly. The
        # ``/mcp`` panel is the canonical source now.
        bottom_parts.append(self._codeindex_badge())

        sep = "  |  "
        if not top_parts and not any(p for p in bottom_parts if p):
            return Text.from_markup(f"[dim]{self._model_name or 'Ready'}[/dim]")

        top = "[dim]" + sep.join(top_parts) + "[/dim]" if top_parts else ""
        bottom = "[dim]" + sep.join(bottom_parts) + "[/dim]"
        return Text.from_markup(f"{top}\n{bottom}")


class QueuePanel(Widget):
    """Interactive panel showing queued messages."""

    can_focus = True

    DEFAULT_CSS = """
    QueuePanel {
        dock: bottom;
        height: auto;
        max-height: 10;
        border-top: solid $accent;
        padding: 0 1;
    }

    QueuePanel.-hidden {
        display: none;
    }

    QueuePanel .queue-header {
        color: $accent;
        text-style: bold;
        height: 1;
    }

    QueuePanel .queue-item {
        height: 1;
        padding: 0 1;
    }

    QueuePanel .queue-item.-selected {
        background: $accent 30%;
        text-style: bold;
    }

    QueuePanel .queue-hint {
        color: $text-muted;
        height: 1;
    }
    """

    class ItemDeleted(Message):
        """Posted when a queue item is deleted."""

        def __init__(self, index: int):
            self.index = index
            super().__init__()

    class ItemEditRequested(Message):
        """Posted when the user wants to edit a queue item."""

        def __init__(self, index: int, text: str):
            self.index = index
            self.text = text
            super().__init__()

    class PanelClosed(Message):
        """Posted when the user closes the panel with Escape."""

        pass

    selected_index = reactive(0)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._items: list[str] = []
        self.add_class("-hidden")

    def refresh_items(self, items: list[str]) -> None:
        """Update the displayed queue items."""
        self._items = list(items)
        if not self._items:
            self.add_class("-hidden")
            self.remove_children()
            return
        self.remove_class("-hidden")
        # Rebuild children FIRST so they exist when the watcher queries
        # them, then clamp selected_index. Reverse order would fire
        # watch_selected_index against a tree that hasn't rebuilt yet.
        self._rebuild()
        clamped = min(self.selected_index, max(0, len(self._items) - 1))
        if clamped != self.selected_index:
            self.selected_index = clamped

    def _rebuild(self) -> None:
        """Rebuild child widgets from current items."""
        self.remove_children()
        if not self._items:
            return
        self.mount(
            Static(
                f"[bold $accent]Queue ({len(self._items)})[/bold $accent]"
                "  [dim]↑↓ navigate  Del remove  Enter edit  Esc close[/dim]",
                classes="queue-header",
            )
        )
        for i, text in enumerate(self._items):
            first_line = text.split("\n", 1)[0].strip()
            preview = first_line if len(first_line) <= 50 else first_line[:47] + "..."
            cls = "queue-item -selected" if i == self.selected_index else "queue-item"
            self.mount(Static(f"  {i + 1}. {preview}", id=f"q-{i}", classes=cls))

    def watch_selected_index(self, old: int, new: int) -> None:
        # No children to update when the panel is empty/hidden. The reactive
        # still fires (e.g. clamping in refresh_items), so bail early instead
        # of spamming "no nodes match" debug logs.
        if not self._items or not self.is_mounted:
            return
        try:
            old_w = self.query_one(f"#q-{old}", Static)
            old_w.remove_class("-selected")
        except Exception as exc:
            logger.debug("Failed to deselect queue item #q-%d: %s", old, exc)
        try:
            new_w = self.query_one(f"#q-{new}", Static)
            new_w.add_class("-selected")
        except Exception as exc:
            logger.debug("Failed to select queue item #q-%d: %s", new, exc)

    def on_key(self, event) -> None:
        if not self._items:
            return
        event.stop()
        event.prevent_default()

        if event.key == "up":
            self.selected_index = max(0, self.selected_index - 1)
        elif event.key == "down":
            self.selected_index = min(len(self._items) - 1, self.selected_index + 1)
        elif event.key in ("delete", "backspace"):
            if 0 <= self.selected_index < len(self._items):
                self.post_message(self.ItemDeleted(self.selected_index))
        elif event.key == "enter":
            if 0 <= self.selected_index < len(self._items):
                self.post_message(
                    self.ItemEditRequested(self.selected_index, self._items[self.selected_index])
                )
        elif event.key == "escape":
            self.post_message(self.PanelClosed())

    def on_click(self, event) -> None:
        """Click a queue item to select it."""
        target = event.widget if hasattr(event, "widget") else None
        if target is None:
            return
        for i in range(len(self._items)):
            try:
                widget = self.query_one(f"#q-{i}", Static)
                if target is widget or target.is_descendant_of(widget):
                    self.selected_index = i
                    return
            except Exception as exc:
                logger.debug("Failed to match click to queue item #q-%d: %s", i, exc)


class TipBar(Static):
    """Usage-tip bar. Host app decides where it sits.

    Originally ``dock: bottom``, but the EmberApp now nests TipBar
    inside the ``#footer`` Vertical container (along with prompt-row
    and status-bar) so a single dock-bottom anchor handles all the
    chrome. Two dock-bottom siblings would overlap rather than
    stack, which broke status-bar visibility on mid-session resize.
    """

    DEFAULT_CSS = """
    TipBar {
        height: 1;
        color: $text-muted;
        padding: 0 1;
    }
    """

    def __init__(self, tip: str | None = None, **kwargs):
        self._tip = tip or ""
        display = f"[dim italic]Tip: {self._tip}[/dim italic]" if self._tip else ""
        super().__init__(display, **kwargs)

    def set_tip(self, tip: str) -> None:
        """Update the displayed tip."""
        self._tip = tip
        self.update(f"[dim italic]Tip: {tip}[/dim italic]")


def _upgrade_command(pkg_name: str) -> str:
    """Return the appropriate upgrade command based on install method."""
    import subprocess
    import sys

    exe = sys.executable

    # Check if running from a Homebrew prefix
    if "/Cellar/" in exe or "/homebrew/" in exe.lower():
        return f"brew upgrade {pkg_name}"

    # Check if pipx manages this package
    try:
        result = subprocess.run(
            ["pipx", "list", "--short"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0 and pkg_name in result.stdout:
            return f"pipx upgrade {pkg_name}"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Check if running inside a uv-managed environment
    if ".venv" in exe:
        try:
            subprocess.run(
                ["uv", "--version"],
                capture_output=True,
                timeout=3,
            )
            return f"uv pip install --upgrade {pkg_name}"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    return f"pip install --upgrade {pkg_name}"


class UpdateBar(Static):
    """Top bar showing an available update notification."""

    DEFAULT_CSS = """
    UpdateBar {
        dock: bottom;
        height: 1;
        color: $warning;
        padding: 0 1;
    }

    UpdateBar.-hidden {
        display: none;
    }
    """

    def __init__(self, **kwargs):
        super().__init__("", **kwargs)
        self.add_class("-hidden")

    def show_update(self, current: str, latest: str, url: str = "", pkg_name: str = "") -> None:
        """Display an update notification."""
        msg = f"Update available: v{current} → v{latest}"
        if pkg_name:
            msg += f"  |  {_upgrade_command(pkg_name)}"
        self.update(msg)
        self.remove_class("-hidden")

    def hide(self) -> None:
        self.add_class("-hidden")
