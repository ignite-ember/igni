"""Monitor config — parses ``.monitors.json`` files (plugins,
project, user tiers). Structured identically to the LSP config:

    {
      "monitors": {
        "build-watcher": {
          "command": "npm",
          "args": ["run", "watch"],
          "cwd": "frontend",
          "env": {"NODE_ENV": "development"},
          "restart": "on_crash"
        }
      }
    }

Precedence (lower → higher, last write wins on collision):
1. ``~/.ember/monitors.json`` (user)
2. ``<project>/.monitors.json`` (project)
3. Plugin-bundled ``.monitors.json`` (namespaced ``<plugin>:<name>``)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


RestartPolicy = Literal["never", "on_crash", "always"]


class MonitorConfig(BaseModel):
    """One background-monitor declaration. Required: ``name`` and
    ``command``. Restart behaviour defaults to ``on_crash``:
    monitors are usually long-running watchers that should
    survive transient failures but not infinite-loop on a hard
    error (the manager backs off and gives up after repeated
    crashes — see ``MonitorManager``).
    """

    model_config = ConfigDict(extra="allow")

    name: str
    command: str
    args: list[str] = Field(default_factory=list)
    # Working directory for the process. Relative paths resolve
    # against the session's ``project_dir`` at launch time;
    # absolute paths are honoured as-is.
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    restart: RestartPolicy = "on_crash"


def _parse_monitors_dict(raw: dict, namespace: str = "") -> dict[str, MonitorConfig]:
    """Coerce a ``monitors`` mapping into typed configs.

    ``namespace`` (plugin tier) is prepended as ``<ns>:<name>`` so
    the same simple name from multiple plugins doesn't collide.
    Malformed rows are dropped with a debug log — a single bad
    entry never sinks the file.
    """
    out: dict[str, MonitorConfig] = {}
    monitors = raw.get("monitors") or {}
    if not isinstance(monitors, dict):
        return out
    for name, entry in monitors.items():
        if not isinstance(entry, dict):
            continue
        command = entry.get("command")
        if not isinstance(command, str) or not command.strip():
            logger.debug("Monitor %s: missing/invalid command — skipping", name)
            continue
        restart = entry.get("restart", "on_crash")
        if restart not in ("never", "on_crash", "always"):
            logger.debug(
                "Monitor %s: unknown restart policy %r — defaulting to on_crash",
                name,
                restart,
            )
            restart = "on_crash"
        env = entry.get("env") or {}
        if not isinstance(env, dict):
            env = {}
        full_name = f"{namespace}:{name}" if namespace else name
        out[full_name] = MonitorConfig(
            name=full_name,
            command=command,
            args=list(entry.get("args") or []),
            cwd=entry.get("cwd") if isinstance(entry.get("cwd"), str) else None,
            env={str(k): str(v) for k, v in env.items()},
            restart=restart,
        )
    return out


def _read_json(path: Path) -> dict | None:
    """Permissive JSON read — returns ``None`` on any read or
    parse failure so the merge step can keep going."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.debug("Monitor config read %s failed: %s", path, exc)
        return None


def load_monitor_config(
    project_dir: Path,
    plugin_roots: list[tuple[Path, str]] | None = None,
) -> dict[str, MonitorConfig]:
    """Discover and merge monitor configs from all tiers.

    Returns ``{name → config}``. ``plugin_roots`` is a list of
    ``(plugin_path, plugin_name)`` tuples (built by
    ``PluginLoader.collect_monitor_roots``).
    """
    out: dict[str, MonitorConfig] = {}

    user_path = Path.home() / ".ember" / "monitors.json"
    if user_path.is_file() and (data := _read_json(user_path)) is not None:
        out.update(_parse_monitors_dict(data))

    project_path = project_dir / ".monitors.json"
    if project_path.is_file() and (data := _read_json(project_path)) is not None:
        out.update(_parse_monitors_dict(data))

    for root, plugin_name in plugin_roots or []:
        plugin_path = root / ".monitors.json"
        if plugin_path.is_file() and (data := _read_json(plugin_path)) is not None:
            out.update(_parse_monitors_dict(data, namespace=plugin_name))

    return out
