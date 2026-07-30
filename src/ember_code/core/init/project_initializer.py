"""Top-level orchestrator: :class:`ProjectInitializer`.

Owns the state (project dir + home dir + :class:`InitConfig`) and
the flow (bootstrap → migrate → first-run writes → checksum sync →
hook provisioning). Every free function from the pre-refactor
``core/init.py`` becomes an instance method here — no more threading
``project_dir`` through seven function signatures.

Delegates:

* Home-config bootstrap + migration →
  :class:`ember_code.core.init.home_migrator.HomeConfigMigrator`.
* Hook script + settings.json provisioning →
  :class:`ember_code.core.init.hook_provisioner.HookProvisioner`.
* Checksum-based agents/skills sync →
  :class:`ember_code.core.init.checksum_store.ChecksumStore`.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ember_code.core.init.checksum_store import ChecksumStore
from ember_code.core.init.home_migrator import HomeConfigMigrator
from ember_code.core.init.hook_provisioner import HookProvisioner
from ember_code.core.init.json_file import JsonFile
from ember_code.core.init.schemas import (
    InitConfig,
    InitResult,
    SettingsFile,
)
from ember_code.core.init_templates import (
    EMBER_MD_TEMPLATE,
    PROJECT_CONFIG_TEMPLATE,
)

logger = logging.getLogger(__name__)


class ProjectInitializer(BaseModel):
    """Initialise + update a project's ``.ember`` directory.

    Two responsibilities:

    1. **First-run init** — copies built-in agents/skills/hooks
       into ``.ember/`` and creates a starter ``ember.md``. A marker
       file (``.ember/.initialized``) ensures this only runs once.
    2. **Update on every start** — compares package files against
       local copies using SHA-256 checksums. Untouched files are
       overwritten; user-modified files trigger a warning + a
       ``.new`` sidecar.

    :meth:`run` is the entry point. Callers wanting the compat
    ``bool`` return should use the module-level ``initialize_project``
    shim in :mod:`ember_code.core.init` (which delegates to
    :meth:`initialize`).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    project_dir: Path
    config: InitConfig = Field(default_factory=InitConfig)
    home_ember: Path = Field(default_factory=lambda: Path.home() / ".ember")

    # ── Public entry point ────────────────────────────────────────

    @classmethod
    def initialize(cls, project_dir: Path, **config_kwargs) -> bool:
        """Construct an instance with the given config kwargs and run it.

        Returns ``True`` if this run performed first-time init on this
        project — matches the pre-refactor ``bool`` return shape that
        :mod:`ember_code.core.session.core` consumes. Forwarded
        ``**config_kwargs`` become fields of :class:`InitConfig`
        (``package_dir=`` is the common one — tests inject a fake
        package root through this entry point rather than
        monkey-patching a module attribute).
        """
        config = InitConfig(**config_kwargs)
        result = cls(project_dir=project_dir, config=config).run()
        return result.first_run

    def run(self) -> InitResult:
        """Run the full init-and-update flow.

        Returns a typed :class:`InitResult` — the compat shim in
        :mod:`ember_code.core.init` collapses this to ``.first_run``
        for backward compat with ``session/core.py``.
        """
        self.home_ember.mkdir(parents=True, exist_ok=True)
        (self.project_dir / ".ember").mkdir(parents=True, exist_ok=True)

        home_marker = self.home_ember / self.config.marker_file
        project_marker = self.project_dir / ".ember" / self.config.marker_file

        migrator = HomeConfigMigrator(home_ember=self.home_ember)

        # First-ever run on this machine: write the starter home config.
        if not home_marker.exists():
            migrator.bootstrap_default_config()
            home_marker.touch()

        # Migrate stale defaults from older versions — runs every
        # startup. Cheap and idempotent (byte-stable no-op once
        # migrated).
        migration = migrator.migrate()

        # First-time project init: create starter files.
        first_run = not project_marker.exists()
        if first_run:
            self._write_ember_md()
            self._write_project_config()
            self._write_project_settings()
            project_marker.touch()

        # Sync built-in agents/skills — checksum-based so user edits
        # are preserved.
        warnings = self._update_built_in_files()
        HookProvisioner(project_dir=self.project_dir).provision()

        for msg in warnings:
            logger.info(msg)

        return InitResult(
            first_run=first_run,
            warnings=warnings,
            migration=migration,
        )

    # ── Starter-file writers (first-run only) ─────────────────────

    def _write_ember_md(self) -> None:
        """Write a starter ``ember.md`` if one doesn't exist."""
        path = self.project_dir / "ember.md"
        if not path.exists():
            path.write_text(EMBER_MD_TEMPLATE)

    def _write_project_config(self) -> None:
        """Write a starter ``.ember/config.yaml`` with commented-out options."""
        path = self.project_dir / ".ember" / "config.yaml"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(PROJECT_CONFIG_TEMPLATE)

    def _write_project_settings(self) -> None:
        """Write a starter ``.ember/settings.local.json`` with default
        permissions.

        Gives users a template they can customise for their project.
        The file is gitignored so each user can have their own
        overrides. Team defaults can go in ``.ember/settings.json``
        (committed).

        Only writes the ``permissions`` block if the file doesn't
        already declare one — respects a user who has pre-seeded
        their own permissions.
        """
        path = self.project_dir / ".ember" / "settings.local.json"

        # Peek at the raw JSON first — we only touch the file if
        # ``permissions`` is absent, matching the pre-refactor
        # semantics that leave user-written files alone. Deliberate
        # raw-dict probe: we're checking key presence, not validating
        # the shape, so paying the Pydantic-validate cost on an
        # already-populated user file would be waste.
        if JsonFile(path=path).load().get("permissions") is not None:
            return

        settings = SettingsFile.load(path)
        settings.permissions = self.config.default_permissions.model_copy(deep=True)
        settings.save(path)

    # ── Checksum-based update (agents / skills) ───────────────────

    def _update_built_in_files(self) -> list[str]:
        """Sync built-in agents and skills using checksum-based merge.

        Returns a list of warning messages for files that were
        modified by the user and could not be auto-updated.
        """
        store = ChecksumStore.load(self.project_dir, self.config)
        warnings: list[str] = []

        # Update agents
        agents_src = self.config.package_dir / "bundled_agents"
        agents_dst = self.project_dir / ".ember" / "agents"
        if agents_src.exists():
            agents_dst.mkdir(parents=True, exist_ok=True)
            for src_file in agents_src.glob("*.md"):
                key = f"agents/{src_file.name}"
                dst_file = agents_dst / src_file.name
                outcome = store.sync_file(src_file, dst_file, key)
                if msg := outcome.warning_message():
                    warnings.append(msg)

        # Update skills
        skills_src = self.config.package_dir / "bundled_skills"
        skills_dst = self.project_dir / ".ember" / "skills"
        if skills_src.exists():
            for skill_dir in skills_src.iterdir():
                if not skill_dir.is_dir():
                    continue
                src_file = skill_dir / "SKILL.md"
                if not src_file.exists():
                    continue
                key = f"skills/{skill_dir.name}/SKILL.md"
                dst_dir = skills_dst / skill_dir.name
                dst_dir.mkdir(parents=True, exist_ok=True)
                dst_file = dst_dir / "SKILL.md"
                outcome = store.sync_file(src_file, dst_file, key)
                if msg := outcome.warning_message():
                    warnings.append(msg)

        store.save()
        return warnings
