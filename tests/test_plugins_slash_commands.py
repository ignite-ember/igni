"""Tests for the ``/plugin`` and ``/plugins`` slash commands.

These cover the parsing + dispatch logic in
``backend/command_handler.py``: which subcommand routes to which
backend call, how ``--ref`` is extracted, how marketplace refs vs
URLs are differentiated, and what error messages surface for the
unhappy paths. Real loader + state + installer used end-to-end where
possible; module-level patches only for git operations so the tests
don't need network access.

The slash command layer was the single biggest untested surface —
roughly 250 lines of argument parsing in ``_cmd_plugin`` /
``_cmd_plugins``. A regression there ships silently.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from ember_code.backend.command_handler import (
    CommandHandler,
)
from ember_code.core.session.core import PluginReloadCounts
from ember_code.core.plugins.loader import PluginLoader
from ember_code.core.plugins.state import PluginsState, load_state

# ── Helpers ─────────────────────────────────────────────────────────


def _write_plugin(root: Path, name: str, version: str = "1.0.0") -> Path:
    """Plant a minimal plugin at ``root/<name>/`` for slash-command tests.

    Slash command logic doesn't care about bundled skills/agents — just
    that ``list_plugins`` / ``get(name)`` resolves the name. So the
    fixture is intentionally bare.
    """
    plugin_dir = root / name
    (plugin_dir / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": version}),
        encoding="utf-8",
    )
    return plugin_dir


def _make_handler(
    tmp_path: Path,
    *,
    plugins: list[str] | None = None,
    disabled: list[str] | None = None,
) -> CommandHandler:
    """Construct a CommandHandler over a mock session whose plugin
    loader + state point at a real on-disk plugin layout under
    *tmp_path*. Real PluginLoader and state file — only the wider
    Session is a MagicMock since slash commands only touch a slice
    of it."""
    user_ember = tmp_path / "home" / ".ember" / "plugins"
    for name in plugins or []:
        _write_plugin(user_ember, name)

    loader = PluginLoader()
    with patch.object(Path, "home", return_value=tmp_path / "home"):
        loader.load_all(project_dir=tmp_path / "proj")

    state = PluginsState(disabled=disabled or [])
    session = MagicMock()
    session.plugin_loader = loader
    session.plugin_state = state
    session.settings.storage.data_dir = str(tmp_path / "ember")
    # ``reload_plugins`` returns a :class:`PluginReloadCounts` that
    # the install / update / remove paths interpolate into the chat
    # message. The session is a MagicMock so we have to set the
    # return value explicitly — otherwise the attribute access on
    # a MagicMock would render a MagicMock repr instead of a number.
    session.reload_plugins.return_value = PluginReloadCounts(
        plugins=0, skills=0, agents=0, hooks=0
    )

    return CommandHandler(session)


def _run(coro):
    return asyncio.run(coro)


# ── /plugins (bare) ─────────────────────────────────────────────────


def test_plugins_bare_opens_panel(tmp_path: Path) -> None:
    """`/plugins` with no args returns the ``plugins`` action so the
    frontend opens the Textual panel — *not* a markdown listing."""
    h = _make_handler(tmp_path)
    result = _run(h.handle("/plugins"))
    assert result.kind == "action"
    assert result.action == "plugins"


def test_plugins_bare_when_loader_missing_returns_info(tmp_path: Path) -> None:
    """Defensive: a Session without ``plugin_loader`` (shouldn't
    happen in real use) gets a clear info message rather than a
    crash. Same for ``plugin_state``."""
    session = MagicMock()
    session.plugin_loader = None
    session.plugin_state = None
    h = CommandHandler(session)
    result = _run(h.handle("/plugins"))
    assert result.kind == "info"
    assert "not initialized" in result.content.lower()


# ── /plugins enable / disable ──────────────────────────────────────


def test_plugins_disable_persists_and_returns_info(tmp_path: Path) -> None:
    """`/plugins disable <name>` adds the name to state.disabled,
    saves, and returns an info result that names the affected
    subsystems (replaces the old "requires restart" message now
    that hot-reload handles disable end-to-end)."""
    h = _make_handler(tmp_path, plugins=["alpha"])
    result = _run(h.handle("/plugins disable alpha"))
    assert result.kind == "info"
    assert "disabled" in result.content
    assert "skills" in result.content.lower()
    # State persisted.
    persisted = load_state(data_dir=tmp_path / "ember")
    assert persisted.disabled == ["alpha"]


def test_plugins_enable_persists_and_returns_info(tmp_path: Path) -> None:
    h = _make_handler(tmp_path, plugins=["alpha"], disabled=["alpha"])
    result = _run(h.handle("/plugins enable alpha"))
    assert result.kind == "info"
    assert "enabled" in result.content
    persisted = load_state(data_dir=tmp_path / "ember")
    assert "alpha" not in persisted.disabled


def test_plugins_enable_unknown_returns_error(tmp_path: Path) -> None:
    """Toggling a plugin that isn't installed is a user error — surface
    a clear message naming the plugin so they catch typos quickly."""
    h = _make_handler(tmp_path, plugins=["alpha"])
    result = _run(h.handle("/plugins enable nope"))
    assert result.kind == "error"
    assert "nope" in result.content


def test_plugins_disable_without_name_errors(tmp_path: Path) -> None:
    h = _make_handler(tmp_path)
    result = _run(h.handle("/plugins disable"))
    assert result.kind == "error"
    assert "Usage" in result.content


def test_plugins_enable_already_enabled_is_noop(tmp_path: Path) -> None:
    """Re-enabling an already-enabled plugin returns ``info`` with a
    no-op message — *not* error. The state file isn't rewritten."""
    h = _make_handler(tmp_path, plugins=["alpha"])
    result = _run(h.handle("/plugins enable alpha"))
    assert result.kind == "info"
    assert "already enabled" in result.content


