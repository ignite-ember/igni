"""Persistence coordinator for the model registry in ``~/.ember/config.yaml``.

Rehomed from :mod:`ember_code.core.auth.credentials` — the previous
``save_model_credentials`` free function mutated YAML for a model
registry entry but lived in the *auth* package (wrong seam) and
walked a raw nested dict (no Pydantic).

:class:`ModelRegistryStore` owns the path + read-modify-write cycle
as instance methods, and :class:`ModelRegistryEntry` is imported
from :mod:`ember_code.core.config.model_entry` (canonical shape) so
the YAML round-trip is Pydantic-typed instead of raw-dict.

Currently unused in-tree — the previous free function had zero
callers — but codifies the correct home so future model-config
persistence code lands in ``core/config/`` instead of ``core/auth/``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from ember_code.core.config.model_entry import ModelRegistryEntry

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path.home() / ".ember" / "config.yaml"


class ModelRegistryStore:
    """Read/write coordinator for the model registry section of the
    Ember Code YAML config file.

    Args:
        config_path: optional override; defaults to
            ``~/.ember/config.yaml``.
    """

    def __init__(self, config_path: Path | None = None) -> None:
        self._path: Path = config_path or DEFAULT_CONFIG_PATH

    @property
    def path(self) -> Path:
        """The resolved on-disk path this store reads / writes."""
        return self._path

    def set_entry(self, model_name: str, entry: ModelRegistryEntry) -> None:
        """Merge ``entry`` into ``models.registry[model_name]``,
        preserving every other config section.

        Round-trips the file through YAML: if the config doesn't
        exist we create parents + a fresh document; if it's
        malformed we log and start from an empty document rather
        than blow up (matches the previous free-function
        behaviour).
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)

        config: dict = {}
        if self._path.exists():
            try:
                data = yaml.safe_load(self._path.read_text())
                if isinstance(data, dict):
                    config = data
            except Exception as exc:
                logger.debug("Failed to load %s: %s", self._path, exc)

        registry = config.setdefault("models", {}).setdefault("registry", {})
        # ``exclude_none`` keeps optional fields the caller didn't
        # populate out of the YAML output — matches the previous
        # per-key setdefault behaviour.
        registry[model_name] = entry.model_dump(exclude_none=True)

        self._path.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))
        logger.debug("Model registry entry for %s saved to %s", model_name, self._path)


__all__ = ["ModelRegistryStore", "DEFAULT_CONFIG_PATH"]
