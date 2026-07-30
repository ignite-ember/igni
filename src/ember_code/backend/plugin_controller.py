"""Plugin lifecycle controller — plugin-panel RPCs on one class.

* :meth:`preview` — shallow-clone a non-installed plugin into a
  temp dir, scan it, tear the clone down. Cached per
  :class:`PluginPreviewKey` on the controller instance.
* :meth:`list_installed` — snapshot of every discovered plugin.
* :meth:`set_enabled` — toggle + persist + hot-reload.
* :meth:`install` / :meth:`update` / :meth:`remove` — git-backed
  lifecycle with hot-reload afterwards.

Session attributes (``plugin_loader``, ``plugin_state``,
``settings.storage.data_dir``) are read fresh inside each method,
never cached in ``__init__``, so callers that mutate the session
after construction see the updates.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from ember_code.backend.plugin_schemas import (
    PluginContents,
    PluginPreviewKey,
    PluginPreviewSource,
)

# Module-attribute imports (not ``from ... import ClassName``) so
# tests patching ``installer.PluginInstaller`` / ``state.save_state``
# at the source module take effect on our call sites.
from ember_code.core.plugins import installer as _plugin_installer
from ember_code.core.plugins import marketplaces as _plugin_marketplaces
from ember_code.core.plugins import state as _plugin_state
from ember_code.core.plugins.git import GitClient, GitError
from ember_code.core.plugins.models import PluginInfo
from ember_code.protocol import messages as msg

if TYPE_CHECKING:
    from ember_code.core.session import Session


class PluginController:
    """Plugin lifecycle: preview / list / toggle / install / update /
    remove. Composed onto :class:`BackendServer` as ``self.plugins``.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._preview_cache: dict[PluginPreviewKey, PluginContents] = {}

    # ── Preview (uninstalled marketplace card) ─────────────────────

    async def preview(
        self,
        source: str,
        branch: str | None = None,
        subdir: str | None = None,
    ) -> PluginContents:
        """Same inventory as :meth:`BackendServer.get_plugin_contents`
        but for a plugin that ISN'T installed yet — performs a
        shallow clone of *source* to a temp dir, scans it, and
        deletes the clone. Cached per
        :class:`PluginPreviewKey` for the lifetime of this
        controller so re-opening the card is instant.
        """
        # The marketplace panel sends ``source`` as a bare URL or
        # the subdir form ``"<url> [<subdir>]"``; parse it back to
        # separate clone URL and subdir.
        parsed = PluginPreviewSource.parse(source)
        clone_url = parsed.url
        if parsed.subdir and not subdir:
            subdir = parsed.subdir

        key = PluginPreviewKey(
            clone_url=clone_url,
            branch=branch or "",
            subdir=subdir or "",
        )
        if key in self._preview_cache:
            return self._preview_cache[key]

        git = GitClient()
        if not git.is_available():
            return PluginContents.error_result("git is not installed on this machine.")

        tmp = Path(tempfile.mkdtemp(prefix="ember-preview-"))
        try:
            await asyncio.to_thread(git.clone, clone_url, tmp, ref=branch or None, shallow=True)
            scan_root = tmp / subdir if subdir else tmp
            if not scan_root.is_dir():
                return PluginContents.error_result(
                    f"Cloned repo has no '{subdir}' subdirectory — "
                    "the marketplace entry may be stale."
                )
            result = PluginContents.from_directory(scan_root, name=source)
            # Don't leak the throwaway temp path — surface the
            # source the user knows about instead. Echo the subdir
            # form so the FE display matches the catalog entry.
            preview_source = PluginPreviewSource(url=clone_url, subdir=subdir or "")
            result.root_path = preview_source.display()
            result.preview_source = preview_source
            self._preview_cache[key] = result
            return result
        except GitError as exc:
            return PluginContents.error_result(f"git clone failed: {exc}")
        except Exception as exc:
            return PluginContents.error_result(str(exc))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ── Installed plugin listing ────────────────────────────────────

    def list_installed(self) -> list[PluginInfo]:
        """Snapshot of every discovered plugin for the panel UI.

        Combines :class:`PluginLoader` discovery state with the
        persisted enable/disable list and pinned-SHA map so the
        panel can render counts, version, source root, and toggle
        status without any further RPC chatter. Returns typed
        :class:`PluginInfo` models — the wire format is defined in
        :mod:`core.plugins.models` so backend and frontend share
        the same shape.
        """
        loader = self._session.plugin_loader
        state = self._session.plugin_state
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

    # ── Enable / disable toggle ────────────────────────────────────

    def set_enabled(self, name: str, enabled: bool) -> msg.Info:
        """Toggle a plugin's enabled flag, persist, and hot-reload.

        Re-applying the plugin set after the flip means an
        ``enable`` activates the plugin's skills/agents/hooks
        immediately, and a ``disable`` makes them disappear from
        the live session — no restart needed.
        """
        loader = self._session.plugin_loader
        state = self._session.plugin_state
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
        _plugin_state.save_state(state, data_dir=self._session.settings.storage.data_dir)

        # Hot-reload — ``reload_plugins`` re-reads the disabled set
        # from disk and rebuilds skills/agents/hooks/MCP-configs
        # accordingly. The main team rebuilds at the end so tools
        # in the disabled plugin disappear from the agent surface,
        # and any bundled MCP servers are disconnected in the
        # background (auto-symmetric with the enable path).
        self._session.reload_plugins()
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

    # ── Install / update / remove ──────────────────────────────────

    def install(
        self,
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
        data_dir = self._session.settings.storage.data_dir
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
        counts = self._session.reload_plugins()
        return msg.Info(
            text=(
                f"Installed plugin '{manifest.name}'{version}. "
                f"Active now — {counts.skills} skill(s), "
                f"{counts.agents} agent(s), {counts.hooks} hook(s). "
                f"Any bundled MCP servers are starting in the background."
            )
        )

    def update(
        self,
        name: str,
        install_ref: str | None = None,
    ) -> msg.Info:
        """Fetch + reset to ``install_ref`` (default: origin's HEAD)."""
        installer = _plugin_installer.PluginInstaller(
            data_dir=self._session.settings.storage.data_dir,
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
        self._session.reload_plugins()
        return msg.Info(text=f"Updated '{name}' to {new_sha[:12]}. Active now.")

    def remove(self, name: str) -> msg.Info:
        """Delete the plugin directory and clear its pin."""
        installer = _plugin_installer.PluginInstaller(
            data_dir=self._session.settings.storage.data_dir,
        )
        try:
            installer.remove(name)
        except _plugin_installer.PluginError as e:
            return msg.Info(text=str(e))
        # Hot-reload so the removed plugin's skills/agents/hooks
        # disappear from the live session immediately. Any
        # bundled MCP servers are also disconnected in the
        # background — symmetric with the enable/install path.
        self._session.reload_plugins()
        return msg.Info(
            text=(
                f"Removed '{name}'. Skills/agents/hooks/tools no "
                "longer active; bundled MCP servers are being "
                "disconnected."
            )
        )
