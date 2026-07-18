"""Home-config migration + bootstrap for ``~/.ember/config.yaml``.

Two responsibilities encapsulated on :class:`HomeConfigMigrator`:

1. :meth:`bootstrap_default_config` — writes a minimal starter
   ``config.yaml`` if one doesn't exist (first-ever run).
2. :meth:`migrate` — strips legacy bundled cloud entries from an
   existing ``config.yaml``. Returns a typed
   :class:`MigrationResult` instead of the old
   ``except Exception: logger.debug(...)`` swallow.

Byte-stability contract (regression-tested):
* No-op path (nothing to remove) MUST NOT re-dump the file —
  Pydantic ``model_dump`` reorders keys to declaration order, so
  any unconditional dump-back would break the
  ``test_customised_entry_is_kept`` / ``test_no_op_when_no_bundled_entries``
  byte-equality assertions in ``tests/test_init.py``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ember_code.core.init.schemas import HomeConfig, MigrationResult
from ember_code.core.init_templates import _HOME_CONFIG_BOOTSTRAP, CONFIG_YAML_HEADER

logger = logging.getLogger(__name__)


class HomeConfigMigrator(BaseModel):
    """Bootstrap + migrate ``~/.ember/config.yaml``.

    Instance state is the home-``.ember`` directory. All disk IO is
    scoped to ``self.home_ember / 'config.yaml'``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    home_ember: Path

    @property
    def config_path(self) -> Path:
        """Absolute path to the home ``config.yaml``."""
        return self.home_ember / "config.yaml"

    def bootstrap_default_config(self) -> None:
        """Write a minimal starter ``config.yaml`` if one doesn't exist."""
        # NOTE: earlier versions dumped the whole ``Settings.default_dict()``
        # here, which duplicated the bundled cloud model entry into every
        # client's home file and made model rollouts a per-user migration
        # headache. The bootstrap is now intentionally empty — users fill
        # it in with overrides as they go, and cloud discovery handles
        # the hosted catalogue automatically.
        if not self.config_path.exists():
            self.config_path.write_text(CONFIG_YAML_HEADER + _HOME_CONFIG_BOOTSTRAP)

    def migrate(self) -> MigrationResult:
        """Strip legacy bundled cloud entries from ``config.yaml``.

        The signal for a bundled entry is Ember Cloud URL +
        ``cloud_token`` api_key — see
        :meth:`HomeModelRegistryEntry.is_bundled_cloud`. User-edited
        entries (different URL, custom api_key, custom model_id) are
        left untouched.

        Idempotent: once the bundled rows are gone, subsequent runs
        return ``MigrationResult(ok=True, removed=[])`` without
        re-dumping the file (byte-stability contract).
        """
        if not self.config_path.exists():
            return MigrationResult(ok=True, reason="no-file")

        home_config = HomeConfig.load(self.config_path)
        if home_config is None:
            logger.debug("Skipping model migration: home config unreadable.")
            return MigrationResult(ok=False, reason="unreadable")

        models = home_config.models
        if models is None or not models.registry:
            # Nothing to inspect → byte-stable no-op (no dump-back).
            return MigrationResult(ok=True)

        removed: list[str] = []
        for name in list(models.registry.keys()):
            entry = models.registry[name]
            if entry.is_bundled_cloud():
                del models.registry[name]
                removed.append(name)

        if not removed:
            # No changes → byte-stable no-op. Do NOT re-dump — see
            # module docstring.
            return MigrationResult(ok=True)

        # If the active default named one of the just-removed entries,
        # clear it so cloud discovery (or the resolver fallback) picks
        # something current.
        if models.default in removed:
            models.default = ""

        try:
            home_config.dump(self.config_path)
            logger.info(
                "Migrated ~/.ember/config.yaml: removed legacy bundled cloud entries %s.",
                removed,
            )
            return MigrationResult(ok=True, removed=removed)
        except Exception:  # noqa: BLE001 — file IO on user file
            logger.warning(
                "Failed to write migrated home config — bundled cloud "
                "entries may still shadow the live catalogue. Edit "
                "~/.ember/config.yaml manually if so.",
                exc_info=True,
            )
            return MigrationResult(ok=False, removed=removed, reason="write-failed")
