"""Whether to sandbox, and whether running unsandboxed is allowed at all.

Read from the tiers that may set each switch rather than from the merged
:class:`~ember_code.core.config.settings.Settings`, because the merge
cannot say where a value came from, and *where* is the whole rule here
(:mod:`ember_code.core.config.schemas.extensions`):

==================  ============  =====  ============  =========  =======
setting             managed file  env    group policy  user file  project
==================  ============  =====  ============  =========  =======
``sandbox``         yes           —      —             yes        **no**
``require_sandbox`` yes           yes    yes           **no**     **no**
==================  ============  =====  ============  =========  =======

The group policy is the organisation's pack as igni server signed it and
igni verified it (:class:`~ember_code.core.config.group_policy.CachedSettingsFetcher`),
never the cached files beside it: a pack that does not verify sets
nothing. It may require the sandbox; it may not turn it off.

A project's value for either is never read: a repository somebody cloned
must not be able to unconfine the extensions that run beside it.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ember_code.core.config.config_io import YamlSource
from ember_code.core.config.managed_policy import ManagedPolicySource
from ember_code.core.config.schemas.extensions import ExtensionsConfig
from ember_code.core.layer import Layers
from ember_code.core.paths import home_config_dir

logger = logging.getLogger(__name__)

#: ``1`` makes a sandbox mandatory, as ``require_sandbox: true`` in the
#: managed file does.
REQUIRE_SANDBOX_ENV = "IGNI_EXTENSIONS_REQUIRE_SANDBOX"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True, slots=True)
class SandboxSettings:
    """The two switches, each from where it may come."""

    #: Confine extension processes. ``False`` only from managed or user.
    enabled: bool = True
    #: Refuse to run one unconfined. Only from managed or the env.
    required: bool = False
    #: Where ``enabled`` was turned off, for the log: ``managed`` or ``user``.
    disabled_by: str = ""

    @classmethod
    def load(
        cls,
        *,
        managed_path: Callable[[], Path | None] = ManagedPolicySource.platform_path,
        user_file: Path | None = None,
        environ: Mapping[str, str] | None = None,
        group: Callable[[], Any] | None = None,
    ) -> SandboxSettings:
        """Read the managed file, the env and the user's file.

        Args:
            managed_path: Where the managed policy is (injectable for tests).
            user_file: The user's ``config.yaml``; ``~/.igni/config.yaml``
                by default, and none when this layer reads no local settings.
            group: Returns the verified group-policy pack, or ``None``.
        """
        env = os.environ if environ is None else environ
        managed = cls._block(managed_path())
        if user_file is None and Layers.current().reads_local_settings():
            user_file = home_config_dir() / "config.yaml"
        user = cls._block(user_file)

        by_group = cls._group_block(group)
        required = (
            managed.get("require_sandbox") is True
            or env.get(REQUIRE_SANDBOX_ENV, "").strip().lower() in _TRUTHY
            or by_group.get("require_sandbox") is True
        )
        if by_group.get("sandbox") is False:
            logger.warning(
                "extensions.sandbox: false in the group policy is ignored: an "
                "organisation's pack may require the sandbox, not turn it off"
            )
        if "require_sandbox" in user:
            logger.warning(
                "extensions.require_sandbox in %s is ignored: only the managed "
                "settings file or %s may set it",
                user_file,
                REQUIRE_SANDBOX_ENV,
            )

        disabled_by = ""
        if managed.get("sandbox") is False:
            disabled_by = "managed"
        elif "sandbox" not in managed and user.get("sandbox") is False:
            disabled_by = "user"
        if disabled_by and required:
            logger.warning(
                "extensions.sandbox: false (%s settings) is ignored because "
                "extensions.require_sandbox is set",
                disabled_by,
            )
            disabled_by = ""
        return cls(enabled=not disabled_by, required=required, disabled_by=disabled_by)

    @classmethod
    def _group_block(cls, group: Callable[[], Any] | None) -> dict[str, Any]:
        """The ``extensions`` block of the verified group-policy pack."""
        if group is None:
            return {}
        try:
            pack = group()
        except Exception as exc:  # noqa: BLE001 — no verified pack sets nothing
            logger.warning("group policy not read for extensions: %s", exc)
            return {}
        if pack is None:
            return {}
        return cls._valid(pack.to_settings_dict().get("extensions"), "the group policy")

    @staticmethod
    def _valid(raw: Any, where: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {}
        try:
            ExtensionsConfig.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("extensions block in %s is invalid and ignored: %s", where, exc)
            return {}
        return {k: v for k, v in raw.items() if k in ExtensionsConfig.model_fields}

    @staticmethod
    def _block(path: Path | None) -> dict[str, Any]:
        """The ``extensions`` block of one YAML file, validated, as set keys only."""
        if path is None:
            return {}
        try:
            raw = YamlSource(path).load().get("extensions")
        except Exception as exc:  # noqa: BLE001 — an unreadable file sets nothing
            logger.warning("could not read %s: %s", path, exc)
            return {}
        return SandboxSettings._valid(raw, path)


__all__ = ["REQUIRE_SANDBOX_ENV", "SandboxSettings"]
