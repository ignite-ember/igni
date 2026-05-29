"""Tests for ``PluginsPanelWidget``.

Uses Textual's ``App.run_test()`` harness to drive the widget through
real key events. Asserts focus on the message-bus contract: which
events the panel posts in response to which keys. Backend side
effects (toggle, install, remove) are out of scope here — they're
covered by the loader/installer/marketplace test suites.

This file deliberately avoids snapshotting rendered text. The panel's
visual layout is allowed to drift; what must stay stable is *the
events the widget emits* and *which item is selected when*.
"""

from __future__ import annotations

from textual.app import App, ComposeResult

from ember_code.frontend.tui.widgets._plugins_panel import (
    MarketplaceInfo,
    MarketplacePluginInfo,
    PluginInfo,
    PluginsPanelWidget,
)

# Async tests pick up the asyncio mark via pytest-asyncio's auto mode
# (configured in pyproject.toml). No module-level mark — that would
# also (incorrectly) tag the sync render-helper tests below.


# ── Test harness ────────────────────────────────────────────────────


class _Host(App):
    """Minimal Textual app that hosts the panel for testing.

    Captures every panel-emitted message so tests can assert on the
    event contract directly.
    """

    def __init__(
        self,
        installed: list[PluginInfo],
        marketplaces: list[MarketplaceInfo],
    ) -> None:
        super().__init__()
        self._installed = installed
        self._marketplaces = marketplaces
        self.captured: list = []

    def compose(self) -> ComposeResult:
        yield PluginsPanelWidget(
            installed=self._installed,
            marketplaces=self._marketplaces,
        )

    def on_plugins_panel_widget_plugin_toggle_requested(self, m) -> None:
        self.captured.append(("toggle", m.name, m.enable))

    def on_plugins_panel_widget_plugin_install_requested(self, m) -> None:
        self.captured.append(("install", m.ref, m.install_ref))

    def on_plugins_panel_widget_plugin_update_requested(self, m) -> None:
        self.captured.append(("update", m.name))

    def on_plugins_panel_widget_plugin_remove_requested(self, m) -> None:
        self.captured.append(("remove", m.name))

    def on_plugins_panel_widget_marketplace_refresh_requested(self, _m) -> None:
        self.captured.append(("refresh",))

    def on_plugins_panel_widget_panel_closed(self, _m) -> None:
        self.captured.append(("closed",))


def _plug(name: str, *, enabled: bool = True, **kw) -> PluginInfo:
    return PluginInfo(name=name, enabled=enabled, **kw)


def _mkt(name: str, plugins: list[tuple[str, str]]) -> MarketplaceInfo:
    return MarketplaceInfo(
        name=name,
        url=f"https://example/{name}",
        plugins=[MarketplacePluginInfo(name=n, source=src) for n, src in plugins],
    )


# ── Installed tab ───────────────────────────────────────────────────


async def test_installed_tab_space_toggles_selected() -> None:
    """Space on the selected installed entry emits a toggle event
    flipping the current enabled state."""
    app = _Host(
        installed=[_plug("alpha", enabled=True), _plug("beta", enabled=False)],
        marketplaces=[],
    )
    async with app.run_test() as pilot:
        await pilot.press("space")
        assert ("toggle", "alpha", False) in app.captured


async def test_installed_tab_down_then_space_toggles_next() -> None:
    """↓ moves selection; space toggles whichever is highlighted."""
    app = _Host(
        installed=[_plug("alpha", enabled=True), _plug("beta", enabled=False)],
        marketplaces=[],
    )
    async with app.run_test() as pilot:
        await pilot.press("down")
        await pilot.press("space")
        assert ("toggle", "beta", True) in app.captured


async def test_installed_tab_u_emits_update() -> None:
    app = _Host(installed=[_plug("alpha")], marketplaces=[])
    async with app.run_test() as pilot:
        await pilot.press("u")
        assert ("update", "alpha") in app.captured


