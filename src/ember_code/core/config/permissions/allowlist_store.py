"""Persistent allowlist storage — reads and writes
``~/.ember/permissions.yaml`` via a typed Pydantic model.

Replaces the raw ``dict[str, list[str]]`` allowlist that lived on
``PermissionGuard`` in the pre-refactor module. The store hides the
YAML I/O behind typed ``add`` / ``matches`` / ``entries_for`` methods
so callers never touch a raw dict.
"""

import fnmatch
import logging
import os
from pathlib import Path

import yaml
from pydantic import ValidationError

from ember_code.core.config.permissions.schemas import (
    AllowlistFile,
    AllowlistPattern,
    PermissionCategory,
)

logger = logging.getLogger(__name__)


class AllowlistStore:
    """Typed persistence layer for the per-category allowlist.

    Instance state:
        * ``_path`` — YAML file on disk (``~/.ember/permissions.yaml``
          by default).
        * ``_file`` — the Pydantic ``AllowlistFile`` model, loaded
          once at construction and mutated in-place on ``add``.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._file = self._load()

    # ── public API ────────────────────────────────────────────────

    @property
    def path(self) -> Path:
        """The on-disk YAML path. Exposed for the back-compat
        ``PermissionGuard.permissions_path`` shim."""
        return self._path

    def add(self, category: PermissionCategory, entry: AllowlistPattern) -> None:
        """Append ``entry`` under ``category`` and persist immediately."""
        bucket = self._file.entries.setdefault(category, [])
        bucket.append(entry)
        self._save()

    def matches(self, category: PermissionCategory, value: str) -> bool:
        """True if ``value`` matches any glob pattern saved under
        ``category``."""
        for entry in self._file.entries.get(category, []):
            if fnmatch.fnmatch(value, entry.pattern):
                return True
        return False

    def entries_for(self, category: PermissionCategory) -> list[AllowlistPattern]:
        """Read-only view of the saved patterns for a category."""
        return list(self._file.entries.get(category, []))

    # ── private I/O ───────────────────────────────────────────────

    def _load(self) -> AllowlistFile:
        """Load the persistent allowlist from disk.

        Tolerates two on-disk shapes for one release:

        1. New shape: ``{entries: {file_write: [{pattern: "src/*"}]}}``
           — a direct dump of :class:`AllowlistFile`.
        2. Legacy shape: ``{allowlist: {file_write: ["src/*"]}}`` —
           raw strings under the ``allowlist`` key. Strings get lifted
           into :class:`AllowlistPattern` instances so validation
           through ``AllowlistFile`` succeeds.

        A missing / malformed file returns an empty ``AllowlistFile``.
        """
        if not self._path.exists():
            return AllowlistFile()
        try:
            with open(self._path) as f:
                data = yaml.safe_load(f)
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("permissions allowlist load failed at %s: %s", self._path, exc)
            return AllowlistFile()

        if not isinstance(data, dict):
            return AllowlistFile()

        # Legacy shape: {allowlist: {category: [raw strings]}}
        if "allowlist" in data and "entries" not in data:
            raw = data.get("allowlist") or {}
            if not isinstance(raw, dict):
                return AllowlistFile()
            migrated: dict[str, list[dict[str, str]]] = {}
            for cat, values in raw.items():
                if not isinstance(values, list):
                    continue
                migrated[cat] = [{"pattern": v} for v in values if isinstance(v, str)]
            data = {"entries": migrated}

        try:
            return AllowlistFile.model_validate(data)
        except ValidationError as exc:
            logger.warning("permissions allowlist parse failed at %s: %s", self._path, exc)
            return AllowlistFile()

    def _save(self) -> None:
        """Persist ``self._file`` to disk under the new schema.

        Atomic: write to a temp sibling then ``os.replace`` — a
        crash between open and rename leaves the original file
        intact instead of half-written.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._file.model_dump(mode="json")
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(tmp, "w") as f:
            yaml.safe_dump(payload, f, default_flow_style=False, sort_keys=False)
        os.replace(tmp, self._path)
