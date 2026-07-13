"""Session picker + model picker + login flow + help panel
handlers for :class:`EmberApp`.

Extracted from ``tui/app.py``. Same pattern as
``codeindex_handlers.py`` etc.: the ``@on(...)``-decorated
class method on :class:`EmberApp` stays as a one-line
delegate; the body lives here.

Grouped together because they're all "modal chrome" — small
widgets the user picks-from or logs-in-through, all sharing
the same "mount → focus → clean up" lifecycle.

Free functions taking ``app: EmberApp`` as first arg:

* :func:`on_session_selected` — thin wrapper over the session
  manager's switch-to.
* :func:`on_session_cancelled` — restore focus.
* :func:`show_model_picker` — refresh cloud catalogue,
  compute the credentialed-only model list, mount.
* :func:`on_model_selected` — switch backend model, sync FE
  registry, refresh status bar.
* :func:`on_model_cancelled` — restore focus.
* :func:`show_login` — mount the LoginWidget + kick the BE.
* :func:`on_logged_in` / :func:`on_login_cancelled` /
  :func:`on_login_status_push` / :func:`on_login_result_push`
  — mirror BE-side login lifecycle to the widget.
* :func:`show_help_panel` / :func:`on_help_panel_closed` —
  the interactive help panel.

Rule 2 clean — all imports at module top.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from textual.css.query import NoMatches

from ember_code.core.auth.credentials import CloudCredentials
from ember_code.core.config.cloud_models import (
    fetch_cloud_models,
    merge_into_registry,
)
from ember_code.frontend.tui.widgets import (
    HelpPanelWidget,
    LoginWidget,
    ModelPickerWidget,
    PromptInput,
    SessionPickerWidget,
)

if TYPE_CHECKING:
    from ember_code.frontend.tui.app import EmberApp

logger = logging.getLogger(__name__)


# ── Session picker ────────────────────────────────────────────


async def on_session_selected(app: "EmberApp", session_id: str) -> None:
    """Delegate to the session manager for the actual switch."""
    await app._sessions.switch_to(session_id)


def on_session_cancelled(app: "EmberApp") -> None:
    """Restore focus to the prompt input."""
    app.query_one("#user-input", PromptInput).focus()


# ── Model picker ──────────────────────────────────────────────


def show_model_picker(app: "EmberApp") -> None:
    """Show models that have credentials: explicit API key, env
    var, key command, or Ember Cloud auth (for models hosted on
    ignite-ember.sh).

    Refreshes the cloud catalogue on open. Synchronous +
    bounded (3s) — opening the picker shouldn't hang the TUI
    even on a flaky network. ``merge_into_registry`` is
    no-op-on-duplicate so user-edited entries survive. The
    Session backend does the same refresh independently on its
    side; doing it here too keeps the TUI display fresh
    without an extra RPC round-trip.
    """
    cloud_token = CloudCredentials(app.settings.auth.credentials_file).access_token

    if cloud_token:
        cloud_entries = fetch_cloud_models(app.settings.api_url, cloud_token)
        if cloud_entries:
            merge_into_registry(
                app.settings.models.registry, cloud_entries, app.settings.api_url
            )

    models = sorted(
        name
        for name, cfg in app.settings.models.registry.items()
        if (cfg.get("api_key") == "cloud_token" and cloud_token)
        or (cfg.get("api_key") and cfg.get("api_key") != "cloud_token")
        or cfg.get("api_key_env")
        or cfg.get("api_key_cmd")
    )
    if not models:
        app._conversation.append_error("No models configured with API keys.")
        return
    current = app.settings.models.default
    picker = ModelPickerWidget(models=models, current_model=current)
    app.mount(picker)
    picker.focus()


async def on_model_selected(app: "EmberApp", model_name: str) -> None:
    """Switch the backend + FE model, refresh the status bar.

    ``switch_model`` is async-await now; the prior fire-and-
    forget version raced the status-bar update below and left
    the footer showing the OLD model name until the next
    render. Awaiting here keeps the bar and the chat info line
    agree.
    """
    if hasattr(app, "_backend"):
        await app._backend.switch_model(model_name)
    app.settings.models.default = model_name
    app._status.update_status_bar()
    app._conversation.append_info(f"Switched to model: {model_name}")
    app.query_one("#user-input", PromptInput).focus()


def on_model_cancelled(app: "EmberApp") -> None:
    """Restore focus to the prompt input."""
    app.query_one("#user-input", PromptInput).focus()


# ── Login ─────────────────────────────────────────────────────


def show_login(app: "EmberApp") -> None:
    """Mount the LoginWidget and tell the BE to start the flow.

    Removes any existing widget first to avoid stacking two
    LoginWidgets when the user re-triggers ``/login`` from a
    partially-completed session.
    """
    try:
        old = app.query_one(LoginWidget)
        old.cancel()
    except NoMatches:
        pass
    widget = LoginWidget(backend=app._backend)
    app.mount(widget)
    widget.focus()
    asyncio.create_task(app._backend.start_login())


def on_logged_in(app: "EmberApp", email: str) -> None:
    """Refresh cloud creds on the session, update the cloud
    status-bar badge, focus back to the prompt."""
    if hasattr(app, "_backend"):
        status = app._backend.reload_cloud_credentials()
        app._status.set_cloud_status(status.cloud_connected, status.cloud_org)
        app._status.update_status_bar()

    app._conversation.append_info(f"Logged in as {email}")
    app.query_one("#user-input", PromptInput).focus()


def on_login_cancelled(app: "EmberApp") -> None:
    """Restore focus to the prompt input."""
    app.query_one("#user-input", PromptInput).focus()


def on_login_status_push(app: "EmberApp", payload: dict) -> None:
    """Handle ``login_status`` push — forward to LoginWidget if mounted."""
    try:
        widget = app.query_one(LoginWidget)
        widget.update_status(payload.get("text", ""))
    except NoMatches:
        pass


def on_login_result_push(app: "EmberApp", payload: dict) -> None:
    """Handle ``login_result`` push — forward to LoginWidget if mounted."""
    try:
        widget = app.query_one(LoginWidget)
        widget.show_result(payload.get("success", False), payload.get("result", ""))
    except NoMatches:
        pass


# ── Help panel ────────────────────────────────────────────────


def show_help_panel(app: "EmberApp") -> None:
    """Mount the interactive help panel."""
    panel = HelpPanelWidget()
    app.mount(panel)
    panel.focus()


def on_help_panel_closed(app: "EmberApp") -> None:
    """Restore focus to the prompt input."""
    app.query_one("#user-input", PromptInput).focus()
