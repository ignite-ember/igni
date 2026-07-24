"""Hook loader — pure orchestration over settings-path discovery,
per-file JSON reading, and the :class:`HookRegistry` merge.

Every concern has an owner class:

* :class:`_SettingsPathDiscovery` — builds the ordered path list
  for ``home / project × ember / claude × base / .local`` given
  the ``cross_tool_support`` flag.
* :class:`_SettingsFileReader` — reads a single JSON file and
  returns either a parsed dict or a :class:`HookLoadWarning`.
* :class:`HookRegistry` (in ``registry.py``) — owns the merge
  itself via :meth:`HookRegistry.merge_from_dict`.

:class:`HookLoader` composes those three into
:meth:`HookLoader.load` (which returns a
:class:`HookLoadResult`) and :meth:`HookLoader.load_plugin_hooks`
(which returns the same result shape so callers can
:meth:`HookLoadResult.merge` them together).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ember_code.core.hooks.registry import HookRegistry
from ember_code.core.hooks.schemas import (
    HookLoadResult,
    HookLoadWarning,
    MergeStrategy,
)


class _SettingsPathDiscovery:
    """Ordered discovery of settings files that may declare hooks.

    Ordering matters — later paths layer on top of earlier ones,
    so project-local files override user globals, and
    ``.local.json`` variants override their base sibling. The
    default order is user-global → user-local → project → project-
    local, mirroring the pre-refactor loader.

    ``cross_tool_support=True`` splices ``.claude`` siblings in
    after the ``.ember`` bucket so a user with hooks in both
    ecosystems gets both loaded — the Ember hooks still win last
    since they're spec'd second.
    """

    def __init__(self, project_dir: Path, *, cross_tool_support: bool):
        self._project_dir = project_dir
        self._cross_tool_support = cross_tool_support

    def discover(self) -> list[Path]:
        """Return the ordered list of settings-file paths to try.

        Missing files are NOT filtered here — the reader handles
        that case by returning a ``None`` payload. That keeps the
        path list stable across runs (useful for logging /
        debugging).
        """
        home_ember = Path.home() / ".ember"
        paths: list[Path] = [
            home_ember / "settings.json",
            home_ember / "settings.local.json",
            self._project_dir / ".ember" / "settings.json",
            self._project_dir / ".ember" / "settings.local.json",
        ]
        if self._cross_tool_support:
            home_claude = Path.home() / ".claude"
            paths.extend(
                [
                    home_claude / "settings.json",
                    home_claude / "settings.local.json",
                    self._project_dir / ".claude" / "settings.json",
                    self._project_dir / ".claude" / "settings.local.json",
                ]
            )
        return paths


class _SettingsFileReader:
    """Reads a single settings-file JSON and extracts its ``hooks``
    block.

    Owns the two failure modes callers used to hit as ``print(...,
    file=sys.stderr)``:

    * :class:`json.JSONDecodeError` — malformed JSON.
    * :class:`OSError` — read failed (permissions, race with a
      concurrent editor, disappearing file).

    Both become structured :class:`HookLoadWarning` instances so
    the caller can log / batch / surface them however it wants.
    Missing files are NOT warnings (they're the expected case for
    most path candidates); the reader returns ``(None, [])`` for
    them.
    """

    def read(self, path: Path) -> tuple[dict[str, Any] | None, list[HookLoadWarning]]:
        """Read *path* and return ``(hooks_block, warnings)``.

        ``hooks_block`` is the dict at the ``hooks`` key of the
        parsed JSON, or ``None`` if the file was missing / broken
        / didn't contain a hooks block. Warnings surface the
        broken/missing-hook-block cases so callers can log them.
        """
        if not path.exists():
            return None, []
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            return None, [HookLoadWarning.from_path(path, "invalid_json", str(e))]
        except OSError as e:
            return None, [HookLoadWarning.from_path(path, "os_error", str(e))]
        hooks_block = data.get("hooks", {})
        if not isinstance(hooks_block, dict):
            return None, [
                HookLoadWarning.from_path(
                    path,
                    "non_dict_block",
                    "top-level 'hooks' key is not a JSON object",
                )
            ]
        return hooks_block, []


class HookLoader:
    """Loads hook definitions from settings files.

    Pure orchestration: composes :class:`_SettingsPathDiscovery`,
    :class:`_SettingsFileReader`, and :class:`HookRegistry` to
    turn a project directory into a populated
    :class:`HookLoadResult`.
    """

    def __init__(self, project_dir: Path | None = None, cross_tool_support: bool = False):
        self.project_dir = project_dir or Path.cwd()
        self.cross_tool_support = cross_tool_support
        self._discovery = _SettingsPathDiscovery(
            self.project_dir, cross_tool_support=cross_tool_support
        )
        self._reader = _SettingsFileReader()

    def load(self) -> HookLoadResult:
        """Load hooks from all settings files.

        Settings locations (merged, later wins):

        1. ``~/.ember/settings.json`` (user global defaults)
        2. ``~/.ember/settings.local.json`` (user local overrides)
        3. ``<project>/.ember/settings.json`` (project overrides,
           committed)
        4. ``<project>/.ember/settings.local.json`` (project local
           overrides, gitignored)

        With ``cross_tool_support=True`` the ``.claude`` siblings
        of each of the above are also consulted (before the
        matching ``.ember`` entry in the ordering — see
        :class:`_SettingsPathDiscovery`).

        Returns a :class:`HookLoadResult` bundling the populated
        registry with any structured warnings that surfaced. The
        registry's underlying dict is shared with downstream
        consumers (see :attr:`HookRegistry.raw`) so a plugin
        hot-reload can extend it in place.
        """
        registry = HookRegistry.from_empty()
        warnings: list[HookLoadWarning] = []
        for path in self._discovery.discover():
            hooks_block, read_warnings = self._reader.read(path)
            warnings.extend(read_warnings)
            if hooks_block is None:
                continue
            warnings.extend(
                registry.merge_from_dict(
                    hooks_block,
                    source=path,
                    strategy=MergeStrategy.APPEND,
                )
            )
        return HookLoadResult(registry=registry, warnings=warnings)

    def load_plugin_hooks(
        self,
        plugin_dir: Path,
        registry: HookRegistry,
    ) -> HookLoadResult:
        """Merge ``<plugin_dir>/hooks/hooks.json`` into *registry*.

        Same schema as ``settings.json``'s ``hooks`` block — the
        file IS the ``hooks`` block (no outer wrapping). Plugins
        are :class:`MergeStrategy.PREPEND`-ed so project-level
        hooks still run last and retain the veto.

        Returns a :class:`HookLoadResult` referencing the SAME
        registry that was passed in (so the caller can
        :meth:`HookLoadResult.merge` it with the settings-load
        result). Failures (missing, malformed, OSError) surface
        as warnings on the result — no ``sys.stderr`` fallout.
        """
        path = plugin_dir / "hooks" / "hooks.json"
        if not path.is_file():
            return HookLoadResult(registry=registry, warnings=[])
        # Read via the same reader that handles settings files,
        # but plugin files ARE the hooks block (no outer ``hooks``
        # key) so we sidestep :meth:`_SettingsFileReader.read` and
        # parse directly. Keeps the reader's contract (extract the
        # ``hooks`` sub-key) uncontaminated.
        try:
            with open(path, encoding="utf-8") as f:
                hooks_block = json.load(f)
        except json.JSONDecodeError as e:
            return HookLoadResult(
                registry=registry,
                warnings=[HookLoadWarning.from_path(path, "invalid_json", str(e))],
            )
        except OSError as e:
            return HookLoadResult(
                registry=registry,
                warnings=[HookLoadWarning.from_path(path, "os_error", str(e))],
            )
        if not isinstance(hooks_block, dict):
            return HookLoadResult(
                registry=registry,
                warnings=[
                    HookLoadWarning.from_path(
                        path,
                        "non_dict_block",
                        "plugin hooks file is not a JSON object",
                    )
                ],
            )
        warnings = registry.merge_from_dict(
            hooks_block, source=path, strategy=MergeStrategy.PREPEND
        )
        return HookLoadResult(registry=registry, warnings=warnings)
