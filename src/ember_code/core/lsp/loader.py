"""LSP config loader — discovers ``.lsp.json`` files across the
user / project / plugin tiers and merges them into a typed result.

Wire format:

    {
      "lspServers": {
        "pyright": {
          "command": "pyright-langserver",
          "args": ["--stdio"],
          "languages": ["python"],
          "rootUri": null,
          "initializationOptions": {}
        },
        "tsserver": {...}
      }
    }

Project / user files live at ``<project>/.lsp.json`` and
``~/.ember/lsp.json`` respectively. Plugin-bundled files live at
``<plugin>/.lsp.json`` and are registered with the plugin name as
a namespace prefix (e.g. ``pyright`` from plugin ``mypy-tools``
becomes ``mypy-tools:pyright``) — same convention as MCP.

Precedence (lower → higher, last write wins on name collision):
1. ``~/.ember/lsp.json`` (user)
2. ``<project>/.lsp.json`` (project)
3. Plugin-bundled ``.lsp.json`` (in plugin priority order)

Per-file / per-entry parse failures are collected as
:class:`LspConfigLoadError` entries on the returned
:class:`LspConfigLoadResult` (Pattern 3 — expected failures as
data) rather than silently dropped.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ember_code.core.lsp.schemas import (
    LspConfigFile,
    LspConfigLoadError,
    LspConfigLoadResult,
    LspServerConfig,
)

logger = logging.getLogger(__name__)


class LspConfigLoader:
    """Discovers and merges LSP server configs across all tiers.

    Construct with the project directory and the plugin roots the
    :class:`PluginLoader` handed us (already filtered on
    disabled state). Call :meth:`load` to get an
    :class:`LspConfigLoadResult` — its ``.servers`` is the merged
    ``{name → LspServerConfig}`` mapping, its ``.errors`` lists
    every per-file and per-entry parse failure so callers can
    surface them to the panel.
    """

    _USER_CONFIG_RELATIVE = Path(".ember") / "lsp.json"
    _PROJECT_CONFIG_NAME = ".lsp.json"
    _PLUGIN_CONFIG_NAME = ".lsp.json"

    def __init__(
        self,
        project_dir: Path,
        plugin_roots: list[tuple[Path, str]] | None = None,
    ) -> None:
        self._project_dir = project_dir
        self._plugin_roots: list[tuple[Path, str]] = list(plugin_roots or [])

    # ── Public API ───────────────────────────────────────────────

    def load(self) -> LspConfigLoadResult:
        """Discover and merge configs from every tier.

        Later sources override earlier ones on name collision
        (last write wins, same as MCP).
        """
        result = LspConfigLoadResult()
        self._load_user_tier(result)
        self._load_project_tier(result)
        for root, plugin_name in self._plugin_roots:
            self._load_plugin_tier(result, root, plugin_name)
        return result

    # ── Tier loaders ─────────────────────────────────────────────

    def _load_user_tier(self, result: LspConfigLoadResult) -> None:
        """Load ``~/.ember/lsp.json`` if present."""
        user_path = Path.home() / self._USER_CONFIG_RELATIVE
        if user_path.is_file():
            self._load_file(result, user_path, namespace="")

    def _load_project_tier(self, result: LspConfigLoadResult) -> None:
        """Load ``<project>/.lsp.json`` if present."""
        project_path = self._project_dir / self._PROJECT_CONFIG_NAME
        if project_path.is_file():
            self._load_file(result, project_path, namespace="")

    def _load_plugin_tier(
        self,
        result: LspConfigLoadResult,
        root: Path,
        plugin_name: str,
    ) -> None:
        """Load ``<plugin>/.lsp.json`` under the plugin's namespace."""
        plugin_path = root / self._PLUGIN_CONFIG_NAME
        if plugin_path.is_file():
            self._load_file(result, plugin_path, namespace=plugin_name)

    # ── Per-file / per-entry parsing ────────────────────────────

    def _load_file(
        self,
        result: LspConfigLoadResult,
        path: Path,
        namespace: str,
    ) -> None:
        """Read one config file and merge its entries into
        ``result``. Whole-file read/decode failures become
        :class:`LspConfigLoadError` entries with ``entry_name=None``.
        """
        raw = self._read_json(result, path)
        if raw is None:
            return
        self._parse_tier(result, raw, path, namespace)

    def _parse_tier(
        self,
        result: LspConfigLoadResult,
        raw: Mapping[str, Any],
        path: Path,
        namespace: str,
    ) -> None:
        """Iterate the ``lspServers`` mapping in one raw config
        payload and merge parsed entries into ``result.servers``.
        Per-entry failures land in ``result.errors``."""
        try:
            typed = LspConfigFile.model_validate(dict(raw))
        except Exception as exc:
            result.errors.append(
                LspConfigLoadError(
                    path=str(path),
                    entry_name=None,
                    reason=f"invalid config file shape: {exc}",
                )
            )
            return
        for name, entry in typed.lspServers.items():
            self._parse_entry(result, path, namespace, name, entry)

    def _parse_entry(
        self,
        result: LspConfigLoadResult,
        path: Path,
        namespace: str,
        name: str,
        entry: Any,
    ) -> None:
        """Delegate one raw entry to
        :meth:`LspServerConfig.from_raw`, appending an
        :class:`LspConfigLoadError` on rejection."""
        parsed = LspServerConfig.from_raw(name, entry, namespace=namespace)
        if parsed is None:
            reason = (
                "entry is not a mapping"
                if not isinstance(entry, Mapping)
                else "missing or blank command"
            )
            result.errors.append(
                LspConfigLoadError(
                    path=str(path),
                    entry_name=name,
                    reason=reason,
                )
            )
            logger.debug("LSP server %s in %s: %s — skipping", name, path, reason)
            return
        result.servers[parsed.name] = parsed

    def _read_json(
        self,
        result: LspConfigLoadResult,
        path: Path,
    ) -> Mapping[str, Any] | None:
        """Best-effort JSON read. Returns ``None`` (and records a
        whole-file :class:`LspConfigLoadError`) on any decode or
        I/O failure."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            result.errors.append(
                LspConfigLoadError(
                    path=str(path),
                    entry_name=None,
                    reason=f"read/decode failed: {exc}",
                )
            )
            logger.debug("LSP config read %s failed: %s", path, exc)
            return None
        if not isinstance(data, Mapping):
            result.errors.append(
                LspConfigLoadError(
                    path=str(path),
                    entry_name=None,
                    reason="top-level JSON is not an object",
                )
            )
            return None
        return data


def load_lsp_config(
    project_dir: Path,
    plugin_roots: list[tuple[Path, str]] | None = None,
) -> dict[str, LspServerConfig]:
    """Back-compat wrapper around :class:`LspConfigLoader`.

    Returns just the merged ``{name → LspServerConfig}`` dict.
    Parse errors are collected on the underlying
    :class:`LspConfigLoadResult` but dropped here — call
    :class:`LspConfigLoader` directly to surface them to the panel.
    """
    return LspConfigLoader(project_dir, plugin_roots).load().servers


__all__ = [
    "LspConfigLoader",
    "load_lsp_config",
]