def test_plugins_disable_already_disabled_is_noop(tmp_path: Path) -> None:
    h = _make_handler(tmp_path, plugins=["alpha"], disabled=["alpha"])
    result = _run(h.handle("/plugins disable alpha"))
    assert result.kind == "info"
    assert "already disabled" in result.content


def test_plugins_unknown_subcommand_returns_error(tmp_path: Path) -> None:
    h = _make_handler(tmp_path, plugins=["alpha"])
    result = _run(h.handle("/plugins random-thing"))
    assert result.kind == "error"
    assert "Unknown" in result.content


# ── /plugin install ────────────────────────────────────────────────


def test_plugin_install_url_calls_installer(tmp_path: Path) -> None:
    """`/plugin install <git-url>` routes to ``PluginInstaller.install``
    with the URL verbatim and no ``ref``."""
    h = _make_handler(tmp_path)
    with patch("ember_code.backend.command_handler.PluginInstaller") as mock_cls:
        installer = mock_cls.return_value
        installer.is_git_available.return_value = True
        manifest = MagicMock()
        manifest.name = "foo"
        manifest.version = "1.0.0"
        installer.install.return_value = manifest
        result = _run(h.handle("/plugin install https://x/y.git"))
    # Bare-URL installs pass ``subdir=None`` since the plugin lives
    # at the clone root.
    installer.install.assert_called_once_with("https://x/y.git", ref=None, subdir=None)
    assert result.kind == "info"
    assert "foo" in result.content
    assert "v1.0.0" in result.content


def test_plugin_install_extracts_ref_flag(tmp_path: Path) -> None:
    """``--ref <value>`` is parsed out and forwarded to the installer
    regardless of position relative to the URL."""
    h = _make_handler(tmp_path)
    with patch("ember_code.backend.command_handler.PluginInstaller") as mock_cls:
        installer = mock_cls.return_value
        installer.is_git_available.return_value = True
        installer.install.return_value = MagicMock(name="m", version="", model_construct=None)
        installer.install.return_value.name = "m"
        installer.install.return_value.version = ""
        _run(h.handle("/plugin install https://x/y.git --ref v1.2.0"))
    installer.install.assert_called_once_with("https://x/y.git", ref="v1.2.0", subdir=None)


def test_plugin_install_marketplace_ref_resolves_first(tmp_path: Path) -> None:
    """`@<marketplace>/<plugin>` is resolved via the marketplace
    registry into a git URL *before* hitting the installer. The
    catalog's ``branch`` field flows through as ``--ref`` when no
    explicit ``--ref`` was given on the command line."""
    from ember_code.core.plugins.marketplaces import ResolvedSource

    h = _make_handler(tmp_path)
    fake_entry = MagicMock()
    fake_entry.branch = "release-1"

    with (
        patch("ember_code.backend.command_handler.PluginInstaller") as mock_cls,
        patch("ember_code.backend.command_handler.resolve_install_ref") as mock_resolve,
    ):
        installer = mock_cls.return_value
        installer.is_git_available.return_value = True
        installer.install.return_value = MagicMock(version="")
        installer.install.return_value.name = "p"
        # Resolver now returns a ``ResolvedSource`` (with the catalog's
        # branch promoted into the ``ref`` field) plus the entry.
        mock_resolve.return_value = (
            ResolvedSource(
                kind="url",
                url="https://resolved/x.git",
                subdir=None,
                ref="release-1",
            ),
            fake_entry,
        )
        _run(h.handle("/plugin install @market/p"))

    mock_resolve.assert_called_once()
    installer.install.assert_called_once_with(
        "https://resolved/x.git",
        ref="release-1",
        subdir=None,
    )


