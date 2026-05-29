"""Plugin installer — clone / update / remove against ``~/.ember/plugins``.

Wraps :class:`GitClient` + persisted :class:`PluginsState`. Plugin
identity comes from the manifest's ``name`` field (not the URL slug)
so two repos named identically by upstream can't collide here.

The install flow is **clone-to-temp-then-rename**: failures (bad
manifest, network drop mid-clone) never leave a partial directory in
``plugins/``. Mid-install crashes leave a ``_plugin_install_tmp/``
sibling which a subsequent install cleans up.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from ember_code.core.plugins.git import GitClient, GitError
from ember_code.core.plugins.models import PluginManifest
from ember_code.core.plugins.state import load_state, save_state

logger = logging.getLogger(__name__)


class PluginError(RuntimeError):
    """An install / update / remove operation failed at the plugin
    layer (bad manifest, already installed, missing on update, etc.).

    Distinct from :class:`GitError` so the slash command can surface
    actionable messages — a missing manifest is a different
    user-facing problem than a network failure during fetch.
    """


class PluginInstaller:
    """Manages ``~/.ember/plugins/`` and the pin map in plugins.json."""

    def __init__(
        self,
        *,
        data_dir: str | Path = "~/.ember",
        git_client: GitClient | None = None,
    ) -> None:
        self._data_dir = Path(str(data_dir)).expanduser()
        self._plugins_dir = self._data_dir / "plugins"
        self._tmp_dir = self._data_dir / "_plugin_install_tmp"
        self._git = git_client or GitClient()

    @property
    def plugins_dir(self) -> Path:
        """Where installed plugins live. Surfaced for callers that
        need to print the path (e.g. ``/plugin install`` confirmation
        prompts, ``/plugins info``)."""
        return self._plugins_dir

    def is_git_available(self) -> bool:
        """Precondition check — the slash command short-circuits with
        an install hint when this returns ``False``."""
        return self._git.is_available()

    # ── Install ─────────────────────────────────────────────────────

    def install(self, url: str, *, ref: str | None = None) -> PluginManifest:
        """Install a plugin from *url* into ``~/.ember/plugins/<name>/``.

        ``ref`` may be a branch, tag, or SHA. Branches and tags are
        passed directly to ``git clone --branch``; SHAs are checked
        out after a default-branch clone (older gits don't accept
        SHAs for ``--branch``).

        Returns the parsed :class:`PluginManifest` so the caller can
        report ``name``, ``version``, etc. The plugin's SHA at install
        time is recorded in ``plugins.json#pins`` for future
        ``/plugin update`` drift detection.

        Raises:
            GitError: clone failed (network, auth, bad URL, …).
            PluginError: manifest missing/malformed, or a plugin with
                the same name is already installed.
        """
        # Stale temp from a prior crashed install — wipe before reuse.
        if self._tmp_dir.exists():
            shutil.rmtree(self._tmp_dir, ignore_errors=True)

        clone_ref = ref if ref and not _looks_like_sha(ref) else None
        self._git.clone(url, self._tmp_dir, ref=clone_ref)

        # If ref is a SHA, check it out post-clone.
        if ref and _looks_like_sha(ref):
            self._git.checkout(self._tmp_dir, ref)

        manifest_path = self._tmp_dir / ".claude-plugin" / "plugin.json"
        if not manifest_path.is_file():
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
            raise PluginError(
                f"The cloned repo at {url} has no .claude-plugin/plugin.json — "
                "is this a Claude-Code-shaped plugin?"
            )

        try:
            manifest = PluginManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        except Exception as e:
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
            raise PluginError(f"plugin.json at {url} is malformed: {e}") from e

        dest = self._plugins_dir / manifest.name
        if dest.exists():
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
            raise PluginError(
                f"Plugin '{manifest.name}' is already installed at {dest}. "
                "Run `/plugin remove {manifest.name}` first to reinstall, "
                "or `/plugin update {manifest.name}` to update in place."
            )

        self._plugins_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self._tmp_dir), str(dest))

        sha = self._git.current_sha(dest)
        state = load_state(self._data_dir)
        state.pins[manifest.name] = sha
        save_state(state, data_dir=self._data_dir)

        logger.info(
            "Installed plugin '%s' at %s (pin=%s)",
            manifest.name,
            dest,
            sha,
        )
        return manifest

    # ── Update ─────────────────────────────────────────────────────

    def update(self, name: str, *, ref: str | None = None) -> str:
        """Fetch and reset the plugin's working tree to *ref*.

        ``ref=None`` (the default) bumps the pin to the current head
        of the plugin's default remote branch. Passing an explicit
        ``ref`` changes the pin to that branch / tag / SHA — useful
        when the user wants to try a specific release.

        Returns the SHA the plugin was set to.
        """
        plugin_dir = self._plugins_dir / name
        if not plugin_dir.is_dir():
            raise PluginError(f"Plugin '{name}' is not installed at {plugin_dir}.")

        try:
            self._git.fetch(plugin_dir)
        except GitError as e:
            raise PluginError(f"Failed to fetch updates for plugin '{name}': {e}") from e

        target = ref or f"origin/{self._git.head_branch(plugin_dir)}"
        self._git.reset_hard(plugin_dir, target)

        new_sha = self._git.current_sha(plugin_dir)
        state = load_state(self._data_dir)
        state.pins[name] = new_sha
        save_state(state, data_dir=self._data_dir)

        logger.info(
            "Updated plugin '%s' to %s (target=%s)",
            name,
            new_sha,
            target,
        )
        return new_sha

    # ── Remove ─────────────────────────────────────────────────────

    def remove(self, name: str) -> None:
        """Delete the plugin's directory and forget its pin.

        Idempotent on the state side: pin and disabled-list entries
        are removed if present, no-op otherwise. The filesystem step
        raises if the plugin dir isn't there — callers should treat
        that as a user error (typo, already removed).
        """
        plugin_dir = self._plugins_dir / name
        if not plugin_dir.is_dir():
            raise PluginError(f"Plugin '{name}' is not installed at {plugin_dir}.")

        shutil.rmtree(plugin_dir)
        state = load_state(self._data_dir)
        state.pins.pop(name, None)
        if name in state.disabled:
            state.disabled = [d for d in state.disabled if d != name]
        save_state(state, data_dir=self._data_dir)

        logger.info("Removed plugin '%s' from %s", name, plugin_dir)


def _looks_like_sha(ref: str) -> bool:
    """Heuristic: 7-40 hex chars with no other content = SHA-ish.

    Used to decide whether to pass ``ref`` to ``git clone --branch``
    (branches/tags only) vs. clone-then-checkout. Errs on the side of
    treating ambiguous values as branches/tags — those code paths
    surface the actual git error message verbatim.
    """
    if not (7 <= len(ref) <= 40):
        return False
    return all(c in "0123456789abcdefABCDEF" for c in ref)
