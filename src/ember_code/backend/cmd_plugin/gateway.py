"""Backend gateway — the single seam for plugin / marketplace ops.

Every verb in :mod:`ember_code.backend.cmd_plugin.verbs` delegates
through :class:`PluginBackendGateway`. Two goals:

1. **Kill the "reach into command_handler module" service-locator.**
   The old god-coordinator held a cached
   ``self._cmd_module = command_handler`` reference and called
   ``self._cmd_module.PluginInstaller(...)`` /
   ``self._cmd_module.resolve_install_ref(...)`` at every verb site.
   That existed only because the test suite patched
   ``ember_code.backend.command_handler.PluginInstaller`` etc. The
   gateway replaces that with a proper class-level seam: tests
   either patch :meth:`PluginBackendGateway.install` (etc.) directly
   or patch ``ember_code.backend.cmd_plugin.gateway.PluginInstaller`` /
   ``resolve_install_ref`` / ``add_marketplace`` / …
   The module-attribute-access pattern (``_plugin_installer.PluginInstaller``)
   mirrors :mod:`ember_code.backend.plugin_controller` so
   patch-at-source still resolves.

2. **Isolate try/except.** Every ``GitError`` / ``PluginError`` /
   ``ValueError`` / ``OSError`` from the core plugin modules is
   caught here and turned into a Result model. Verbs never see raised
   exceptions — they match on ``result.ok`` and render.

Constructed per-command by :class:`~ember_code.backend.cmd_plugin.commands.PluginCommand`
et al. Not cached on the SlashCommand instance — synthesis note (b)
requires fresh construction so a mid-session settings reload flows
through the ``data_dir`` snapshot passed to each installer /
marketplace call.
"""

from __future__ import annotations

from ember_code.backend.plugin_schemas import (
    AddMarketplaceResult,
    InstallResult,
    RefreshOneResult,
    RemoveResult,
    ResolvedInstallRef,
    UpdateResult,
)

# Module-attribute imports (not ``from ... import ClassName``) so
# tests patching ``ember_code.backend.cmd_plugin.gateway.PluginInstaller``
# / ``resolve_install_ref`` / ``add_marketplace`` / ``load_registry``
# / ``refresh_marketplace`` / ``remove_marketplace`` take effect
# on our call sites. Matches the pattern in
# :mod:`ember_code.backend.plugin_controller`.
from ember_code.core.plugins import installer as _plugin_installer
from ember_code.core.plugins import marketplaces as _plugin_marketplaces
from ember_code.core.plugins.git import GitError

# Re-exports so tests can patch either
# ``ember_code.backend.cmd_plugin.gateway.PluginInstaller`` or the
# source module directly. The old ``command_handler.PluginInstaller``
# re-export path stays for one release cycle for backward compat
# (see command_handler.py).
PluginInstaller = _plugin_installer.PluginInstaller
PluginError = _plugin_installer.PluginError
resolve_install_ref = _plugin_marketplaces.resolve_install_ref
add_marketplace = _plugin_marketplaces.add_marketplace
load_registry = _plugin_marketplaces.load_registry
refresh_marketplace = _plugin_marketplaces.refresh_marketplace
remove_marketplace = _plugin_marketplaces.remove_marketplace


class GitNotAvailable(RuntimeError):
    """Raised by :meth:`PluginBackendGateway._require_installer` when
    ``git`` isn't on PATH. Caught inside the gateway and returned as
    ``ok=False`` on the caller's Result model — never leaks out."""