def test_plugin_install_marketplace_explicit_ref_wins(tmp_path: Path) -> None:
    """If the user passes ``--ref X`` on a marketplace install, it
    takes precedence over the catalog's default branch — explicit
    user choice always wins over inferred metadata."""
    from ember_code.core.plugins.marketplaces import ResolvedSource

    h = _make_handler(tmp_path)
    fake_entry = MagicMock()
    fake_entry.branch = "default-branch"

    with (
        patch("ember_code.backend.command_handler.PluginInstaller") as mock_cls,
        patch("ember_code.backend.command_handler.resolve_install_ref") as mock_resolve,
    ):
        installer = mock_cls.return_value
        installer.is_git_available.return_value = True
        installer.install.return_value = MagicMock(version="")
        installer.install.return_value.name = "p"
        mock_resolve.return_value = (
            ResolvedSource(
                kind="url",
                url="https://x.git",
                subdir=None,
                ref="default-branch",
            ),
            fake_entry,
        )
        _run(h.handle("/plugin install @market/p --ref user-pin"))

    installer.install.assert_called_once_with(
        "https://x.git",
        ref="user-pin",
        subdir=None,
    )


def test_plugin_install_marketplace_unresolved_errors(tmp_path: Path) -> None:
    """Unknown marketplace OR unknown plugin name → error with a hint
    pointing at ``/plugin marketplace list``. The installer is not
    invoked at all."""
    h = _make_handler(tmp_path)
    with (
        patch("ember_code.backend.command_handler.PluginInstaller") as mock_cls,
        patch("ember_code.backend.command_handler.resolve_install_ref") as mock_resolve,
    ):
        installer = mock_cls.return_value
        installer.is_git_available.return_value = True
        mock_resolve.return_value = None
        result = _run(h.handle("/plugin install @unknown/p"))

    assert result.kind == "error"
    assert "marketplace" in result.content.lower()
    installer.install.assert_not_called()


def test_plugin_install_no_git_short_circuits(tmp_path: Path) -> None:
    """When ``git`` isn't on PATH we don't even attempt the install
    — surface a clear hint to install git."""
    h = _make_handler(tmp_path)
    with patch("ember_code.backend.command_handler.PluginInstaller") as mock_cls:
        installer = mock_cls.return_value
        installer.is_git_available.return_value = False
        result = _run(h.handle("/plugin install https://x/y.git"))
    assert result.kind == "error"
    assert "git" in result.content.lower()
    installer.install.assert_not_called()


def test_plugin_install_missing_url_errors(tmp_path: Path) -> None:
    h = _make_handler(tmp_path)
    result = _run(h.handle("/plugin install"))
    assert result.kind == "error"
    assert "Usage" in result.content


def test_plugin_install_too_many_args_errors(tmp_path: Path) -> None:
    """Extra positionals (e.g. two URLs) should error — silent
    discard would hide the user's mistake."""
    h = _make_handler(tmp_path)
    result = _run(h.handle("/plugin install https://x/a.git https://x/b.git"))
    assert result.kind == "error"
    assert "Usage" in result.content


def test_plugin_install_surfaces_git_error(tmp_path: Path) -> None:
    """A ``GitError`` from the installer bubbles up verbatim with a
    ``git error:`` prefix so the user knows what layer failed."""
    from ember_code.core.plugins.git import GitError

    h = _make_handler(tmp_path)
    with patch("ember_code.backend.command_handler.PluginInstaller") as mock_cls:
        installer = mock_cls.return_value
        installer.is_git_available.return_value = True
        installer.install.side_effect = GitError("auth required")
        result = _run(h.handle("/plugin install https://x/y.git"))
    assert result.kind == "error"
    assert "git error" in result.content
    assert "auth required" in result.content


