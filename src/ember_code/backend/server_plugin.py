"""Plugin + marketplace RPCs.

Extracted from :mod:`ember_code.backend.server`. Ten free
functions taking ``BackendServer`` as arg — the class holds
one-line delegates:

* :func:`preview_plugin` — shallow-clone a non-installed
  plugin into a temp dir, scan it, tear the clone down. Cached
  per (source, branch, subdir) so re-opening the card is
  instant.
* :func:`get_plugin_details` — snapshot every discovered
  plugin for the panel (combines loader state + persisted
  enable/disable list + pinned SHAs).
* :func:`set_plugin_enabled` — toggle + persist + hot-reload.
* :func:`install_plugin` / :func:`update_plugin` /
  :func:`remove_plugin` — git-backed install/update/uninstall
  with hot-reload afterwards.
* :func:`get_marketplaces` / :func:`add_marketplace` /
  :func:`remove_marketplace` / :func:`refresh_marketplaces` —
  marketplace registry CRUD + bulk refresh.

Rule 2 clean — all inline imports hoisted to module top.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from ember_code.backend.server_helpers import _scan_plugin_dir

# NOTE: Rule 2 (no inline imports) has a documented exception for
# test patchability. Tests patch symbols at the source module
# (``ember_code.core.plugins.installer.PluginInstaller``, etc.)
# — importing the *modules* here and looking up attributes at
# call time keeps those patches effective, which
# ``from ... import PluginInstaller`` at module top would break.
from ember_code.core.plugins import installer as _plugin_installer
from ember_code.core.plugins import marketplaces as _plugin_marketplaces
from ember_code.core.plugins.git import GitClient, GitError
from ember_code.core.plugins.models import (
    MarketplaceInfo,
    MarketplacePluginInfo,
    PluginInfo,
)
from ember_code.core.plugins import state as _plugin_state
from ember_code.protocol import messages as msg

if TYPE_CHECKING:
    from ember_code.backend.server import BackendServer


async def preview_plugin(
    backend: "BackendServer",
    source: str,
    branch: str | None = None,
    subdir: str | None = None,
) -> PluginContents:
    """Same inventory as :meth:`get_plugin_contents`, but for a
    plugin that ISN'T installed yet — performs a shallow clone of
    *source* to a temp dir, scans it, and deletes the clone. Cached
    per (source, branch, subdir) for the lifetime of this backend
    so re-opening the card is instant.
    """
    # The marketplace panel sends ``source`` as the formatted
    # display string from ``get_marketplaces`` — bare URL or the
    # subdir form ``"<url> [<subdir>]"``. Split it back so we
    # clone the right URL and descend into the right path.
    m = re.match(r"^(.+?)\s+\[(.+?)\]\s*$", source.strip())
    if m and not subdir:
        clone_url = m.group(1).strip()
        subdir = m.group(2).strip()
    else:
        clone_url = source.strip()

    key = (clone_url, branch or "", subdir or "")
    # Lazy-initialize the preview cache on first access.
    preview_cache: dict[tuple[str, str, str], PluginContents] = (
        getattr(backend, "_preview_cache", None) or {}
    )
    if not hasattr(backend, "_preview_cache"):
        backend._preview_cache = preview_cache
    if key in preview_cache:
        return preview_cache[key]

    git = GitClient()
    if not git.is_available():
        return PluginContents(error="git is not installed on this machine.")

    tmp = Path(tempfile.mkdtemp(prefix="ember-preview-"))
    try:
        await asyncio.to_thread(git.clone, clone_url, tmp, ref=branch or None, shallow=True)
        scan_root = tmp / subdir if subdir else tmp
        if not scan_root.is_dir():
            return PluginContents(
                error=(
                    f"Cloned repo has no '{subdir}' subdirectory — "
                    "the marketplace entry may be stale."
                )
            )
        result = _scan_plugin_dir(scan_root, name=source)
        # Don't leak the throwaway temp path — surface the source
        # the user knows about instead. Echo the subdir form so
        # the FE display matches the catalog entry.
        result.root_path = f"{clone_url} [{subdir}]" if subdir else clone_url
        preview_cache[key] = result
        return result
    except GitError as exc:
        return PluginContents(error=f"git clone failed: {exc}")
    except Exception as exc:
        return PluginContents(error=str(exc))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def get_plugin_details(backend: "BackendServer") -> list[PluginInfo]:
    """Snapshot of every discovered plugin for the panel UI.

    Combines :class:`PluginLoader` discovery state with the
    persisted enable/disable list and pinned-SHA map so the panel
    can render counts, version, source root, and toggle status
    without any further RPC chatter. Returns typed
    :class:`PluginInfo` models — the wire format is defined in
    :mod:`core.plugins.models` so backend and frontend share the
    same shape.
    """
    loader = backend._session.plugin_loader
    state = backend._session.plugin_state
    disabled = set(state.disabled)
    return [
        PluginInfo(
            name=p.name,
            version=p.manifest.version or "",
            description=p.manifest.description or "",
            source_root=p.source.root,
            path=str(p.root_path),
            # Managed plugins ignore the persisted disable
            # list (see ``Session._disabled_plugins``); reflect
            # that here so the panel shows them enabled and
            # locks the toggle.
            enabled=p.is_managed or p.name not in disabled,
            has_skills=p.has_skills,
            has_agents=p.has_agents,
            has_hooks=p.has_hooks,
            has_mcp=p.has_mcp,
            has_tools=p.has_tools,
            has_lsp=p.has_lsp,
            has_monitors=p.has_monitors,
            managed=p.is_managed,
            pin=state.pins.get(p.name, ""),
        )
        for p in loader.list_plugins()
    ]


def set_plugin_enabled(backend: "BackendServer", name: str, enabled: bool) -> msg.Info:
    """Toggle a plugin's enabled flag, persist, and hot-reload.

    Re-applying the plugin set after the flip means an
    ``enable`` activates the plugin's skills/agents/hooks
    immediately, and a ``disable`` makes them disappear from
    the live session — no restart needed.
    """
    loader = backend._session.plugin_loader
    state = backend._session.plugin_state
    plugin = loader.get(name)
    if plugin is None:
        return msg.Info(text=f"No plugin named '{name}'.")
    if plugin.is_managed and not enabled:
        # Managed plugins are sysadmin-enforced; refuse the
        # disable attempt explicitly so the user knows why
        # rather than seeing a silent no-op.
        return msg.Info(
            text=(
                f"Plugin '{name}' is managed (sysadmin-enforced) and cannot be "
                "disabled. Remove it from the managed plugins directory to "
                "uninstall."
            )
        )

    disabled_set = set(state.disabled)
    if enabled:
        disabled_set.discard(name)
    else:
        disabled_set.add(name)
    state.disabled = sorted(disabled_set)
    _plugin_state.save_state(state, data_dir=backend._session.settings.storage.data_dir)

    # Hot-reload — ``reload_plugins`` re-reads the disabled set
    # from disk and rebuilds skills/agents/hooks/MCP-configs
    # accordingly. The main team rebuilds at the end so tools
    # in the disabled plugin disappear from the agent surface,
    # and any bundled MCP servers are disconnected in the
    # background (auto-symmetric with the enable path).
    backend._session.reload_plugins()
    if enabled:
        tail = (
            "Its skills/agents/hooks/tools are active; any bundled "
            "MCP servers are starting in the background."
        )
    else:
        tail = (
            "Its skills/agents/hooks/tools are no longer active; "
            "any bundled MCP servers are being disconnected."
        )
    verb = "enabled" if enabled else "disabled"
    return msg.Info(text=f"Plugin '{name}' {verb}. {tail}")


def install_plugin(
    backend: "BackendServer",
    ref: str,
    install_ref: str | None = None,
) -> msg.Info:
    """Install a plugin by git URL or ``@<marketplace>/<plugin>`` ref.

    ``install_ref`` (the ``--ref`` flag in the slash command — a
    branch / tag / SHA) is forwarded to the installer. Marketplace
    refs may carry a default ``branch`` in the catalog; honored
    only when ``install_ref`` is omitted so explicit user choice
    wins.
    """
    data_dir = backend._session.settings.storage.data_dir
    installer = _plugin_installer.PluginInstaller(data_dir=data_dir)
    if not installer.is_git_available():
        return msg.Info(text="git not found on PATH. Install git and retry.")

    url = ref
    subdir: str | None = None
    if ref.startswith("@"):
        resolved = _plugin_marketplaces.resolve_install_ref(ref, data_dir=data_dir)
        if resolved is None:
            return msg.Info(
                text=f"Could not resolve '{ref}'. Run "
                "/plugin marketplace list to see registered "
                "marketplaces, or use a git URL."
            )
        resolved_source, _mkt_entry = resolved
        url = resolved_source.url
        subdir = resolved_source.subdir
        if install_ref is None:
            install_ref = resolved_source.ref

    try:
        manifest = installer.install(url, ref=install_ref, subdir=subdir)
    except GitError as e:
        return msg.Info(text=f"git error: {e}")
    except _plugin_installer.PluginError as e:
        return msg.Info(text=str(e))

    version = f" v{manifest.version}" if manifest.version else ""
    # Hot-reload — pull the new plugin's skills / agents / hooks /
    # MCP configs / custom tools into the running session so the
    # user can use them immediately. ``reload_plugins`` rebuilds
    # the main team at the end, and auto-connects any new MCP
    # servers in the background (the existing approval prompt
    # gates first-use, so consent is still required).
    counts = backend._session.reload_plugins()
    return msg.Info(
        text=(
            f"Installed plugin '{manifest.name}'{version}. "
            f"Active now — {counts.skills} skill(s), "
            f"{counts.agents} agent(s), {counts.hooks} hook(s). "
            f"Any bundled MCP servers are starting in the background."
        )
    )


def update_plugin(
    backend: "BackendServer",
    name: str,
    install_ref: str | None = None,
) -> msg.Info:
    """Fetch + reset to ``install_ref`` (default: origin's HEAD)."""
    installer = _plugin_installer.PluginInstaller(
        data_dir=backend._session.settings.storage.data_dir,
    )
    if not installer.is_git_available():
        return msg.Info(text="git not found on PATH.")
    try:
        new_sha = installer.update(name, ref=install_ref)
    except GitError as e:
        return msg.Info(text=f"git error: {e}")
    except _plugin_installer.PluginError as e:
        return msg.Info(text=str(e))
    # Hot-reload so the updated plugin's contents replace the
    # old ones in the live session.
    backend._session.reload_plugins()
    return msg.Info(text=f"Updated '{name}' to {new_sha[:12]}. Active now.")


def remove_plugin(backend: "BackendServer", name: str) -> msg.Info:
    """Delete the plugin directory and clear its pin."""
    installer = _plugin_installer.PluginInstaller(
        data_dir=backend._session.settings.storage.data_dir,
    )
    try:
        installer.remove(name)
    except _plugin_installer.PluginError as e:
        return msg.Info(text=str(e))
    # Hot-reload so the removed plugin's skills/agents/hooks
    # disappear from the live session immediately. Any
    # bundled MCP servers are also disconnected in the
    # background — symmetric with the enable/install path.
    backend._session.reload_plugins()
    return msg.Info(
        text=(
            f"Removed '{name}'. Skills/agents/hooks/tools no "
            "longer active; bundled MCP servers are being "
            "disconnected."
        )
    )


def get_marketplaces(backend: "BackendServer") -> list[MarketplaceInfo]:
    """Snapshot of every registered marketplace for the panel.

    Returns typed :class:`MarketplaceInfo` models (nesting
    :class:`MarketplacePluginInfo` per catalog entry). Same wire
    contract as ``get_plugin_details`` — source-of-truth shape
    lives in :mod:`core.plugins.models`.

    The catalog's raw ``source`` field can be a string OR a dict
    (see :class:`ResolvedSource` for the three official shapes).
    We collapse it to a single human-readable string here so the
    panel's :class:`MarketplacePluginInfo` (which types ``source``
    as ``str`` for display simplicity) doesn't blow up on the
    dict-shaped entries Anthropic's marketplace ships for
    ~75% of its plugins.
    """
    registry = _plugin_marketplaces.load_registry(
        data_dir=backend._session.settings.storage.data_dir,
    )
    out: list[MarketplaceInfo] = []
    for m in registry.marketplaces:
        plugins: list[MarketplacePluginInfo] = []
        for p in m.cached.plugins if m.cached else []:
            resolved = p.resolved_source(m.url)
            # Display string: ``url`` for bare-URL installs,
            # ``url [subdir/path]`` for subdir/relative shapes,
            # or repr(raw) as a last-resort fallback when the
            # entry is so malformed we can't resolve it at all.
            if resolved is None:
                source_display = str(p.source) if p.source else ""
            elif resolved.subdir:
                source_display = f"{resolved.url} [{resolved.subdir}]"
            else:
                source_display = resolved.url
            plugins.append(
                MarketplacePluginInfo(
                    name=p.name,
                    source=source_display,
                    description=p.description or "",
                    version=p.version or "",
                    branch=p.branch or "",
                )
            )
        out.append(
            MarketplaceInfo(
                name=m.name,
                url=m.url,
                last_fetched=m.last_fetched or "",
                plugins=plugins,
            )
        )
    return out


def add_marketplace(backend: "BackendServer", url: str) -> msg.Info:
    try:
        entry = _plugin_marketplaces.add_marketplace(
            url, data_dir=backend._session.settings.storage.data_dir
        )
    except GitError as e:
        return msg.Info(text=f"git error: {e}")
    except Exception as e:  # noqa: BLE001 — surface verbatim
        return msg.Info(text=f"Failed to add marketplace: {e}")
    count = len(entry.cached.plugins) if entry.cached else 0
    return msg.Info(text=f"Added '{entry.name}' ({count} plugin(s) catalogued).")


def remove_marketplace(backend: "BackendServer", name: str) -> msg.Info:
    if not _plugin_marketplaces.remove_marketplace(
        name, data_dir=backend._session.settings.storage.data_dir
    ):
        return msg.Info(text=f"No marketplace named '{name}'.")
    return msg.Info(text=f"Unregistered '{name}'. Installed plugins from it remain.")


def refresh_marketplaces(backend: "BackendServer", name: str | None = None) -> msg.Info:
    """Re-fetch one marketplace or all. Errors per-marketplace are
    collected and reported together so a single bad URL doesn't
    abort the whole refresh."""
    data_dir = backend._session.settings.storage.data_dir
    if name:
        try:
            entry = _plugin_marketplaces.refresh_marketplace(name, data_dir=data_dir)
        except Exception as e:  # noqa: BLE001
            return msg.Info(text=f"Refresh failed for '{name}': {e}")
        if entry is None:
            return msg.Info(text=f"No marketplace named '{name}'.")
        count = len(entry.cached.plugins) if entry.cached else 0
        return msg.Info(text=f"Refreshed '{entry.name}' ({count} plugins).")

    registry = _plugin_marketplaces.load_registry(data_dir=data_dir)
    ok: list[str] = []
    failed: list[str] = []
    for m in registry.marketplaces:
        try:
            _plugin_marketplaces.refresh_marketplace(m.name, data_dir=data_dir)
            ok.append(m.name)
        except Exception as e:  # noqa: BLE001
            failed.append(f"{m.name} ({e})")
    if not ok and not failed:
        return msg.Info(text="No marketplaces to refresh.")
    line = f"Refreshed {len(ok)} ok"
    if failed:
        line += f"; {len(failed)} failed: {', '.join(failed)}"
    return msg.Info(text=line)
