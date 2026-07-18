"""Pydantic schemas for the project-initializer package.

Owns every wire model this package writes to disk plus the value
objects that replace the old raw-dict blobs and module-level
constants inside ``core/init.py`` (Rule 1 — no dict literals for
structured payloads, Rule 5 — no module-level mutable state).

Wire shapes:

* :class:`SettingsFile` — the fields of ``.ember/settings.json`` /
  ``.ember/settings.local.json`` this package touches (``hooks`` +
  ``permissions``). ``extra="allow"`` preserves user-added top-level
  keys (``mcpServers``, ``env``, …) across a load-then-save cycle
  so this package's writes never nuke keys owned by other subsystems.
* :class:`HomeConfig` / :class:`HomeModelSection` /
  :class:`HomeModelRegistryEntry` — the fields of
  ``~/.ember/config.yaml`` the home-config migrator inspects. Also
  ``extra="allow"`` so unknown user overrides round-trip untouched.
* :class:`BuiltInHookSpec` — one built-in hook shipped by the package
  (script content + settings.json registration). Grew ``write_script``
  and ``register_in`` methods so the spec owns its own provisioning
  instead of being pushed through a free ``_provision_hooks`` function.
* :class:`InitConfig` — replaces the old module-level ``PACKAGE_DIR``
  / ``MARKER_FILE`` / ``CHECKSUMS_FILE`` / ``DEFAULT_PERMISSIONS``
  constants. Tests inject an alternative ``package_dir`` via
  constructor arg (Rule 5 — no monkey-patch on a module attribute).
* :class:`InitResult` / :class:`MigrationResult` — typed replacements
  for the old ``bool`` / swallowed-``Exception`` returns; give callers
  structured access to what happened during ``run()``.
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ember_code.core.config.tool_permissions.schemas import (
    EmberSettingsPermissionsFile,
)
from ember_code.core.hooks.schemas import HookDefinition
from ember_code.core.init.json_file import JsonFile
from ember_code.core.init_templates import CONFIG_YAML_HEADER

# ── SettingsFile — typed view over .ember/settings*.json ──────────


class SettingsFile(BaseModel):
    """Typed model of ``.ember/settings.json`` / ``settings.local.json``.

    Only the two keys this package writes (``hooks`` +
    ``permissions``) are declared explicitly; ``extra="allow"``
    captures every other top-level key (``mcpServers``, ``env``,
    other subsystems' overrides) verbatim so a load-then-save cycle
    doesn't strip them.

    :meth:`register_hook` is the typed replacement for the old
    ``settings.setdefault("hooks", {}).setdefault(event, [])`` dict
    reach — dedup is by ``command`` (the only unique identifier for
    a hook wire entry), matching the pre-refactor behaviour.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    hooks: dict[str, list[HookDefinition]] = Field(default_factory=dict)
    permissions: EmberSettingsPermissionsFile | None = None

    @classmethod
    def load(cls, path: Path) -> SettingsFile:
        """Load from disk. Missing file / invalid JSON → empty instance.

        Fails soft — the init flow must survive a hand-corrupted
        settings.json without crashing session startup. Both the
        JSON-decode failure (empty dict) and the Pydantic
        :class:`~pydantic.ValidationError` failure (empty instance +
        ``logger.warning``) are handled inside :meth:`JsonFile.load_model`.
        """
        return JsonFile(path=path).load_model(cls)

    def save(self, path: Path) -> None:
        """Persist to disk, preserving user-added keys.

        ``exclude_defaults=True`` keeps the on-disk file lean — an
        empty ``settings.local.json`` where the user has set no hooks
        and no permissions round-trips to an empty ``{}`` rather than
        ``{"hooks": {}, "permissions": null}`` noise.

        ``by_alias=True`` renders every embedded :class:`HookDefinition`
        with its wire field names (the canonical schema uses ``type``
        directly, so this is a no-op today, but keeps the code robust
        if future fields grow aliases).
        """
        JsonFile(path=path).save_model(
            self,
            exclude_none=True,
            exclude_defaults=True,
            by_alias=True,
        )

    def register_hook(self, event: str, definition: HookDefinition) -> None:
        """Idempotently append ``definition`` to ``hooks[event]``.

        Dedup key is the ``command`` field — the only unique
        identifier for a hook wire entry. Calling ``run()`` twice in
        a row must not append duplicate hook entries.
        """
        event_hooks = self.hooks.setdefault(event, [])
        for existing in event_hooks:
            if existing.command == definition.command:
                return
        event_hooks.append(definition)


# ── BuiltInHookSpec — one built-in hook shipped by the package ────


class BuiltInHookSpec(BaseModel):
    """One built-in hook shipped by the package.

    ``content`` is the script body — written to
    ``.ember/hooks/<filename>`` and marked executable by
    :meth:`write_script`. ``definition`` is registered in
    ``settings.json`` under the ``event`` key by :meth:`register_in`.

    The spec owns its own provisioning (Rule 4 — related helpers
    become methods on a class whose name captures the subject).
    """

    model_config = ConfigDict(frozen=True)

    filename: str
    content: str
    event: str
    definition: HookDefinition

    def write_script(self, hooks_dir: Path) -> None:
        """Write the hook script into ``hooks_dir`` and mark it +x.

        Always overwrites — hook scripts are code shipped by the
        package, not user-editable config in the way agents/skills
        are. Users configure hooks via ``settings.json``, not by
        editing the scripts.
        """
        script_path = hooks_dir / self.filename
        script_path.write_text(self.content)
        script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def register_in(self, settings_file: SettingsFile) -> None:
        """Register :attr:`definition` in ``settings_file`` under
        :attr:`event`. Idempotent via
        :meth:`SettingsFile.register_hook`.
        """
        settings_file.register_hook(self.event, self.definition)


# ── HomeConfig — typed view over ~/.ember/config.yaml ─────────────


class HomeModelRegistryEntry(BaseModel):
    """One entry in ``~/.ember/config.yaml`` under
    ``models.registry.<name>``.

    Only the two fields the migrator inspects (``url`` + ``api_key``)
    are declared. ``extra="allow"`` captures user-added fields
    (``provider`` / ``model_id`` / ``temperature`` / …) so the
    migrator can round-trip untouched entries byte-for-byte.
    """

    model_config = ConfigDict(extra="allow")

    url: str = ""
    api_key: str = ""

    def is_bundled_cloud(self) -> bool:
        """True for entries that match the old bundled cloud-model
        shape (Ember Cloud URL + ``cloud_token`` api_key).

        Both signals anchor on conventions only the package ever
        wrote; a user pointing their own ``cloud_token`` sentinel at
        a different URL (or vice-versa) doesn't match.
        """
        return "ignite-ember.sh" in self.url and self.api_key == "cloud_token"


class HomeModelSection(BaseModel):
    """The ``models:`` block inside ``~/.ember/config.yaml``.

    ``extra="allow"`` covers user-added keys under ``models:`` that
    aren't ``default`` / ``registry`` (future config knobs).
    """

    model_config = ConfigDict(extra="allow")

    default: str = ""
    registry: dict[str, HomeModelRegistryEntry] = Field(default_factory=dict)


class HomeConfig(BaseModel):
    """Typed model of ``~/.ember/config.yaml``.

    Only ``models:`` is declared — the migrator doesn't touch any
    other top-level key. ``extra="allow"`` guarantees a user's own
    top-level keys round-trip byte-for-byte through a load/dump
    cycle.
    """

    model_config = ConfigDict(extra="allow")

    models: HomeModelSection | None = None

    @classmethod
    def load(cls, path: Path) -> HomeConfig | None:
        """Load from a YAML file at ``path``.

        Returns ``None`` for the "unreadable / unparseable" case — a
        hand-corrupted ``~/.ember/config.yaml`` must not crash
        session startup. Bare :class:`Exception` catch is intentional
        here (user file, fail soft — see :class:`MigrationResult`).
        Missing file also returns ``None``.
        """
        if not path.exists():
            return None
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except Exception:  # noqa: BLE001 — user file, fail soft
            return None
        if not isinstance(data, dict):
            # Top-level YAML that isn't a mapping is a no-op — return
            # ``None`` so the caller emits the "unreadable" migration
            # result rather than raising.
            return None
        try:
            return cls.model_validate(data)
        except Exception:  # noqa: BLE001 — user file, fail soft
            return None

    def dump(self, path: Path) -> None:
        """Write the config back to ``path`` with the standard header.

        ``exclude_none=True`` keeps ``models: null`` out of the file
        when the migrator has just stripped the whole section.
        """
        payload = self.model_dump(exclude_none=True)
        # Tidy: an empty ``models`` block (default cleared, registry
        # empty) should be dropped entirely so the file is minimal.
        models = payload.get("models")
        if isinstance(models, dict):
            if not models.get("default"):
                models.pop("default", None)
            if not models.get("registry"):
                models.pop("registry", None)
            if not models:
                payload.pop("models", None)
        body = yaml.dump(payload, default_flow_style=False, sort_keys=False)
        path.write_text(CONFIG_YAML_HEADER + body)


# ── Result envelopes ──────────────────────────────────────────────


class MigrationResult(BaseModel):
    """Typed outcome of one home-config migration pass.

    Replaces the two ``except Exception: logger.debug(...)`` swallow
    blocks in the old ``_migrate_home_model_default`` (Pattern 3 —
    typed failure envelope instead of naked exception handling).

    * ``ok=True, removed=[]`` → no-op (already migrated, or nothing
      to remove). The migrator MUST skip the dump-back step in this
      case so byte-stability of the untouched file is preserved.
    * ``ok=True, removed=[name, ...]`` → entries stripped, file
      rewritten.
    * ``ok=False, reason=...`` → the load failed (unreadable /
      unparseable). Nothing was written.
    """

    ok: bool
    removed: list[str] = Field(default_factory=list)
    reason: str = ""


class SyncOutcome(BaseModel):
    """Typed outcome of one :meth:`ChecksumStore.sync_file` call.

    Replaces the old ``str | None`` return where a truthy string
    meant "warn the user about a diverged file" and ``None``
    conflated the four other distinct outcomes (Rule 1 —
    structured return over string signalling, AP4 — Literal-tagged
    taxonomy for a closed set of outcomes).

    ``kind`` values:

    * ``copied`` — dst didn't exist; the package file was copied in
      and its hash was recorded.
    * ``recorded_legacy`` — dst existed with no stored checksum
      (legacy layout); current package hash was recorded, dst was
      left untouched.
    * ``unchanged`` — package file matches the stored checksum;
      no-op.
    * ``overwritten`` — package changed but the user hadn't
      modified dst; dst was overwritten and its hash was updated.
    * ``diverged`` — package changed AND the user modified dst; a
      ``.new`` sidecar was written next to dst and the stored hash
      was bumped. The only outcome that produces a warning via
      :meth:`warning_message`.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    kind: Literal[
        "copied",
        "recorded_legacy",
        "unchanged",
        "overwritten",
        "diverged",
    ]
    key: str
    sidecar_path: Path | None = None

    def warning_message(self) -> str | None:
        """User-facing warning for this outcome, or ``None`` if the
        outcome is benign.

        Only ``diverged`` produces a message — the other four kinds
        are silent success paths. Callers collect via ``if msg :=
        outcome.warning_message(): warnings.append(msg)`` rather
        than switching on :attr:`kind`.
        """
        if self.kind != "diverged":
            return None
        return (
            f"Built-in {self.key} was updated but you have local modifications. "
            f"New version saved as .ember/{self.key}.new — diff and merge at "
            f"your convenience."
        )


class InitResult(BaseModel):
    """Typed outcome of one :meth:`ProjectInitializer.run` call.

    Replaces the old ``bool`` return so callers who care about the
    checksum-merge warnings or the home-config migration outcome
    can read them structured rather than through the logger.

    ``first_run`` preserves the pre-refactor semantics — the
    ``initialize_project`` compat shim in ``__init__.py`` returns
    this field as a bare ``bool`` so legacy callers
    (``session/core.py`` and ``tests/test_onboarding_and_audit.py``)
    keep working without changes.
    """

    first_run: bool
    warnings: list[str] = Field(default_factory=list)
    migration: MigrationResult | None = None


# ── InitConfig — knobs the initializer reads (replaces constants) ─


# Default permissions for new projects. Use display names only —
# ``tool_permissions.py:FUNC_TO_TOOL`` normalises Agno function names
# (``run_shell_command`` / ``edit_file`` / ``save_file`` / …) to the
# display name (``Bash`` / ``Edit`` / ``Write``) before any rule
# lookup, so listing both is redundant and leaks an internal detail
# into a user-facing config file.
_DEFAULT_PROJECT_PERMISSIONS = EmberSettingsPermissionsFile(
    allow=["Glob", "Grep", "LS", "Read", "WebSearch", "WebFetch"],
    ask=["Write", "Edit", "Bash", "BashOutput", "Python"],
    deny=[],
)


def _default_package_dir() -> Path:
    """Compute the package-root directory (``src/ember_code``).

    ``bundled_agents`` / ``bundled_skills`` live under this root; the
    globs return empty if it points at the wrong directory.
    """
    return Path(__file__).resolve().parent.parent.parent


class InitConfig(BaseModel):
    """Configuration knobs for :class:`ProjectInitializer`.

    Replaces the module-level ``PACKAGE_DIR`` / ``MARKER_FILE`` /
    ``CHECKSUMS_FILE`` / ``DEFAULT_PERMISSIONS`` constants (Rule 5 —
    no module-level mutable state). Tests inject an alternative
    ``package_dir`` via :meth:`ProjectInitializer.initialize` (which
    forwards ``**config_kwargs`` here) rather than monkey-patching
    a module attribute.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    package_dir: Path = Field(default_factory=_default_package_dir)
    marker_file: str = ".initialized"
    checksums_file: str = ".checksums.json"
    default_permissions: EmberSettingsPermissionsFile = Field(
        default_factory=lambda: _DEFAULT_PROJECT_PERMISSIONS.model_copy(deep=True)
    )