def test_plugin_install_surfaces_plugin_error(tmp_path: Path) -> None:
    """A ``PluginError`` (already-installed, missing manifest, etc.)
    bubbles up directly — these messages are already user-friendly so
    no extra prefix is added."""
    from ember_code.core.plugins.installer import PluginError

    h = _make_handler(tmp_path)
    with patch("ember_code.backend.command_handler.PluginInstaller") as mock_cls:
        installer = mock_cls.return_value
        installer.is_git_available.return_value = True
        installer.install.side_effect = PluginError("already installed")
        result = _run(h.handle("/plugin install https://x/y.git"))
    assert result.kind == "error"
    assert "already installed" in result.content


# ── /plugin update ──────────────────────────────────────────────────


def test_plugin_update_calls_installer(tmp_path: Path) -> None:
    h = _make_handler(tmp_path)
    with patch("ember_code.backend.command_handler.PluginInstaller") as mock_cls:
        installer = mock_cls.return_value
        installer.is_git_available.return_value = True
        installer.update.return_value = "a" * 40
        result = _run(h.handle("/plugin update foo"))
    installer.update.assert_called_once_with("foo", ref=None)
    assert result.kind == "info"
    assert "a" * 12 in result.content  # truncated SHA in output


def test_plugin_update_with_ref(tmp_path: Path) -> None:
    h = _make_handler(tmp_path)
    with patch("ember_code.backend.command_handler.PluginInstaller") as mock_cls:
        installer = mock_cls.return_value
        installer.is_git_available.return_value = True
        installer.update.return_value = "b" * 40
        _run(h.handle("/plugin update foo --ref dev"))
    installer.update.assert_called_once_with("foo", ref="dev")


def test_plugin_update_missing_name_errors(tmp_path: Path) -> None:
    h = _make_handler(tmp_path)
    result = _run(h.handle("/plugin update"))
    assert result.kind == "error"
    assert "Usage" in result.content


# ── /plugin remove ──────────────────────────────────────────────────


def test_plugin_remove_calls_installer(tmp_path: Path) -> None:
    h = _make_handler(tmp_path)
    with patch("ember_code.backend.command_handler.PluginInstaller") as mock_cls:
        installer = mock_cls.return_value
        installer.is_git_available.return_value = True
        result = _run(h.handle("/plugin remove foo"))
    installer.remove.assert_called_once_with("foo")
    assert result.kind == "info"
    assert "foo" in result.content


def test_plugin_remove_missing_returns_error(tmp_path: Path) -> None:
    """The installer raises ``PluginError`` for a missing plugin —
    bubbled up to the user verbatim."""
    from ember_code.core.plugins.installer import PluginError

    h = _make_handler(tmp_path)
    with patch("ember_code.backend.command_handler.PluginInstaller") as mock_cls:
        installer = mock_cls.return_value
        installer.is_git_available.return_value = True
        installer.remove.side_effect = PluginError("not installed")
        result = _run(h.handle("/plugin remove nope"))
    assert result.kind == "error"
    assert "not installed" in result.content


# ── /plugin marketplace ────────────────────────────────────────────


def test_plugin_marketplace_add_calls_add_marketplace(tmp_path: Path) -> None:
    h = _make_handler(tmp_path)
    fake_entry = MagicMock()
    fake_entry.name = "mkt"
    fake_entry.cached = MagicMock()
    fake_entry.cached.plugins = [MagicMock(), MagicMock(), MagicMock()]
    with patch("ember_code.backend.command_handler.add_marketplace") as mock_add:
        mock_add.return_value = fake_entry
        result = _run(h.handle("/plugin marketplace add https://m/k.git"))
    mock_add.assert_called_once()
    # Positional arg or kwarg ok — we just want the URL forwarded.
    args, kwargs = mock_add.call_args
    assert "https://m/k.git" in (args + tuple(kwargs.values()))
    assert result.kind == "info"
    assert "mkt" in result.content
    assert "3" in result.content  # plugin count


def test_plugin_marketplace_add_missing_url(tmp_path: Path) -> None:
    h = _make_handler(tmp_path)
    result = _run(h.handle("/plugin marketplace add"))
    assert result.kind == "error"
    assert "Usage" in result.content


def test_plugin_marketplace_list_empty(tmp_path: Path) -> None:
    """With no marketplaces registered, list returns a markdown hint
    pointing at how to add one — not an error."""
    h = _make_handler(tmp_path)
    result = _run(h.handle("/plugin marketplace list"))
    assert result.kind == "markdown"
    assert "none registered" in result.content
    assert "add" in result.content.lower()


