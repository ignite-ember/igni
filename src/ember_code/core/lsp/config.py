"""LSP server config — parses ``.lsp.json`` files from plugins
and project / user tiers, same precedence model as MCP.

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
``<plugin>/.lsp.json`` and are registered with the plugin name
as a namespace prefix (e.g. ``pyright`` from plugin ``mypy-tools``
becomes ``mypy-tools:pyright``) — same convention as MCP.

Precedence (lower → higher, last write wins on name collision):
1. ``~/.ember/lsp.json`` (user)
2. ``<project>/.lsp.json`` (project)
3. Plugin-bundled ``.lsp.json`` (in plugin priority order)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class LspServerConfig(BaseModel):
    """One LSP server's launch + protocol-init config.

    The required minimum is ``command`` — everything else has a
    workable default. Unknown fields are preserved
    (``extra="allow"``) so future Claude Code LSP-manifest additions
    don't bounce the file.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    command: str
    args: list[str] = Field(default_factory=list)
    # ``languages`` is purely informational — used by the
    # ``lsp_query`` tool's discoverability output and by future
    # convenience wrappers that route by file extension. The LSP
    # protocol itself doesn't care.
    languages: list[str] = Field(default_factory=list)
    # ``rootUri`` — workspace root passed in the ``initialize``
    # request. ``None`` means "use the project_dir at launch
    # time".
    root_uri: str | None = None
    # Free-form options passed verbatim in ``initializationOptions``.
    initialization_options: dict[str, Any] = Field(default_factory=dict)
    # Optional env overrides for the spawned process — useful for
    # things like ``PYTHONPATH`` or per-server log levels.
    env: dict[str, str] = Field(default_factory=dict)


def _parse_servers_dict(raw: dict, namespace: str = "") -> dict[str, LspServerConfig]:
    """Coerce one ``lspServers`` mapping into typed configs.

    ``namespace`` is prepended to each server name with a colon
    when set (plugin tier) — keeps server names unique across
    tiers without forcing plugin authors to pre-prefix.
    """
    out: dict[str, LspServerConfig] = {}
    servers = raw.get("lspServers") or {}
    if not isinstance(servers, dict):
        return out
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        command = entry.get("command")
        if not isinstance(command, str) or not command.strip():
            logger.debug("LSP server %s: missing/invalid command — skipping", name)
            continue
        # Accept both camelCase (CC) and snake_case keys for
        # ``rootUri`` / ``initializationOptions`` — the LSP spec
        # uses camelCase but Python users default to snake.
        root_uri = entry.get("rootUri", entry.get("root_uri"))
        init_opts = entry.get("initializationOptions", entry.get("initialization_options", {}))
        if not isinstance(init_opts, dict):
            init_opts = {}
        env = entry.get("env") or {}
        if not isinstance(env, dict):
            env = {}
        full_name = f"{namespace}:{name}" if namespace else name
        out[full_name] = LspServerConfig(
            name=full_name,
            command=command,
            args=list(entry.get("args") or []),
            languages=list(entry.get("languages") or []),
            root_uri=root_uri if isinstance(root_uri, str) else None,
            initialization_options=init_opts,
            env={str(k): str(v) for k, v in env.items()},
        )
    return out


def _read_json(path: Path) -> dict | None:
    """Best-effort JSON read — returns ``None`` on any failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.debug("LSP config read %s failed: %s", path, exc)
        return None


def load_lsp_config(
    project_dir: Path,
    plugin_roots: list[tuple[Path, str]] | None = None,
) -> dict[str, LspServerConfig]:
    """Discover and merge LSP server configs from all tiers.

    Returns ``{name → config}``. ``plugin_roots`` is a list of
    ``(plugin_path, plugin_name)`` tuples; usually built by the
    ``PluginLoader`` so disabled plugins are excluded upstream.
    Later sources override earlier ones on name collision (last
    write wins, same as MCP).
    """
    out: dict[str, LspServerConfig] = {}

    # User-tier (cross-tool naming preserved for symmetry with
    # other configs).
    user_path = Path.home() / ".ember" / "lsp.json"
    if user_path.is_file() and (data := _read_json(user_path)) is not None:
        out.update(_parse_servers_dict(data))

    # Project-tier.
    project_path = project_dir / ".lsp.json"
    if project_path.is_file() and (data := _read_json(project_path)) is not None:
        out.update(_parse_servers_dict(data))

    # Plugin-tier — namespaced by plugin name.
    for root, plugin_name in plugin_roots or []:
        plugin_path = root / ".lsp.json"
        if plugin_path.is_file() and (data := _read_json(plugin_path)) is not None:
            out.update(_parse_servers_dict(data, namespace=plugin_name))

    return out
