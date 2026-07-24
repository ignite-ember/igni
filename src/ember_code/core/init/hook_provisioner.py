"""Hook-provisioning coordinator for the init flow.

Owns the "write built-in hook scripts + register them in
settings.json" pass. Replaces the old free ``_provision_hooks``
function — every dict-manipulation line the free function did
(``settings.setdefault("hooks", {}).setdefault(event, [])``)
disappears into typed :class:`SettingsFile` accessors on
:mod:`ember_code.core.init.schemas`.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ember_code.core.init.hooks_catalog import BUILT_IN_HOOKS
from ember_code.core.init.schemas import BuiltInHookSpec, SettingsFile

logger = logging.getLogger(__name__)


class HookProvisioner(BaseModel):
    """Provision built-in hooks into a project's ``.ember`` directory.

    Constructor takes the project directory and an optional
    iterable of :class:`BuiltInHookSpec` (defaults to
    :data:`BUILT_IN_HOOKS`). Callers that want to test with a custom
    catalog pass it via the ``hooks`` keyword — no monkey-patching
    of a module-level tuple required.

    :meth:`provision` is idempotent — running it twice does not
    duplicate hook entries in ``settings.json`` (dedup by
    ``command`` inside :meth:`SettingsFile.register_hook`).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    project_dir: Path
    hooks: tuple[BuiltInHookSpec, ...] = Field(default_factory=lambda: BUILT_IN_HOOKS)

    def provision(self) -> None:
        """Write every hook script and register each definition.

        Steps:

        1. Ensure ``.ember/hooks/`` exists.
        2. Load ``.ember/settings.json`` via :meth:`SettingsFile.load`
           (fail-soft — a corrupt file becomes an empty instance).
        3. For each spec, call
           :meth:`BuiltInHookSpec.write_script` (always overwrites —
           hooks are code, not config) and
           :meth:`BuiltInHookSpec.register_in` (idempotent).
        4. Save the settings file back — user-added top-level keys
           survive via :attr:`SettingsFile.model_config`'s
           ``extra="allow"``.
        """
        hooks_dir = self.project_dir / ".ember" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)

        settings_path = self.project_dir / ".ember" / "settings.json"
        settings = SettingsFile.load(settings_path)

        for hook in self.hooks:
            hook.write_script(hooks_dir)
            hook.register_in(settings)

        settings.save(settings_path)