def test_plugin_marketplace_list_renders_entries(tmp_path: Path) -> None:
    h = _make_handler(tmp_path)
    fake_registry = MagicMock()
    fake_entry = MagicMock()
    fake_entry.name = "mkt"
    fake_entry.url = "https://m/k.git"
    fake_entry.last_fetched = "2026-05-28T10:00:00+00:00"
    fake_entry.cached = MagicMock()
    fake_entry.cached.plugins = [MagicMock(), MagicMock()]
    fake_registry.marketplaces = [fake_entry]
    with patch("ember_code.backend.command_handler.load_registry") as mock_load:
        mock_load.return_value = fake_registry
        result = _run(h.handle("/plugin marketplace list"))
    assert result.kind == "markdown"
    assert "mkt" in result.content
    assert "https://m/k.git" in result.content
    assert "2 plugin" in result.content


def test_plugin_marketplace_remove_calls_remove(tmp_path: Path) -> None:
    h = _make_handler(tmp_path)
    with patch("ember_code.backend.command_handler.remove_marketplace") as mock_remove:
        mock_remove.return_value = True
        result = _run(h.handle("/plugin marketplace remove mkt"))
    mock_remove.assert_called_once()
    assert result.kind == "info"
    assert "mkt" in result.content


def test_plugin_marketplace_remove_unknown_errors(tmp_path: Path) -> None:
    h = _make_handler(tmp_path)
    with patch("ember_code.backend.command_handler.remove_marketplace") as mock_remove:
        mock_remove.return_value = False
        result = _run(h.handle("/plugin marketplace remove ghost"))
    assert result.kind == "error"
    assert "ghost" in result.content


def test_plugin_marketplace_refresh_one(tmp_path: Path) -> None:
    h = _make_handler(tmp_path)
    fake_entry = MagicMock()
    fake_entry.name = "mkt"
    fake_entry.cached = MagicMock()
    fake_entry.cached.plugins = [MagicMock(), MagicMock()]
    with patch("ember_code.backend.command_handler.refresh_marketplace") as mock_refresh:
        mock_refresh.return_value = fake_entry
        result = _run(h.handle("/plugin marketplace refresh mkt"))
    assert result.kind == "info"
    assert "mkt" in result.content
    # Forwarded the name argument.
    args, kwargs = mock_refresh.call_args
    assert "mkt" in (args + tuple(kwargs.values()))


def test_plugin_marketplace_refresh_all(tmp_path: Path) -> None:
    """`/plugin marketplace refresh` (no name) iterates all
    registered marketplaces and reports per-marketplace status as a
    single markdown block."""
    h = _make_handler(tmp_path)
    fake_registry = MagicMock()
    e1 = MagicMock()
    e1.name = "m1"
    e2 = MagicMock()
    e2.name = "m2"
    fake_registry.marketplaces = [e1, e2]
    with (
        patch("ember_code.backend.command_handler.load_registry") as mock_load,
        patch("ember_code.backend.command_handler.refresh_marketplace") as mock_refresh,
    ):
        mock_load.return_value = fake_registry
        result = _run(h.handle("/plugin marketplace refresh"))
    # Both marketplaces refreshed.
    assert mock_refresh.call_count == 2
    assert result.kind == "markdown"


def test_plugin_marketplace_unknown_action(tmp_path: Path) -> None:
    h = _make_handler(tmp_path)
    result = _run(h.handle("/plugin marketplace flarghbiscuit"))
    assert result.kind == "error"
    assert "Unknown" in result.content


def test_plugin_marketplace_empty(tmp_path: Path) -> None:
    h = _make_handler(tmp_path)
    result = _run(h.handle("/plugin marketplace"))
    assert result.kind == "error"
    assert "Usage" in result.content


# ── /plugin (bare) and unknowns ────────────────────────────────────


def test_plugin_bare_returns_usage(tmp_path: Path) -> None:
    h = _make_handler(tmp_path)
    result = _run(h.handle("/plugin"))
    assert result.kind == "error"
    assert "Usage" in result.content


def test_plugin_unknown_subcommand(tmp_path: Path) -> None:
    h = _make_handler(tmp_path)
    result = _run(h.handle("/plugin frobnicate foo"))
    assert result.kind == "error"
    assert "Unknown" in result.content
