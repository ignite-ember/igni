"""Plugins panel widget — browse, toggle, install, update, remove plugins.

Two tabs:

  * **Installed** — every discovered plugin with name, version, source
    root, enabled status, bundled contents. Space toggles enable/
    disable. ``u`` / ``r`` update / remove. Enter expands the detail
    panel (description, pinned SHA, path).
  * **Marketplace** — every plugin in registered marketplace catalogs.
    ``i`` installs the selected entry. ``a`` opens an inline prompt
    to add a marketplace by URL.

Tab switches via Tab. Esc closes. ``?`` shows the help line.

Modeled after :class:`MCPPanelWidget` — same docked-at-bottom layout,
same nav idioms, same Pydantic data models for the panel's view of
state. The data shape (``PluginInfo`` / ``MarketplaceInfo``) is
intentionally redundant with the backend dicts so the widget owns its
own model layer and isn't coupled to the RPC payload format.
"""

from __future__ import annotations

import logging
from typing import Literal

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

# The view models are the wire format for plugin / marketplace RPC
# payloads. They live in ``core.plugins.models`` so backend and
# frontend share one source-of-truth shape — adding a field on the
# backend doesn't require keeping a parallel definition in sync here.
from ember_code.core.plugins.models import (
    MarketplaceInfo,
    MarketplacePluginInfo,
    PluginInfo,
)

logger = logging.getLogger(__name__)


Tab = Literal["installed", "marketplace"]


# Re-export so widget consumers keep their existing import paths.
__all__ = [
    "MarketplaceInfo",
    "MarketplacePluginInfo",
    "PluginInfo",
    "PluginsPanelWidget",
]


# ── Widget ─────────────────────────────────────────────────────────


