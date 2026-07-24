"""``UserConfigStore`` — read/write ``~/.ember/config.yaml``.

Absorbs the ``save_default_model`` free function and
``Settings.persist_default_model`` classmethod. Both used to live
on the settings module (as a shim) and on the ``Settings`` class
respectively — a classic "state-first-arg free function" and
"classmethod-with-throwaway-cls" pair that the OOP audit flagged.

The path is constructor-injected so tests can point the store at a
``tmp_path`` without monkey-patching ``Path.home()``. Callers that
want the default location construct with no args.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class UserConfigStore:
    """Read/write for the user-global ``config.yaml``.

    The store never reaches for the process-global home dir except
    when no explicit path is passed to ``__init__`` — that
    single-owner boundary means test isolation can be done via
    constructor injection instead of ``Path.home`` monkey-patches.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else Path.home() / ".ember" / "config.yaml"

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> dict[str, Any]:
        """Return the parsed contents of the user config, or an empty
        dict if the file doesn't exist / isn't a YAML dict."""
        if not self._path.exists():
            return {}
        with open(self._path) as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}

    def write(self, payload: dict[str, Any]) -> None:
        """Persist ``payload`` to the user config, creating parent
        directories as needed. Uses ``yaml.safe_dump`` with block
        style and preserved key order — matches the shape the old
        :meth:`Settings.persist_default_model` produced so a diff
        of an existing user config only touches the intended keys.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w") as f:
            yaml.safe_dump(payload, f, default_flow_style=False, sort_keys=False)

    def set_default_model(self, model_name: str) -> None:
        """Persist a model choice to the user config so it survives
        across app restarts.

        Reads the existing file (or starts blank), sets/updates
        ``models.default``, and writes back. If ``models`` exists
        but isn't a dict (e.g. someone wrote a list by hand), the
        method overwrites it rather than crashing — the previous
        bad value is gone, which is acceptable because the
        alternative is the next launch failing to load config.

        The hosted-model *registry* is intentionally NOT persisted
        here — it gets refreshed from cloud discovery on session
        start, so freezing it would just stale-out as new models
        ship. Only the default identity is sticky.
        """
        existing = self.read()
        models_block = existing.setdefault("models", {})
        if not isinstance(models_block, dict):
            models_block = {}
            existing["models"] = models_block
        models_block["default"] = model_name
        self.write(existing)
