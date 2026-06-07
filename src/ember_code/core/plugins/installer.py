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

    def install(
        self,
        url: str,
        *,
        ref: str | None = None,
        subdir: str | None = None,
    ) -> PluginManifest:
        """Install a plugin from *url* into ``~/.ember/plugins/<name>/``.

        ``ref`` may be a branch, tag, or SHA. Branches and tags are
        passed directly to ``git clone --branch``; SHAs are checked
        out after a default-branch clone (older gits don't accept
        SHAs for ``--branch``).

        ``subdir`` (when set) — the cloned repo is itself a parent
        of the plugin, and the actual plugin lives at
        ``<clone>/<subdir>/``. Used for the official marketplace's
        ``git-subdir`` and intra-marketplace (``./relative/path``)
        source shapes — about half the official catalog. Without
        this, those entries fail with "no ``.claude-plugin/plugin.json``"
        because they look in the wrong directory.

        Returns the parsed :class:`PluginManifest` so the caller can
        report ``name``, ``version``, etc. The plugin's SHA at install
        time is recorded in ``plugins.json#pins`` for future
        ``/plugin update`` drift detection. For subdir installs the
        recorded SHA is the parent repo's HEAD — that's the only
        SHA git knows about; ``update`` re-clones the same shape.

        Raises:
            GitError: clone failed (network, auth, bad URL, …).
            PluginError: manifest missing/malformed, subdir doesn't
                exist, or a plugin with the same name is already
                installed.
        """
        # Stale temp from a prior crashed install — wipe before reuse.
        if self._tmp_dir.exists():
            shutil.rmtree(self._tmp_dir, ignore_errors=True)

        clone_ref = ref if ref and not _looks_like_sha(ref) else None
        self._git.clone(url, self._tmp_dir, ref=clone_ref)

        # If ref is a SHA, check it out post-clone.
        if ref and _looks_like_sha(ref):
            self._git.checkout(self._tmp_dir, ref)

        # Resolve the actual plugin root — the clone root itself for
        # bare-URL installs, or the subdirectory for git-subdir /
        # relative-path entries.
        plugin_root = self._tmp_dir / subdir if subdir else self._tmp_dir
        if subdir and not plugin_root.is_dir():
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
            raise PluginError(
                f"The cloned repo at {url} has no '{subdir}' subdirectory — "
                "the marketplace entry may be stale or the path is wrong."
            )

        manifest_path = plugin_root / ".claude-plugin" / "plugin.json"
        if not manifest_path.is_file():
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
            location = f"{url} (subdir: {subdir})" if subdir else url
            raise PluginError(
                f"The cloned repo at {location} has no "
                ".claude-plugin/plugin.json — is this a Claude-Code-"
                "shaped plugin?"
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
        # For subdir installs we move just the plugin subtree into
        # place, then nuke the leftover parent clone. Bare-URL
        # installs move the whole clone as before.
        if subdir:
            shutil.move(str(plugin_root), str(dest))
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
        else:
            shutil.move(str(self._tmp_dir), str(dest))

        # Record the parent repo's SHA. For subdir installs that's
        # the only commit ID we have; ``update`` re-clones with the
        # same subdir spec to track upstream changes.
        sha = self._git.current_sha(dest) if not subdir else _safe_current_sha(self._git, dest)
        state = load_state(self._data_dir)
        state.pins[manifest.name] = sha or ""
        save_state(state, data_dir=self._data_dir)

        logger.info(
            "Installed plugin '%s' at %s (pin=%s, subdir=%s)",
            manifest.name,
            dest,
            sha,
            subdir or "-",
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


def _safe_current_sha(git: GitClient, path: Path) -> str | None:
    """Best-effort ``git rev-parse HEAD`` for subdir-installed plugins.

    For a bare-URL install the dest is a full git checkout and
    ``current_sha`` always succeeds. Subdir installs move just a
    subtree of the clone into place — the moved subtree isn't a
    git repo on its own, so ``rev-parse HEAD`` would fail. We
    swallow that error: the pin in ``plugins.json`` ends up empty
    for subdir plugins, which is honest — there's no
    self-contained commit identity to pin against.
    """
    try:
        return git.current_sha(path)
    except Exception:
        return None