class PluginsPanelWidget(Widget):
    """Bottom-docked panel for the plugin system."""

    can_focus = True

    DEFAULT_CSS = """
    PluginsPanelWidget {
        layer: dialog;
        dock: bottom;
        width: 100%;
        height: auto;
        max-height: 24;
        background: $surface-darken-1;
        border-top: heavy $accent;
        padding: 0 2;
    }

    PluginsPanelWidget .plugins-title {
        text-style: bold;
        color: $accent;
    }

    PluginsPanelWidget .plugins-tabs {
        color: $text-muted;
        margin-bottom: 1;
    }

    PluginsPanelWidget .plugins-tabs .-active {
        color: $accent;
        text-style: bold;
    }

    PluginsPanelWidget .plugins-list {
        height: auto;
        max-height: 16;
        overflow-y: auto;
    }

    PluginsPanelWidget .plugins-entry {
        padding: 0 1;
        height: auto;
    }

    PluginsPanelWidget .plugins-entry.-selected {
        background: $accent;
        color: $text;
        text-style: bold;
    }

    PluginsPanelWidget .plugins-empty {
        color: $text-muted;
        padding: 1 0;
    }

    PluginsPanelWidget .hint {
        color: $text-muted;
        margin-top: 1;
    }
    """

    # ── Outbound messages ───────────────────────────────────────────

    class PluginToggleRequested(Message):
        def __init__(self, name: str, enable: bool):
            self.name = name
            self.enable = enable
            super().__init__()

    class PluginInstallRequested(Message):
        """Install a plugin by ``@marketplace/plugin`` ref or git URL."""

        def __init__(self, ref: str, install_ref: str | None = None):
            self.ref = ref
            self.install_ref = install_ref
            super().__init__()

    class PluginUpdateRequested(Message):
        def __init__(self, name: str):
            self.name = name
            super().__init__()

    class PluginRemoveRequested(Message):
        def __init__(self, name: str):
            self.name = name
            super().__init__()

    class MarketplaceRefreshRequested(Message):
        pass

    class PanelClosed(Message):
        pass

    # ── Reactive state ──────────────────────────────────────────────

    active_tab: reactive[Tab] = reactive("installed")
    selected_index = reactive(0)

    def __init__(
        self,
        installed: list[PluginInfo],
        marketplaces: list[MarketplaceInfo],
    ):
        super().__init__()
        self._installed = installed
        self._marketplaces = marketplaces

    # ── Layout ──────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Static(self._title_text(), classes="plugins-title")
        yield Static(self._tabs_text(), classes="plugins-tabs")
        with Vertical(classes="plugins-list"):
            yield from self._render_entries()
        yield Static(self._hint_text(), classes="hint")

    # ── Helpers: text builders ──────────────────────────────────────

    def _title_text(self) -> str:
        if self.active_tab == "installed":
            n = len(self._installed)
            enabled = sum(1 for p in self._installed if p.enabled)
            return (
                f"[bold $accent]Plugins[/bold $accent]  "
                f"[dim]{enabled} enabled / {n} installed[/dim]"
            )
        n_mkts = len(self._marketplaces)
        total_catalog = sum(len(m.plugins) for m in self._marketplaces)
        return (
            f"[bold $accent]Plugins · Marketplace[/bold $accent]  "
            f"[dim]{total_catalog} plugin(s) across {n_mkts} marketplace(s)[/dim]"
        )

    def _tabs_text(self) -> str:
        installed = "Installed"
        marketplace = "Marketplace"
        if self.active_tab == "installed":
            return f"[bold $accent][{installed}][/bold $accent]  [dim]{marketplace}[/dim]  [dim](Tab to switch)[/dim]"
        return f"[dim]{installed}[/dim]  [bold $accent][{marketplace}][/bold $accent]  [dim](Tab to switch)[/dim]"

    def _hint_text(self) -> str:
        if self.active_tab == "installed":
            return (
                "[dim]↑/↓ navigate · Space toggle · u update · "
                "r remove · Tab marketplace · Esc close[/dim]"
            )
        return (
            "[dim]↑/↓ navigate · i install · R refresh catalogs · Tab installed · Esc close[/dim]"
        )

    def _render_entries(self) -> list[Static]:
        items = self._current_items()
        if not items:
            return [Static(self._empty_text(), classes="plugins-empty")]
        rendered = []
        for i, item in enumerate(items):
            classes = ["plugins-entry"]
            if i == self.selected_index:
                classes.append("-selected")
            rendered.append(
                Static(
                    self._render_item(item, i),
                    id=f"plug-{i}",
                    classes=" ".join(classes),
                )
            )
        return rendered

    def _current_items(self) -> list:
        if self.active_tab == "installed":
            return self._installed
        # Flatten marketplaces into one list with section headers
        # encoded by attaching marketplace name to each entry.
        flat: list[tuple[MarketplaceInfo, MarketplacePluginInfo]] = []
        for m in self._marketplaces:
            for p in m.plugins:
                flat.append((m, p))
        return flat

    def _empty_text(self) -> str:
        if self.active_tab == "installed":
            return (
                "No plugins installed. Use `/plugin install <url>` or "
                "open the Marketplace tab (Tab key)."
            )
        return (
            "No marketplaces registered. Run `/plugin marketplace add <git-url>` from the prompt."
        )

    def _render_item(self, item, _i: int) -> str:
        if self.active_tab == "installed":
            assert isinstance(item, PluginInfo)
            status = "[green]●[/green]" if item.enabled else "[dim]○[/dim]"
            version = f" [dim]v{item.version}[/dim]" if item.version else ""
            bundles = _format_bundle_summary(item)
            source = f"[dim]{item.source_root}[/dim]"
            return f"  {status} {item.name}{version}  {source}  {bundles}"

        # Marketplace tab — item is (MarketplaceInfo, MarketplacePluginInfo)
        mkt, plug = item
        installed_marker = ""
        if any(p.name == plug.name for p in self._installed):
            installed_marker = "  [green](installed)[/green]"
        version = f" [dim]v{plug.version}[/dim]" if plug.version else ""
        return f"  [dim]@{mkt.name}/[/dim]{plug.name}{version}{installed_marker}"

    # ── Refresh in place ───────────────────────────────────────────

    def refresh_data(
        self,
        installed: list[PluginInfo],
        marketplaces: list[MarketplaceInfo],
    ) -> None:
        """Update the panel with fresh plugin + marketplace data."""
        self._installed = installed
        self._marketplaces = marketplaces
        self.selected_index = min(self.selected_index, max(0, len(self._current_items()) - 1))
        self._rebuild()

    def _rebuild(self) -> None:
        """Rebuild list entries + title + tabs in place.

        Update-in-place rather than remove-then-mount: Textual's
        ``Widget.remove()`` queues asynchronously, so a sync rebuild
        that removes then mounts widgets with the same IDs hits a
        ``DuplicateIds`` error before the removals flush.
        """
        try:
            container = self.query_one(".plugins-list", Vertical)
            title = self.query_one(".plugins-title", Static)
            tabs = self.query_one(".plugins-tabs", Static)
            hint = self.query_one(".hint", Static)
        except Exception:
            return

        items = self._current_items()
        existing: dict[str, Static] = {
            child.id: child
            for child in container.children
            if child.id and child.id.startswith("plug-")
        }
        empty_widgets = [
            child for child in container.children if "plugins-empty" in (child.classes or set())
        ]

        if not items:
            # Drop any in-place entries, mount the empty notice once.
            for w in existing.values():
                w.remove()
            if not empty_widgets:
                container.mount(Static(self._empty_text(), classes="plugins-empty"))
            else:
                empty_widgets[0].update(self._empty_text())
        else:
            # Clear any prior empty notice — we have items now.
            for w in empty_widgets:
                w.remove()
            for i, item in enumerate(items):
                widget_id = f"plug-{i}"
                content = self._render_item(item, i)
                if widget_id in existing:
                    existing[widget_id].update(content)
                    if i == self.selected_index:
                        existing[widget_id].add_class("-selected")
                    else:
                        existing[widget_id].remove_class("-selected")
                else:
                    classes = ["plugins-entry"]
                    if i == self.selected_index:
                        classes.append("-selected")
                    container.mount(
                        Static(
                            content,
                            id=widget_id,
                            classes=" ".join(classes),
                        )
                    )
            # Trim excess widgets from a previous larger list.
            for widget_id, child in existing.items():
                try:
                    idx = int(widget_id.split("-")[1])
                    if idx >= len(items):
                        child.remove()
                except (ValueError, IndexError):
                    pass

        title.update(self._title_text())
        tabs.update(self._tabs_text())
        hint.update(self._hint_text())

    # ── Reactive watchers ──────────────────────────────────────────

    def watch_active_tab(self, _old: Tab, _new: Tab) -> None:
        # Reset selection when switching tabs so we're never pointing
        # at an out-of-bounds index from the previous list.
        self.selected_index = 0
        self._rebuild()

    def watch_selected_index(self, old: int, new: int) -> None:
        for i, marker in ((old, False), (new, True)):
            try:
                widget = self.query_one(f"#plug-{i}", Static)
                if marker:
                    widget.add_class("-selected")
                else:
                    widget.remove_class("-selected")
            except Exception:
                pass

    # ── Input handling ─────────────────────────────────────────────

    def on_key(self, event) -> None:
        event.stop()
        event.prevent_default()

        items = self._current_items()

        if event.key == "escape":
            self.post_message(self.PanelClosed())
            self.remove()
            return

        if event.key == "tab":
            self.active_tab = "marketplace" if self.active_tab == "installed" else "installed"
            return

        if not items:
            return

        if event.key == "up":
            self.selected_index = max(0, self.selected_index - 1)
        elif event.key == "down":
            self.selected_index = min(len(items) - 1, self.selected_index + 1)
        elif self.active_tab == "installed":
            if event.key == "space":
                self._toggle_selected_installed()
            elif event.key == "u":
                self._action_update_selected()
            elif event.key == "r":
                self._action_remove_selected()
        elif self.active_tab == "marketplace":
            if event.key == "i":
                self._action_install_selected()
            elif event.key in ("R", "shift+r"):
                self.post_message(self.MarketplaceRefreshRequested())

    def _toggle_selected_installed(self) -> None:
        if not (0 <= self.selected_index < len(self._installed)):
            return
        p = self._installed[self.selected_index]
        self.post_message(self.PluginToggleRequested(name=p.name, enable=not p.enabled))

    def _action_update_selected(self) -> None:
        if not (0 <= self.selected_index < len(self._installed)):
            return
        p = self._installed[self.selected_index]
        self.post_message(self.PluginUpdateRequested(name=p.name))

    def _action_remove_selected(self) -> None:
        if not (0 <= self.selected_index < len(self._installed)):
            return
        p = self._installed[self.selected_index]
        self.post_message(self.PluginRemoveRequested(name=p.name))

    def _action_install_selected(self) -> None:
        items = self._current_items()
        if not (0 <= self.selected_index < len(items)):
            return
        mkt, plug = items[self.selected_index]
        ref = f"@{mkt.name}/{plug.name}"
        install_ref = plug.branch or None
        self.post_message(self.PluginInstallRequested(ref=ref, install_ref=install_ref))

    def on_click(self, event) -> None:
        target = event.widget if hasattr(event, "widget") else None
        if target is None:
            return
        items = self._current_items()
        for i in range(len(items)):
            try:
                widget = self.query_one(f"#plug-{i}", Static)
                if target is widget or target.is_descendant_of(widget):
                    self.selected_index = i
                    return
            except Exception:
                pass


# ── Rendering helpers ──────────────────────────────────────────────


def _format_bundle_summary(plugin: PluginInfo) -> str:
    """Render the bundled-contents summary used in installed-tab rows."""
    flags: list[str] = []
    if plugin.has_skills:
        flags.append("S")
    if plugin.has_agents:
        flags.append("A")
    if plugin.has_hooks:
        flags.append("H")
    if plugin.has_mcp:
        flags.append("M")
    if plugin.has_tools:
        flags.append("T")
    if not flags:
        return "[dim](manifest only)[/dim]"
    return f"[dim]{' '.join(flags)}[/dim]"