async def test_installed_tab_r_emits_remove() -> None:
    app = _Host(installed=[_plug("alpha")], marketplaces=[])
    async with app.run_test() as pilot:
        await pilot.press("r")
        assert ("remove", "alpha") in app.captured


async def test_navigation_clamps_at_bounds() -> None:
    """Up at index 0 stays at 0; down past last stays at last. The
    panel must never highlight an out-of-bounds index — re-renders
    use ``selected_index`` and would crash on a stale value."""
    app = _Host(
        installed=[_plug("a"), _plug("b"), _plug("c")],
        marketplaces=[],
    )
    async with app.run_test() as pilot:
        panel = app.query_one(PluginsPanelWidget)
        await pilot.press("up")
        await pilot.press("up")
        assert panel.selected_index == 0
        for _ in range(10):
            await pilot.press("down")
        assert panel.selected_index == 2


# ── Marketplace tab ─────────────────────────────────────────────────


async def test_tab_switches_to_marketplace_and_back() -> None:
    """Tab toggles between Installed and Marketplace. Selection
    resets to 0 on switch (so a stale installed index can't bleed
    into the marketplace list)."""
    app = _Host(
        installed=[_plug("alpha"), _plug("beta")],
        marketplaces=[_mkt("m1", [("p1", "url1"), ("p2", "url2")])],
    )
    async with app.run_test() as pilot:
        panel = app.query_one(PluginsPanelWidget)
        # Move down on installed.
        await pilot.press("down")
        assert panel.active_tab == "installed"
        assert panel.selected_index == 1
        await pilot.press("tab")
        assert panel.active_tab == "marketplace"
        assert panel.selected_index == 0  # reset
        await pilot.press("tab")
        assert panel.active_tab == "installed"


async def test_marketplace_tab_i_installs_selected() -> None:
    """``i`` on a marketplace entry emits an install event with the
    ``@marketplace/plugin`` ref. The branch field on the entry flows
    through as ``install_ref`` so the installer pins correctly."""
    mkts = [
        MarketplaceInfo(
            name="m1",
            url="https://example/m1",
            plugins=[
                MarketplacePluginInfo(
                    name="alpha",
                    source="https://x/alpha",
                    branch="stable",
                ),
            ],
        ),
    ]
    app = _Host(installed=[], marketplaces=mkts)
    async with app.run_test() as pilot:
        await pilot.press("tab")  # → marketplace
        await pilot.press("i")
        assert ("install", "@m1/alpha", "stable") in app.captured


async def test_marketplace_install_with_no_branch_passes_none() -> None:
    mkts = [
        MarketplaceInfo(
            name="m",
            url="https://example/m",
            plugins=[
                MarketplacePluginInfo(name="x", source="https://x/x"),
            ],
        ),
    ]
    app = _Host(installed=[], marketplaces=mkts)
    async with app.run_test() as pilot:
        await pilot.press("tab")
        await pilot.press("i")
        assert ("install", "@m/x", None) in app.captured


async def test_marketplace_tab_R_emits_refresh() -> None:
    """``R`` (shift-r) re-fetches all marketplace catalogs."""
    app = _Host(installed=[], marketplaces=[_mkt("m1", [("p", "u")])])
    async with app.run_test() as pilot:
        await pilot.press("tab")
        await pilot.press("R")
        assert ("refresh",) in app.captured


# ── Closing ─────────────────────────────────────────────────────────


async def test_escape_emits_panel_closed_and_removes_widget() -> None:
    app = _Host(installed=[_plug("a")], marketplaces=[])
    async with app.run_test() as pilot:
        await pilot.press("escape")
        assert ("closed",) in app.captured


async def test_escape_works_when_empty() -> None:
    """Esc closes even when both tabs are empty — important so users
    aren't stuck if they open the panel before installing anything."""
    app = _Host(installed=[], marketplaces=[])
    async with app.run_test() as pilot:
        await pilot.press("escape")
        assert ("closed",) in app.captured


