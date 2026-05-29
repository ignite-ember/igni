"""Tests for the plugin RPC methods on ``BackendServer``.

These are thin wrappers around the underlying APIs — installer,
marketplaces, state — but the wrapping logic (marketplace-ref vs
URL detection, branch flow-through, error message shaping, ack
text) is currently the most user-visible surface for plugin
operations in the TUI, and was wholly untested.

Real loader + state under tmp_path; module-level patches only for
``PluginInstaller`` and ``marketplaces`` so we exercise the
wrapping logic without doing real git operations.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from ember_code.backend.server import BackendServer
from ember_code.core.plugins.loader import PluginLoader
from ember_code.core.plugins.state import PluginsState, load_state, save_state

# ── Helpers ─────────────────────────────────────────────────────────


def _write_plugin(root: Path, name: str, **kw) -> Path:
    """Plant a plugin with optional bundled-contents flags."""
    plugin_dir = root / name
    (plugin_dir / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    manifest = {"name": name}
    manifest.update({k: v for k, v in kw.items() if k in ("version", "description", "author")})
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    if kw.get("with_skills"):
        (plugin_dir / "skills").mkdir(exist_ok=True)
    if kw.get("with_agents"):
        (plugin_dir / "agents").mkdir(exist_ok=True)
    if kw.get("with_hooks"):
        (plugin_dir / "hooks").mkdir(exist_ok=True)
        (plugin_dir / "hooks" / "hooks.json").write_text("{}")
    if kw.get("with_mcp"):
        (plugin_dir / ".mcp.json").write_text("{}")
    if kw.get("with_tools"):
        (plugin_dir / "tools").mkdir(exist_ok=True)
    return plugin_dir


def _make_backend(
    tmp_path: Path,
    *,
    plugins: list[tuple[str, dict]] | None = None,
    disabled: list[str] | None = None,
    pins: dict[str, str] | None = None,
) -> BackendServer:
    """Construct a BackendServer over a session whose plugin state
    points at *tmp_path*. Only the slice the plugin methods touch is
    populated — the wider session is a MagicMock."""
    user_ember = tmp_path / "home" / ".ember" / "plugins"
    for name, kw in plugins or []:
        _write_plugin(user_ember, name, **kw)

    loader = PluginLoader()
    with patch.object(Path, "home", return_value=tmp_path / "home"):
        loader.load_all(project_dir=tmp_path / "proj")

    state = PluginsState(disabled=disabled or [], pins=pins or {})
    save_state(state, data_dir=tmp_path / "ember")

    session = MagicMock()
    session.plugin_loader = loader
    session.plugin_state = state
    session.settings.storage.data_dir = str(tmp_path / "ember")

    backend = BackendServer.__new__(BackendServer)
    backend._session = session
    return backend


def _run(coro):
    return asyncio.run(coro)


# ── get_plugin_details ─────────────────────────────────────────────


def test_get_plugin_details_basic(tmp_path: Path) -> None:
    """Returns one ``PluginInfo`` per discovered plugin with the
    fields the panel reads. Shape is the wire contract — the
    backend constructs typed models that the frontend consumes as
    the same model type."""
    backend = _make_backend(
        tmp_path,
        plugins=[("alpha", {"version": "1.2.3", "description": "A test"})],
    )
    details = backend.get_plugin_details()
    assert len(details) == 1
    d = details[0]
    assert d.name == "alpha"
    assert d.version == "1.2.3"
    assert d.description == "A test"
    assert d.enabled is True
    assert d.source_root == "user-ember"
    assert d.pin == ""
    for attr in ("has_skills", "has_agents", "has_hooks", "has_mcp", "has_tools"):
        assert getattr(d, attr) is False


def test_get_plugin_details_reflects_disabled_state(tmp_path: Path) -> None:
    backend = _make_backend(
        tmp_path,
        plugins=[("alpha", {}), ("beta", {})],
        disabled=["alpha"],
    )
    details = {d.name: d for d in backend.get_plugin_details()}
    assert details["alpha"].enabled is False
    assert details["beta"].enabled is True


def test_get_plugin_details_reflects_pins_and_bundles(tmp_path: Path) -> None:
    """Pins from ``plugins.json`` and bundled-contents flags from disk
    both surface on the model — the panel uses them for version
    display and the S/A/H/M/T badge."""
    backend = _make_backend(
        tmp_path,
        plugins=[("alpha", {"with_skills": True, "with_hooks": True})],
        pins={"alpha": "a" * 40},
    )
    d = backend.get_plugin_details()[0]
    assert d.pin == "a" * 40
    assert d.has_skills is True
    assert d.has_hooks is True
    assert d.has_agents is False


def test_get_plugin_details_empty(tmp_path: Path) -> None:
    backend = _make_backend(tmp_path)
    assert backend.get_plugin_details() == []


# ── set_plugin_enabled ─────────────────────────────────────────────


def test_set_plugin_enabled_persists(tmp_path: Path) -> None:
    """Toggle writes the state file and returns an ``Info`` with a
    restart hint. The panel relies on this exact return shape."""
    backend = _make_backend(tmp_path, plugins=[("alpha", {})])
    result = backend.set_plugin_enabled("alpha", False)
    assert "disabled" in result.text
    assert "restart" in result.text.lower()
    persisted = load_state(data_dir=tmp_path / "ember")
    assert persisted.disabled == ["alpha"]


def test_set_plugin_enabled_unknown_plugin(tmp_path: Path) -> None:
    backend = _make_backend(tmp_path)
    result = backend.set_plugin_enabled("ghost", False)
    assert "ghost" in result.text
    # State unchanged.
    persisted = load_state(data_dir=tmp_path / "ember")
    assert persisted.disabled == []


def test_set_plugin_enabled_round_trip(tmp_path: Path) -> None:
    """Disable, then re-enable. Final state should be back to empty
    disabled list — toggling is symmetric."""
    backend = _make_backend(tmp_path, plugins=[("alpha", {})])
    backend.set_plugin_enabled("alpha", False)
    backend.set_plugin_enabled("alpha", True)
    persisted = load_state(data_dir=tmp_path / "ember")
    assert "alpha" not in persisted.disabled


# ── install_plugin ─────────────────────────────────────────────────


def test_install_plugin_url_delegates_to_installer(tmp_path: Path) -> None:
    backend = _make_backend(tmp_path)
    with patch("ember_code.core.plugins.installer.PluginInstaller") as mock_cls:
        installer = mock_cls.return_value
        installer.is_git_available.return_value = True
        manifest = MagicMock()
        manifest.name = "foo"
        manifest.version = "1.0.0"
        installer.install.return_value = manifest
        result = backend.install_plugin("https://x/y.git")
    installer.install.assert_called_once_with("https://x/y.git", ref=None)
    assert "foo" in result.text
    assert "v1.0.0" in result.text


def test_install_plugin_marketplace_ref(tmp_path: Path) -> None:
    """Marketplace ref → resolve → install. Catalog branch flows
    through when no explicit ``install_ref`` is passed."""
    backend = _make_backend(tmp_path)
    fake_entry = MagicMock()
    fake_entry.branch = "release"
    with (
        patch("ember_code.core.plugins.installer.PluginInstaller") as mock_cls,
        patch("ember_code.core.plugins.marketplaces.resolve_install_ref") as mock_resolve,
    ):
        installer = mock_cls.return_value
        installer.is_git_available.return_value = True
        installer.install.return_value = MagicMock(version="")
        installer.install.return_value.name = "p"
        mock_resolve.return_value = ("https://resolved.git", fake_entry)
        backend.install_plugin("@m/p")
    installer.install.assert_called_once_with(
        "https://resolved.git",
        ref="release",
    )


def test_install_plugin_explicit_ref_wins_over_catalog_branch(tmp_path: Path) -> None:
    backend = _make_backend(tmp_path)
    fake_entry = MagicMock()
    fake_entry.branch = "default"
    with (
        patch("ember_code.core.plugins.installer.PluginInstaller") as mock_cls,
        patch("ember_code.core.plugins.marketplaces.resolve_install_ref") as mock_resolve,
    ):
        installer = mock_cls.return_value
        installer.is_git_available.return_value = True
        installer.install.return_value = MagicMock(version="")
        installer.install.return_value.name = "p"
        mock_resolve.return_value = ("https://x.git", fake_entry)
        backend.install_plugin("@m/p", install_ref="user-pin")
    installer.install.assert_called_once_with(
        "https://x.git",
        ref="user-pin",
    )


def test_install_plugin_unresolved_marketplace_ref(tmp_path: Path) -> None:
    """Unresolved ref → ``Info`` with a list hint, no installer call."""
    backend = _make_backend(tmp_path)
    with (
        patch("ember_code.core.plugins.installer.PluginInstaller") as mock_cls,
        patch("ember_code.core.plugins.marketplaces.resolve_install_ref") as mock_resolve,
    ):
        installer = mock_cls.return_value
        installer.is_git_available.return_value = True
        mock_resolve.return_value = None
        result = backend.install_plugin("@ghost/p")
    installer.install.assert_not_called()
    assert "marketplace" in result.text.lower()


def test_install_plugin_no_git(tmp_path: Path) -> None:
    backend = _make_backend(tmp_path)
    with patch("ember_code.core.plugins.installer.PluginInstaller") as mock_cls:
        installer = mock_cls.return_value
        installer.is_git_available.return_value = False
        result = backend.install_plugin("https://x.git")
    assert "git not found" in result.text.lower()
    installer.install.assert_not_called()


def test_install_plugin_surfaces_git_error(tmp_path: Path) -> None:
    from ember_code.core.plugins.git import GitError

    backend = _make_backend(tmp_path)
    with patch("ember_code.core.plugins.installer.PluginInstaller") as mock_cls:
        installer = mock_cls.return_value
        installer.is_git_available.return_value = True
        installer.install.side_effect = GitError("clone failed")
        result = backend.install_plugin("https://x.git")
    assert "git error" in result.text.lower()
    assert "clone failed" in result.text


def test_install_plugin_surfaces_plugin_error(tmp_path: Path) -> None:
    from ember_code.core.plugins.installer import PluginError

    backend = _make_backend(tmp_path)
    with patch("ember_code.core.plugins.installer.PluginInstaller") as mock_cls:
        installer = mock_cls.return_value
        installer.is_git_available.return_value = True
        installer.install.side_effect = PluginError("already installed")
        result = backend.install_plugin("https://x.git")
    assert "already installed" in result.text


# ── update_plugin / remove_plugin ──────────────────────────────────


def test_update_plugin_delegates(tmp_path: Path) -> None:
    backend = _make_backend(tmp_path)
    with patch("ember_code.core.plugins.installer.PluginInstaller") as mock_cls:
        installer = mock_cls.return_value
        installer.is_git_available.return_value = True
        installer.update.return_value = "a" * 40
        result = backend.update_plugin("foo")
    installer.update.assert_called_once_with("foo", ref=None)
    assert "a" * 12 in result.text


def test_update_plugin_with_ref(tmp_path: Path) -> None:
    backend = _make_backend(tmp_path)
    with patch("ember_code.core.plugins.installer.PluginInstaller") as mock_cls:
        installer = mock_cls.return_value
        installer.is_git_available.return_value = True
        installer.update.return_value = "b" * 40
        backend.update_plugin("foo", "dev")
    installer.update.assert_called_once_with("foo", ref="dev")


def test_remove_plugin_delegates(tmp_path: Path) -> None:
    backend = _make_backend(tmp_path)
    with patch("ember_code.core.plugins.installer.PluginInstaller") as mock_cls:
        installer = mock_cls.return_value
        result = backend.remove_plugin("foo")
    installer.remove.assert_called_once_with("foo")
    assert "foo" in result.text
    assert "restart" in result.text.lower()


def test_remove_plugin_surfaces_error(tmp_path: Path) -> None:
    from ember_code.core.plugins.installer import PluginError

    backend = _make_backend(tmp_path)
    with patch("ember_code.core.plugins.installer.PluginInstaller") as mock_cls:
        installer = mock_cls.return_value
        installer.remove.side_effect = PluginError("not installed")
        result = backend.remove_plugin("ghost")
    assert "not installed" in result.text


# ── Marketplace methods ────────────────────────────────────────────


def test_get_marketplaces_empty(tmp_path: Path) -> None:
    backend = _make_backend(tmp_path)
    assert backend.get_marketplaces() == []


def test_get_marketplaces_shape(tmp_path: Path) -> None:
    """Returned models match the schema the panel reads — name, url,
    last_fetched, plugins[]. Typed via ``MarketplaceInfo`` /
    ``MarketplacePluginInfo``."""
    backend = _make_backend(tmp_path)
    fake_registry = MagicMock()
    e = MagicMock()
    e.name = "m1"
    e.url = "https://m1.git"
    e.last_fetched = "2026-05-28T10:00:00Z"
    e.cached = MagicMock()
    p1 = MagicMock()
    p1.name = "alpha"
    p1.source = "https://a.git"
    p1.description = "A"
    p1.version = "1.0"
    p1.branch = "main"
    e.cached.plugins = [p1]
    fake_registry.marketplaces = [e]
    with patch("ember_code.core.plugins.marketplaces.load_registry") as mock_load:
        mock_load.return_value = fake_registry
        out = backend.get_marketplaces()
    assert len(out) == 1
    m = out[0]
    assert m.name == "m1"
    assert m.url == "https://m1.git"
    assert m.last_fetched == "2026-05-28T10:00:00Z"
    assert len(m.plugins) == 1
    p = m.plugins[0]
    assert p.name == "alpha"
    assert p.source == "https://a.git"
    assert p.version == "1.0"
    assert p.branch == "main"


def test_add_marketplace_delegates(tmp_path: Path) -> None:
    backend = _make_backend(tmp_path)
    fake_entry = MagicMock()
    fake_entry.name = "m1"
    fake_entry.cached = MagicMock()
    fake_entry.cached.plugins = [MagicMock(), MagicMock()]
    with patch("ember_code.core.plugins.marketplaces.add_marketplace") as mock_add:
        mock_add.return_value = fake_entry
        result = backend.add_marketplace("https://m.git")
    args, kwargs = mock_add.call_args
    assert "https://m.git" in (args + tuple(kwargs.values()))
    assert "m1" in result.text
    assert "2 plugin" in result.text


def test_add_marketplace_surfaces_git_error(tmp_path: Path) -> None:
    from ember_code.core.plugins.git import GitError

    backend = _make_backend(tmp_path)
    with patch("ember_code.core.plugins.marketplaces.add_marketplace") as mock_add:
        mock_add.side_effect = GitError("auth required")
        result = backend.add_marketplace("https://m.git")
    assert "git error" in result.text.lower()


def test_remove_marketplace_delegates(tmp_path: Path) -> None:
    backend = _make_backend(tmp_path)
    with patch("ember_code.core.plugins.marketplaces.remove_marketplace") as mock_remove:
        mock_remove.return_value = True
        result = backend.remove_marketplace("m1")
    assert "m1" in result.text


def test_remove_marketplace_unknown(tmp_path: Path) -> None:
    backend = _make_backend(tmp_path)
    with patch("ember_code.core.plugins.marketplaces.remove_marketplace") as mock_remove:
        mock_remove.return_value = False
        result = backend.remove_marketplace("ghost")
    assert "ghost" in result.text
    assert "no marketplace" in result.text.lower()


def test_refresh_marketplaces_named(tmp_path: Path) -> None:
    backend = _make_backend(tmp_path)
    fake_entry = MagicMock()
    fake_entry.name = "m1"
    fake_entry.cached = MagicMock()
    fake_entry.cached.plugins = [MagicMock(), MagicMock(), MagicMock()]
    with patch("ember_code.core.plugins.marketplaces.refresh_marketplace") as mock_refresh:
        mock_refresh.return_value = fake_entry
        result = backend.refresh_marketplaces("m1")
    assert "m1" in result.text
    assert "3 plugin" in result.text


def test_refresh_marketplaces_named_unknown(tmp_path: Path) -> None:
    backend = _make_backend(tmp_path)
    with patch("ember_code.core.plugins.marketplaces.refresh_marketplace") as mock_refresh:
        mock_refresh.return_value = None
        result = backend.refresh_marketplaces("ghost")
    assert "ghost" in result.text


def test_refresh_marketplaces_all_aggregates_errors(tmp_path: Path) -> None:
    """A single failed marketplace doesn't abort the rest. The reply
    reports both counts: how many refreshed cleanly, how many
    failed and why."""
    backend = _make_backend(tmp_path)
    fake_registry = MagicMock()
    e1 = MagicMock()
    e1.name = "m1"
    e2 = MagicMock()
    e2.name = "m2"
    fake_registry.marketplaces = [e1, e2]

    def _refresh_side_effect(name, *, data_dir):
        if name == "m1":
            return MagicMock(cached=MagicMock(plugins=[]))
        raise RuntimeError("boom")

    with (
        patch("ember_code.core.plugins.marketplaces.load_registry") as mock_load,
        patch("ember_code.core.plugins.marketplaces.refresh_marketplace") as mock_refresh,
    ):
        mock_load.return_value = fake_registry
        mock_refresh.side_effect = _refresh_side_effect
        result = backend.refresh_marketplaces(None)
    assert "1 ok" in result.text
    assert "1 failed" in result.text
    assert "m2" in result.text  # failure listed by name


def test_refresh_marketplaces_all_empty_registry(tmp_path: Path) -> None:
    backend = _make_backend(tmp_path)
    fake_registry = MagicMock()
    fake_registry.marketplaces = []
    with patch("ember_code.core.plugins.marketplaces.load_registry") as mock_load:
        mock_load.return_value = fake_registry
        result = backend.refresh_marketplaces(None)
    assert "no marketplaces" in result.text.lower()
