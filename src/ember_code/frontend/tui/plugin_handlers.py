"""Plugins panel event handlers for :class:`EmberApp`.

Extracted from ``tui/app.py``. Same pattern as
``codeindex_handlers.py`` etc.: the ``@on(...)``-decorated
class method on :class:`EmberApp` stays as a one-line
delegate; the body lives here.

Free functions taking ``app: EmberApp`` as first arg:

* :func:`show_plugins_panel` — build initial installed +
  marketplace state, mount the panel.
* :func:`build_plugin_state` — RPC call + wire-model map for
  both installed plugins and marketplaces. Used by mount +
  every post-mutation refresh.
* :func:`refresh_plugins_panel` — rebuild + push to widget.
* :func:`on_plugin_toggle` / :func:`on_plugin_install` /
  :func:`on_plugin_update` / :func:`on_plugin_remove` /
  :func:`on_marketplace_refresh` — mutation handlers, all end
  with a state refresh.
* :func:`on_plugins_panel_closed` — restore focus.

Rule 2 clean — all imports at module top.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ember_code.frontend.tui.widgets import (
    MarketplaceInfo,
    PluginInfo,
    PluginsPanelWidget,
    PromptInput,
)

if TYPE_CHECKING:
    from ember_code.frontend.tui.app import EmberApp

logger = logging.getLogger(__name__)


async def build_plugin_state(
    app: "EmberApp",
) -> tuple[list[PluginInfo], list[MarketplaceInfo]]:
    """Fetch installed plugins + marketplace catalog + convert to
    the panel's wire models."""
    installed = await app._backend.get_plugin_details()
    marketplaces = await app._backend.get_marketplaces()
    return installed, marketplaces


async def show_plugins_panel(app: "EmberApp") -> None:
    """Build initial plugin + marketplace lists, mount the panel.

    Wrapped in ``try/except`` because the parent dispatch fires
    this in an ``asyncio.create_task`` that swallows any error
    — without the wrap a bad catalog row makes ``/plugins``
    look like nothing happens, which is exactly the bug a
    stricter wire schema caused (the silent ValidationError
    that hid for a session before we found it).
    """
    try:
        installed, marketplaces = await build_plugin_state(app)
    except Exception as e:
        logger.exception("Failed to build plugin panel state")
        app._conversation.append_error(f"Could not open the plugins panel: {e}")
        return
    panel = PluginsPanelWidget(installed=installed, marketplaces=marketplaces)
    app.mount(panel)
    panel.focus()


async def refresh_plugins_panel(app: "EmberApp") -> None:
    """Rebuild installed + marketplace state and push to the mounted panel."""
    try:
        panel = app.query_one(PluginsPanelWidget)
    except Exception:
        return
    installed, marketplaces = await build_plugin_state(app)
    panel.refresh_data(installed=installed, marketplaces=marketplaces)


async def on_plugin_toggle(app: "EmberApp", name: str, enable: bool) -> None:
    """Enable / disable a plugin, log the result, refresh panel."""
    result = await app._backend.set_plugin_enabled(name, enable)
    app._conversation.append_info(result.text)
    await refresh_plugins_panel(app)


async def on_plugin_install(app: "EmberApp", ref: str, install_ref: str | None) -> None:
    """Install a plugin by ref, log the result, refresh panel."""
    app._conversation.append_info(f"Installing {ref}…")
    result = await app._backend.install_plugin(ref, install_ref)
    app._conversation.append_info(result.text)
    await refresh_plugins_panel(app)


async def on_plugin_update(app: "EmberApp", name: str) -> None:
    """Update a plugin, log the result, refresh panel."""
    app._conversation.append_info(f"Updating {name}…")
    result = await app._backend.update_plugin(name)
    app._conversation.append_info(result.text)
    await refresh_plugins_panel(app)


async def on_plugin_remove(app: "EmberApp", name: str) -> None:
    """Remove a plugin, log the result, refresh panel."""
    result = await app._backend.remove_plugin(name)
    app._conversation.append_info(result.text)
    await refresh_plugins_panel(app)


async def on_marketplace_refresh(app: "EmberApp") -> None:
    """Re-fetch every registered marketplace, log the result, refresh panel."""
    result = await app._backend.refresh_marketplaces()
    app._conversation.append_info(result.text)
    await refresh_plugins_panel(app)


def on_plugins_panel_closed(app: "EmberApp") -> None:
    """Restore focus to the prompt input."""
    app.query_one("#user-input", PromptInput).focus()