class PluginBackendGateway:
    """Facade over
    :mod:`ember_code.core.plugins.installer` +
    :mod:`ember_code.core.plugins.marketplaces`.

    All git + PluginError + OSError + ValueError catches live in
    this one class. Verbs are exception-free.

    Constructor takes ``data_dir`` as a string (already resolved
    from ``session.plugin_data_dir`` by the SlashCommand builder).
    Since gateway instances are per-command, snapshotting the
    data_dir at construction is safe — the "fresh read" invariant
    is preserved by re-constructing the gateway per invocation, not
    by re-reading settings inside each method.
    """

    def __init__(self, data_dir: str) -> None:
        self._data_dir = data_dir

    # ── Preconditions ───────────────────────────────────────────

    def _make_installer(self) -> _plugin_installer.PluginInstaller:
        """Fresh installer per call. Module-attribute access so
        test patches on
        ``ember_code.backend.cmd_plugin.gateway.PluginInstaller``
        (or on the source module) resolve here."""
        # Attribute-lookup on the module — not the top-of-file
        # ``PluginInstaller = _plugin_installer.PluginInstaller``
        # alias — because that alias captures the pre-patch value.
        installer_cls = _plugin_installer.PluginInstaller
        return installer_cls(data_dir=self._data_dir)

    def is_git_available(self) -> bool:
        """Precondition every install/update path checks. Verbs call
        this first and short-circuit with a "install git" hint on
        False, so we don't chase a git error into the installer."""
        return self._make_installer().is_git_available()

    # ── /plugin install / update / remove ──────────────────────

    def resolve_install_ref(self, target: str) -> ResolvedInstallRef | None:
        """Resolve an ``@<marketplace>/<plugin>`` ref to a concrete
        git URL (+ optional subdir + marketplace-declared ref).
        Returns ``None`` when the marketplace or plugin name is not
        registered."""
        resolved = _plugin_marketplaces.resolve_install_ref(target, data_dir=self._data_dir)
        if resolved is None:
            return None
        resolved_source, _entry = resolved
        return ResolvedInstallRef(
            url=resolved_source.url,
            subdir=resolved_source.subdir,
            ref=resolved_source.ref,
        )

    def install(self, url: str, *, ref: str | None, subdir: str | None) -> InstallResult:
        """Install a plugin. Wraps :meth:`PluginInstaller.install`
        with the GitError / PluginError branches."""
        installer = self._make_installer()
        try:
            manifest = installer.install(url, ref=ref, subdir=subdir)
        except GitError as e:
            return InstallResult(ok=False, error=f"git error: {e}")
        except _plugin_installer.PluginError as e:
            return InstallResult(ok=False, error=str(e))
        return InstallResult(ok=True, name=manifest.name, version=manifest.version or "")

    def update(self, name: str, *, ref: str | None) -> UpdateResult:
        """Update an installed plugin to a new HEAD (or a specified
        branch/tag/SHA via ``ref``)."""
        installer = self._make_installer()
        try:
            new_sha = installer.update(name, ref=ref)
        except GitError as e:
            return UpdateResult(ok=False, error=f"git error: {e}")
        except _plugin_installer.PluginError as e:
            return UpdateResult(ok=False, error=str(e))
        return UpdateResult(ok=True, sha=new_sha)

    def remove(self, name: str) -> RemoveResult:
        """Uninstall a plugin. Only PluginError is expected (no git
        network I/O here) — anything else propagates as a bug."""
        installer = self._make_installer()
        try:
            installer.remove(name)
        except _plugin_installer.PluginError as e:
            return RemoveResult(ok=False, error=str(e))
        return RemoveResult(ok=True)

    # ── /plugin marketplace add / list / remove / refresh ──────

    def add_marketplace(self, url: str) -> AddMarketplaceResult:
        """Register a marketplace by URL. Enumerated failure surface:
        ``GitError`` (clone), ``PluginError`` (manifest parse),
        ``ValueError`` (bad URL), ``OSError`` (disk full during cache
        write). Unexpected exception types are NOT swallowed — they
        surface as bugs rather than being rendered as a generic
        failure."""
        try:
            entry = _plugin_marketplaces.add_marketplace(url, data_dir=self._data_dir)
        except GitError as e:
            return AddMarketplaceResult(ok=False, error=f"git error: {e}")
        except (_plugin_installer.PluginError, ValueError, OSError) as e:
            return AddMarketplaceResult(ok=False, error=f"Failed to add marketplace: {e}")
        count = len(entry.cached.plugins) if entry.cached else 0
        return AddMarketplaceResult(ok=True, name=entry.name, plugin_count=count)

    def list_marketplaces(self):
        """Load the full :class:`MarketplaceRegistry`. Verbs render
        the markdown listing themselves — no wrapping Result needed
        because ``load_registry`` never raises for a missing file
        (returns an empty registry)."""
        return _plugin_marketplaces.load_registry(data_dir=self._data_dir)

    def remove_marketplace(self, name: str) -> bool:
        """Unregister a marketplace. Returns ``True`` on success,
        ``False`` when no marketplace by that name is registered.
        No exceptions expected — the remove path is filesystem-only."""
        return _plugin_marketplaces.remove_marketplace(name, data_dir=self._data_dir)

    def refresh_marketplace(self, name: str) -> RefreshOneResult:
        """Refresh one marketplace by name. Distinguishes
        "not registered" (``not_found=True``) from a git failure
        (``ok=False`` + ``error``) so the verb can surface distinct
        messages."""
        try:
            refreshed = _plugin_marketplaces.refresh_marketplace(name, data_dir=self._data_dir)
        except GitError as e:
            return RefreshOneResult(ok=False, error=f"git error: {e}")
        except Exception as e:  # noqa: BLE001
            # Enumerated failure surface for the marketplaces module
            # is broader than the installer's — the underlying store
            # can raise on parse/validate. Fold into a generic
            # ``Refresh failed:`` prefix rather than typing the
            # exception in the user surface.
            return RefreshOneResult(ok=False, error=f"Refresh failed: {e}")
        if refreshed is None:
            return RefreshOneResult(ok=False, not_found=True, name=name)
        # ``str(...)`` here — the marketplace layer returns a
        # :class:`MarketplaceEntry` whose ``name`` is always a str
        # in production, but test fixtures hand us MagicMocks and
        # pydantic would reject the raw attribute. Explicit cast is
        # cheap and preserves the real code path. ``plugin_count``
        # falls back to 0 when the cached-plugins collection isn't
        # a real list (again, MagicMock resilience).
        try:
            count = len(refreshed.cached.plugins) if refreshed.cached else 0
        except TypeError:
            count = 0
        return RefreshOneResult(ok=True, name=str(refreshed.name), plugin_count=count)


__all__ = ["PluginBackendGateway", "GitNotAvailable"]