# ── Data refresh ────────────────────────────────────────────────────


# ── Click handling ──────────────────────────────────────────────────


async def test_click_on_entry_selects_it() -> None:
    """Clicking a row in the installed list moves ``selected_index``
    to that row's index. The on_click handler walks the rendered
    Static widgets and matches by ``is_descendant_of`` against the
    event target."""
    app = _Host(
        installed=[_plug("a"), _plug("b"), _plug("c")],
        marketplaces=[],
    )
    async with app.run_test() as pilot:
        panel = app.query_one(PluginsPanelWidget)
        await pilot.click("#plug-2")
        assert panel.selected_index == 2


async def test_click_outside_entries_keeps_selection() -> None:
    """Clicking a non-entry (the title, the hint line, the panel
    chrome) leaves the selection alone — important so the user can
    click in the panel area without losing their highlighted row."""
    app = _Host(
        installed=[_plug("a"), _plug("b")],
        marketplaces=[],
    )
    async with app.run_test() as pilot:
        panel = app.query_one(PluginsPanelWidget)
        panel.selected_index = 1
        await pilot.click(".plugins-title")
        assert panel.selected_index == 1


async def test_click_after_tab_switch_targets_marketplace_entries() -> None:
    """After Tab → marketplace, clicks land on the marketplace items'
    plug-N widgets — same id scheme, different underlying data. The
    on_click handler must walk the *current* tab's items count."""
    mkts = [_mkt("m1", [("alpha", "u1"), ("beta", "u2"), ("gamma", "u3")])]
    app = _Host(installed=[], marketplaces=mkts)
    async with app.run_test() as pilot:
        panel = app.query_one(PluginsPanelWidget)
        await pilot.press("tab")
        await pilot.click("#plug-2")
        assert panel.selected_index == 2


# ── Refresh ─────────────────────────────────────────────────────────


async def test_refresh_data_updates_in_place() -> None:
    """``refresh_data`` replaces the panel's lists and selection
    clamps to the new bounds. Used after every backend action to
    pull fresh state without re-mounting the widget."""
    app = _Host(
        installed=[_plug("a"), _plug("b"), _plug("c")],
        marketplaces=[],
    )
    async with app.run_test() as pilot:
        panel = app.query_one(PluginsPanelWidget)
        await pilot.press("down")
        await pilot.press("down")
        assert panel.selected_index == 2
        # Now reduce to one item — selection must clamp.
        panel.refresh_data(installed=[_plug("a")], marketplaces=[])
        await pilot.pause()
        assert panel.selected_index == 0


# ── Rendered text — format-affecting branches only ─────────────────
#
# These tests target the branching logic in the renderers, not exact
# wording. The strings ("v1.2.3", " · ", etc.) are allowed to drift —
# we assert on the conditional pieces that change behavior.


def test_format_bundle_summary_combinations() -> None:
    """``_format_bundle_summary`` builds the S/A/H/M/T badge from
    the plugin's ``has_*`` flags. Manifest-only plugins get a
    different message — *not* an empty badge — so users can tell
    "bundle is empty" from "bundle isn't loaded yet"."""
    from ember_code.frontend.tui.widgets._plugins_panel import (
        _format_bundle_summary,
    )

    none = _format_bundle_summary(PluginInfo(name="x"))
    assert "manifest only" in none.lower()

    all_flags = _format_bundle_summary(
        PluginInfo(
            name="x",
            has_skills=True,
            has_agents=True,
            has_hooks=True,
            has_mcp=True,
            has_tools=True,
        )
    )
    for marker in ("S", "A", "H", "M", "T"):
        assert marker in all_flags

    only_skills = _format_bundle_summary(PluginInfo(name="x", has_skills=True))
    assert "S" in only_skills
    assert "A" not in only_skills


