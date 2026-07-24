"""``ManagedPolicySource`` — the "external file source that lifts
specific keys into the merge pipeline" collaborator.

Owns two responsibilities that used to sprawl across the loader:

* Platform-specific discovery of the sysadmin-enforced managed
  policy file (``platform_path``) — previously a staticmethod on
  ``SettingsLoader`` plus a shim on the settings module.
* Silent-fail loading of a CC-style ``settings.json`` fragment
  restricted to a whitelist of top-level keys (``load_json_fragment``)
  — previously a classmethod on ``SettingsLoader``.

Naming caveat: the ``settings.json`` fragment technically lives at
``~/.ember/settings.json`` (user-tier), not at the managed-tier
location. Both are grouped here because they share the same
"external file → limited key lift → merged into the settings
accumulator" shape. The alternative — a second ``JsonFragmentSource``
collaborator — was rejected in synthesis: two 20-line files for the
same conceptual job costs more than the naming stretch.
"""

from __future__ import annotations

import json as _json
import logging
import os
import sys
from pathlib import Path

from pydantic import BaseModel

from ember_code.core.config.config_io import YamlSource

logger = logging.getLogger(__name__)


class JsonFragmentResult(BaseModel):
    """Typed result of a settings.json fragment read.

    Replaces the previous "returns {} on any error" contract with an
    explicit ``ok`` / ``reason`` shape so callers can log why a
    fragment was skipped without the broad ``except Exception:
    return {}`` swallowing the details.
    """

    ok: bool
    data: dict = {}
    reason: str | None = None


class ManagedPolicySource:
    """External-policy file source (managed YAML + settings.json fragment)."""

    # Top-level keys we lift from ``settings.json`` into the unified
    # ``Settings`` config. Other keys (notably ``hooks``) are owned
    # by dedicated loaders that read ``settings.json`` themselves —
    # we don't double-import them here. ``permissions`` is the one
    # block the CC-style settings file shares with the YAML config
    # and without lifting it the PermissionEvaluator never sees
    # user-tier deny rules.
    _JSON_KEYS_TO_LIFT: tuple[str, ...] = ("permissions",)

    @staticmethod
    def platform_path() -> Path | None:
        """OS-specific path for the sysadmin-enforced managed policy
        file.

        Mirrors Claude Code's managed-settings tier — a
        write-protected location that overrides every other layer
        including CLI flags. The intent is that a sysadmin (or MDM
        profile) drops a YAML file here to enforce org-wide policy
        (e.g. ``permissions.mode: dontAsk``, a pinned model, a
        blocked-commands list) that a user can't disable just by
        adding a ``--strict`` flag or editing project config.

        The file format is YAML (also accepts JSON, since JSON is a
        strict subset of YAML). Returns ``None`` on unknown platforms
        — the loader treats that as "no managed tier."
        """
        if sys.platform == "darwin":
            return Path("/Library/Application Support/Ember/managed-settings.yaml")
        if sys.platform.startswith("linux"):
            return Path("/etc/ember/managed-settings.yaml")
        if sys.platform == "win32":
            program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
            return Path(program_data) / "Ember" / "managed-settings.yaml"
        return None

    @classmethod
    def load(cls) -> dict:
        """Load the managed-settings YAML/JSON file if one is
        deployed. Returns ``{}`` when no file exists or the platform
        has no defined managed-settings location."""
        path = cls.platform_path()
        if path is None:
            return {}
        return YamlSource(path).load()

    @classmethod
    def load_json_fragment(cls, path: Path) -> JsonFragmentResult:
        """Read ``settings.json`` at ``path`` and lift ONLY the
        whitelisted top-level keys into the merge pipeline.

        Returns a typed :class:`JsonFragmentResult` so the caller
        can distinguish a missing file (``ok=False, reason="missing"``)
        from a malformed one (``ok=False, reason="parse error: ..."``)
        without the previous broad ``except Exception: return {}``
        swallowing the diagnostic.
        """
        if not path.exists():
            return JsonFragmentResult(ok=False, reason="missing")
        try:
            data = _json.loads(path.read_text())
        except Exception as exc:
            logger.debug("settings.json load failed at %s: %s", path, exc)
            return JsonFragmentResult(ok=False, reason=f"parse error: {exc}")
        if not isinstance(data, dict):
            return JsonFragmentResult(ok=False, reason="non-dict payload")
        lifted = {k: data[k] for k in cls._JSON_KEYS_TO_LIFT if k in data}
        return JsonFragmentResult(ok=True, data=lifted)
