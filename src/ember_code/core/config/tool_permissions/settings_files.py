"""Settings file I/O for tool permissions.

Owns the read/write side of ``.igni/settings.json`` &
``.igni/settings.local.json`` — split out of the monolithic
``tool_permissions.py`` so the store class no longer mixes disk I/O
with rule evaluation.

Two collaborators, one shared wire model:

* :class:`SettingsFileLoader` — reads the four-path priority chain
  (home global → home local → project global → project local),
  returning a :class:`LoadResult` per file (Pattern 3 typed
  failure).
* :class:`SettingsFileWriter` — writes back a persisted rule to
  ``.igni/settings.local.json`` (project-local, falls back to home
  when no project dir is set).

Both use :class:`EmberSettingsPermissionsFile` (from
:mod:`ember_code.core.config.tool_permissions.schemas`) as the
typed on-disk shape — Pattern 7 wire/domain split.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ember_code.core.config.tool_permissions.schemas import (
    EmberSettingsPermissionsFile,
    LoadResult,
    PermissionLevel,
)
from ember_code.core.paths import home_config_dir, project_config_dir

logger = logging.getLogger(__name__)


class SettingsFileLoader:
    """Reads the ordered chain of ember settings files.

    The four-path priority order (highest priority applied last)
    matches the historical behaviour:

    1. ``~/.igni/settings.json``            — user global defaults
    2. ``~/.igni/settings.local.json``      — user local overrides
    3. ``<project>/.igni/settings.json``    — project committed
    4. ``<project>/.igni/settings.local.json`` — project local

    Errors (missing file, bad JSON, wrong shape) yield a
    :class:`LoadResult` with ``ok=False`` — the caller decides
    whether to warn, degrade, or ignore.
    """

    def __init__(self, project_dir: Path | None = None) -> None:
        self._project_dir = project_dir or Path.cwd()

    def paths(self) -> list[Path]:
        """The ordered list of settings files this loader consults —
        exposed so tests can assert against the exact search order
        without duplicating the constants.

        Through the legacy-aware helpers, not ``/ CONFIG_DIR``: the
        settings merge plan resolves the very same four files that way
        (``merge_plan.py`` builds its ``JsonFragmentTier``s off
        ``home_config_dir()`` / ``project_config_dir()``), so hand-joining
        the current name here made two readers of one file disagree. On a
        project still holding ``.ember/`` — the pre-rename directory the
        helpers fall back to — ``Settings`` read ``.ember/settings.json``
        while this loader saw nothing, which silently dropped that file's
        ``deny`` rules. A permission list that fails open is the worst
        direction for this particular disagreement to fail in.
        """
        home_ember = home_config_dir()
        project_ember = project_config_dir(self._project_dir)
        return [
            home_ember / "settings.json",
            home_ember / "settings.local.json",
            project_ember / "settings.json",
            project_ember / "settings.local.json",
        ]

    def load_all(self) -> list[LoadResult]:
        """Load every file in :meth:`paths`, in priority order.

        Missing files are silently skipped (they're the common case
        for a fresh install). Parse errors or shape mismatches
        surface as :class:`LoadResult` with ``ok=False, reason=...``.
        """
        results: list[LoadResult] = []
        for path in self.paths():
            if not path.exists():
                continue
            results.append(self._load_one(path))
        return results

    def _load_one(self, path: Path) -> LoadResult:
        """Load a single file. Returns a :class:`LoadResult` — never
        raises so a corrupt settings file can't break session start."""
        try:
            raw_text = path.read_text()
        except OSError as exc:
            reason = f"read failed: {exc}"
            logger.warning("Failed to read %s: %s", path, exc)
            return LoadResult(path=str(path), ok=False, reason=reason)

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            reason = f"json decode failed: {exc}"
            logger.warning("Failed to parse %s: %s", path, exc)
            return LoadResult(path=str(path), ok=False, reason=reason)

        perms_blob = data.get("permissions") if isinstance(data, dict) else None
        if not perms_blob:
            # Empty / missing permissions block is not an error — the
            # file might carry other settings. Return an ``ok`` result
            # with an empty file model so the caller can uniformly
            # iterate.
            return LoadResult(
                path=str(path),
                ok=True,
                file=EmberSettingsPermissionsFile(),
            )

        try:
            file = EmberSettingsPermissionsFile.model_validate(perms_blob)
        except ValidationError as exc:
            reason = f"schema validation failed: {exc.errors()[:1]}"
            logger.warning("Failed to validate %s: %s", path, exc)
            return LoadResult(path=str(path), ok=False, reason=reason)

        return LoadResult(path=str(path), ok=True, file=file)


class SettingsFileWriter:
    """Writes a permission rule to the local overrides file.

    Split from :class:`SettingsFileLoader` because read and write
    are genuinely different responsibilities — the writer targets a
    single file (the project-local override, or home-local as a
    fallback) whereas the loader walks the four-path chain.

    Shared surface with the loader: both use
    :class:`EmberSettingsPermissionsFile` as the on-disk shape. That
    keeps the wire schema in one place even though the reader and
    writer live in sibling classes.
    """

    def __init__(self, project_dir: Path | None = None) -> None:
        self._project_dir = project_dir

    def target_path(self) -> Path:
        """The file this writer persists to.

        Project-local when a project directory was provided;
        home-local otherwise. Kept as a method (not a property) so
        the resolution appears at the call site — makes it easy to
        assert against in tests.

        Same helpers as :meth:`SettingsFileLoader.paths`, and for a
        sharper reason: writing to ``.igni/settings.local.json`` while an
        unmigrated project keeps its rules in ``.ember/`` would *create*
        the new directory, which is what ``project_config_dir`` prefers —
        so the next load would read the one-rule file this writer just
        made and stop seeing the old one entirely.
        """
        if self._project_dir:
            return project_config_dir(self._project_dir) / "settings.local.json"
        return home_config_dir() / "settings.local.json"

    def save_rule(self, rule: str, level: PermissionLevel) -> Path:
        """Persist ``rule`` under the ``permissions.<level>`` list of
        :meth:`target_path`.

        Removes the rule from any of the sibling ``allow`` / ``ask``
        / ``deny`` lists first so switching a rule's level doesn't
        leave a stale duplicate.

        Returns the file path written — useful for logs / tests.
        """
        path = self.target_path()
        data: dict[str, Any] = self._read_raw(path)

        perms = data.setdefault("permissions", {})
        for key in ("allow", "ask", "deny"):
            existing = perms.get(key, [])
            if rule in existing:
                existing.remove(rule)
                perms[key] = existing

        perms.setdefault(level, []).append(rule)

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
        return path

    def _read_raw(self, path: Path) -> dict[str, Any]:
        """Read the raw JSON dict for merging.

        We keep the raw ``dict[str, Any]`` shape here — not the
        :class:`EmberSettingsPermissionsFile` wire model — because
        the file may carry non-permissions keys (``mode`` etc.)
        that we must preserve verbatim across a save. Round-tripping
        through the wire model would drop them.
        """
        if not path.exists():
            return {}
        try:
            parsed = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "SettingsFileWriter: failed to read %s (%s); starting from empty",
                path,
                exc,
            )
            return {}
        return parsed if isinstance(parsed, dict) else {}
