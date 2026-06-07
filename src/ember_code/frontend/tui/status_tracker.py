"""StatusTracker — tracks token usage, context window, and message count."""

from typing import TYPE_CHECKING

from textual.css.query import NoMatches

from ember_code.frontend.tui.widgets import StatusBar

if TYPE_CHECKING:
    from ember_code.frontend.tui.app import EmberApp


class StatusTracker:
    """Tracks token usage, context window, and delegates to StatusBar."""

    def __init__(self, app: "EmberApp"):
        self._app = app
        self.total_tokens_used: int = 0
        # Context tokens: only main conversation agent tokens (excludes sub-agents)
        self._context_input_tokens: int = 0
        self.max_context_tokens: int = 128_000

    def _bar(self) -> StatusBar | None:
        try:
            return self._app.query_one("#status-bar", StatusBar)
        except NoMatches:
            return None

    def add_tokens(self, input_tokens: int, output_tokens: int) -> None:
        self.total_tokens_used += input_tokens + output_tokens
        bar = self._bar()
        if bar:
            bar.add_tokens(input_tokens, output_tokens)

    def start_run(self) -> None:
        bar = self._bar()
        if bar:
            bar.start_run()

    def end_run(self) -> None:
        bar = self._bar()
        if bar:
            bar.end_run()

    def set_run_tokens(self, input_tokens: int, output_tokens: int) -> None:
        bar = self._bar()
        if bar:
            bar.set_run_tokens(input_tokens, output_tokens)

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

    def add_context_tokens(self, input_tokens: int) -> None:
        """Track main conversation input tokens for context % calculation.

        Only call this for the top-level agent's model requests — not sub-agents.
        The input_tokens of the last main-agent request approximates how full
        the context window is (since it includes conversation history).
        """
        self._context_input_tokens = input_tokens

    def update_context_usage(self) -> None:
        bar = self._bar()
        if bar:
            bar.set_context_usage(self._context_input_tokens, self.max_context_tokens)

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

    def record_turn(self) -> None:
        pass  # No longer tracking message count in status bar

    def reset(self) -> None:
        self.total_tokens_used = 0
        self._context_input_tokens = 0
