"""StatusTracker — keeps the status bar in sync with session state.

Token-related state is intentionally minimal: just the locally-counted
context fill and the model's window size. The bar's other slots
(model, session, cloud, CodeIndex) are pure passthrough from backend
RPCs. We don't track per-run or session-total token sums anymore —
those used to read API-reported ``input_tokens`` which on prompt-
caching providers compounded ``cache_read`` into millions of tokens
and triggered the auto-compaction → history wipe path.
"""

from typing import TYPE_CHECKING

from textual.css.query import NoMatches

from ember_code.frontend.tui.widgets import StatusBar

if TYPE_CHECKING:
    from ember_code.frontend.tui.app import EmberApp


class StatusTracker:
    """Tracks context window usage and delegates to StatusBar."""

    def __init__(self, app: "EmberApp"):
        self._app = app
        # Backend-supplied count of the current conversation's tokens.
        # Refreshed in ``RunController._post_run_compaction`` via
        # ``BackendClient.count_context_tokens`` (Agno's per-model
        # tokenizer). Cached here so the bar's sync render can read
        # it without an RPC.
        self._context_tokens: int = 0
        self.max_context_tokens: int = 128_000

    def _bar(self) -> StatusBar | None:
        try:
            return self._app.query_one("#status-bar", StatusBar)
        except NoMatches:
            return None

    def start_run(self) -> None:
        bar = self._bar()
        if bar:
            bar.start_run()

    def end_run(self) -> None:
        bar = self._bar()
        if bar:
            bar.end_run()

    def update_status_bar(self) -> None:
        backend = getattr(self._app, "_backend", None)
        if not backend:
            return
        bar = self._bar()
        if bar:
            status = backend.get_status()
            bar.update_model(status.model)
            bar.set_cloud_status(status.cloud_connected, status.cloud_org)
            # ``session_id`` is cached by ``BackendClient.refresh_cache``
            # on connect and refreshed when the user switches sessions
            # via ``/sessions``, so it's already current here — no
            # extra RPC. Empty string short-circuits the bar render.
            bar.set_session_id(getattr(backend, "session_id", ""))

    def set_context_tokens(self, tokens: int) -> None:
        """Replace the cached context-fill count.

        Called after the backend re-counts the conversation locally;
        also called with ``0`` after auto-compaction wipes history.
        """
        self._context_tokens = max(0, tokens)

    def update_context_usage(self) -> None:
        bar = self._bar()
        if bar:
            bar.set_context_usage(self._context_tokens, self.max_context_tokens)

    def set_ide_status(self, name: str, connected: bool) -> None:
        """Update the IDE connection indicator in the status bar."""
        bar = self._bar()
        if bar:
            bar.set_ide_status(name, connected)

    def set_cloud_status(self, connected: bool, org_name: str = "") -> None:
        """Update the Ember Cloud connection indicator in the status bar."""
        bar = self._bar()
        if bar:
            bar.set_cloud_status(connected, org_name)

    def set_codeindex_status(self, status) -> None:
        """Pipe a ``CodeIndexStatusInfo`` to the status bar's
        always-on CodeIndex slot. ``None`` is tolerated — the bar
        keeps the previous value rather than blanking on transient
        poll failures."""
        bar = self._bar()
        if bar:
            bar.set_codeindex_status(status)

    def reset(self) -> None:
        self._context_tokens = 0
