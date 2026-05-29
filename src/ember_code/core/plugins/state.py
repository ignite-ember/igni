"""Persisted plugin state at ``~/.ember/plugins.json``.

Tracks which plugins are user-disabled and which git SHA each plugin
was pinned to at install time. Read on every session start; written
when the user toggles enable/disable or installs/updates a plugin.

The file is intentionally tiny — most plugin info comes from
re-scanning disk each session. Persisting only what *can't* be
re-derived (user preferences + install pins) keeps recovery from a
corrupted state file painless: delete and continue.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class PluginsState(BaseModel):
    """The shape of ``~/.ember/plugins.json``.

    ``disabled`` is a flat list of plugin names that should not be
    activated even though they're present on disk. ``pins`` maps each
    install-managed plugin to the git ref (SHA or tag) it was installed
    at — used by ``/plugin update`` to detect drift and by the panel
    to display the installed version.
    """

    disabled: list[str] = Field(default_factory=list)
    pins: dict[str, str] = Field(default_factory=dict)


def state_path(data_dir: str | Path = "~/.ember") -> Path:
    """Where the plugins state file lives."""
    return Path(str(data_dir)).expanduser() / "plugins.json"


def load_state(data_dir: str | Path = "~/.ember") -> PluginsState:
    """Read the state file, or return an empty state if missing/corrupt.

    A corrupt file is logged at WARNING and treated as missing — the
    user can recover by deleting the file (no data loss since pins
    can be re-established by re-installing).
    """
    path = state_path(data_dir)
    if not path.is_file():
        return PluginsState()
    try:
        return PluginsState.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Failed to parse plugins state at %s: %s", path, e)
        return PluginsState()


def save_state(state: PluginsState, data_dir: str | Path = "~/.ember") -> None:
    """Atomically write the state file. Creates parent dir if needed."""
    path = state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)