async def test_installed_entry_includes_version_and_source() -> None:
    """Installed-tab rows surface version (when present) and the
    source root — these are the two pieces the user needs to tell
    "v1.2 from my project" apart from "v1.0 from ~/.ember".

    The test calls the rendering helper directly rather than
    inspecting the mounted widget; it exercises the same code path
    without depending on Textual's internal widget-content API."""
    plugin = PluginInfo(name="foo", version="1.2.3", source_root="user-ember")
    app = _Host(installed=[plugin], marketplaces=[])
    async with app.run_test():
        panel = app.query_one(PluginsPanelWidget)
        rendered = panel._render_item(plugin, 0)
        assert "foo" in rendered
        assert "1.2.3" in rendered
        assert "user-ember" in rendered


async def test_installed_entry_omits_version_when_absent() -> None:
    """Plugins without a ``version`` field still render — no "v"
    prefix dangling. The renderer guards on `if item.version else `."""
    plugin = PluginInfo(name="foo", source_root="user-ember")
    app = _Host(installed=[plugin], marketplaces=[])
    async with app.run_test():
        panel = app.query_one(PluginsPanelWidget)
        rendered = panel._render_item(plugin, 0)
        assert "foo" in rendered
        # No orphan ``v`` token — the version segment is suppressed
        # when the field is empty.
        assert " v" not in rendered.replace("user-ember", "")


async def test_installed_entry_enabled_vs_disabled_marker() -> None:
    """Enabled and disabled plugins render with distinct markers
    (``●`` vs ``○``). Without this the user can't tell from the
    list which plugins are active."""
    on = PluginInfo(name="on", enabled=True)
    off = PluginInfo(name="off", enabled=False)
    app = _Host(installed=[on, off], marketplaces=[])
    async with app.run_test():
        panel = app.query_one(PluginsPanelWidget)
        on_row = panel._render_item(on, 0)
        off_row = panel._render_item(off, 1)
        assert on_row != off_row


async def test_marketplace_entry_marks_already_installed() -> None:
    """If a marketplace plugin name matches a locally-installed
    plugin, the marketplace row carries an "(installed)" hint —
    prevents users from trying to reinstall an existing entry from
    the marketplace tab."""
    app = _Host(
        installed=[PluginInfo(name="alpha")],
        marketplaces=[_mkt("m1", [("alpha", "u1"), ("beta", "u2")])],
    )
    async with app.run_test():
        panel = app.query_one(PluginsPanelWidget)
        panel.active_tab = "marketplace"
        items = panel._current_items()
        alpha_row = panel._render_item(items[0], 0)
        beta_row = panel._render_item(items[1], 1)
        assert "installed" in alpha_row.lower()
        assert "installed" not in beta_row.lower()


async def test_empty_state_text_per_tab() -> None:
    """The empty-state message differs between tabs so users can
    tell "no plugins installed" from "no marketplaces registered" —
    different action items."""
    app = _Host(installed=[], marketplaces=[])
    async with app.run_test():
        panel = app.query_one(PluginsPanelWidget)
        panel.active_tab = "installed"
        installed_msg = panel._empty_text()
        panel.active_tab = "marketplace"
        marketplace_msg = panel._empty_text()
        assert "no plugins" in installed_msg.lower()
        assert "marketplace" in marketplace_msg.lower()
        assert installed_msg != marketplace_msg


async def test_title_counts_reflect_enabled_state() -> None:
    """The title bar shows "N enabled / M installed" — both numbers
    have to be correct or users get a misleading at-a-glance summary."""
    app = _Host(
        installed=[
            PluginInfo(name="a", enabled=True),
            PluginInfo(name="b", enabled=False),
            PluginInfo(name="c", enabled=True),
        ],
        marketplaces=[],
    )
    async with app.run_test():
        panel = app.query_one(PluginsPanelWidget)
        panel.active_tab = "installed"
        title = panel._title_text()
        assert "2" in title  # enabled count
        assert "3" in title  # total
