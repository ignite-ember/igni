"""Status bar — two-line footer with identity + live activity.

Extracted from ``_chrome.py`` (iter 38) per Pattern 8: small
modules, one responsibility. Top row shows slow-changing
identity (model, session, cloud), bottom row shows live activity
(run timer, context usage, CodeIndex state) — split so the eye
has a stable anchor while the bottom row ticks.
"""

from __future__ import annotations

from rich.text import Text
from textual.reactive import reactive
from textual.timer import Timer
from textual.widget import Widget

from ember_code.frontend.tui.widgets._codeindex_panel import CodeIndexStatusInfo
from ember_code.frontend.tui.widgets._formatting import format_elapsed_time, format_token_count


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
            return "CodeIndex [dim]checking…[/dim]"
        if s.sync_error:
            return "CodeIndex [red]!err[/red]"
        if s.head_indexed:
            return "CodeIndex [green]✓[/green]"
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
            top_parts.append(f"[cyan]☁[/cyan] {self._cloud_org}")

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
