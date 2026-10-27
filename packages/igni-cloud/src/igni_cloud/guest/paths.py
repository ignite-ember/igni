"""Where igni and the guest agent meet: the VM's run directory.

``docs/CLOUD.md`` §9. In a producer's VM igni and the guest agent
(``igni__cloudvm``, ``docs/guest-protocol.md``) talk through three files
on the tmpfs ``/run/igni``, and the guest agent's ``restored``. Each has its own variable, so a test or an
image with another layout can move one without moving the others; the
run directory moves all three at once.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict


class GuestPaths(BaseModel):
    """The files of the guest contract."""

    model_config = ConfigDict(frozen=True)

    RUN_DIR_ENV: ClassVar[str] = "IGNI_RUN_DIR"
    ACTIVITY_ENV: ClassVar[str] = "IGNI_ACTIVITY_FILE"
    READY_ENV: ClassVar[str] = "IGNI_READY_FILE"
    PRODUCER_ENV_ENV: ClassVar[str] = "IGNI_PRODUCER_ENV_FILE"
    RESTORED_ENV: ClassVar[str] = "IGNI_RESTORED_FILE"
    SUSPENDING_ENV: ClassVar[str] = "IGNI_SUSPENDING_FILE"
    SUSPEND_READY_ENV: ClassVar[str] = "IGNI_SUSPEND_READY_FILE"
    TOKEN_ENV: ClassVar[str] = "IGNI_ACCESS_TOKEN_FILE"
    MANAGED_SETTINGS_ENV: ClassVar[str] = "IGNI_MANAGED_SETTINGS_FILE"
    VM_CREDENTIAL_ENV: ClassVar[str] = "IGNI_VM_CREDENTIAL_FILE"
    DEFAULT_VM_CREDENTIAL: ClassVar[str] = "/vol/igni/vm-credential"
    DEFAULT_RUN_DIR: ClassVar[str] = "/run/igni"

    #: igni's ``app.activity``, written by igni, read by the guest agent's ``status``.
    activity: Path
    #: Present once igni serves; the guest agent's ``ready``.
    ready: Path
    #: Who this VM is for, written by the node agent after every boot and restore.
    producer_env: Path
    #: ``{generation, at_ms, clock_step_ms}``, rewritten by the guest agent after every restore.
    restored: Path
    #: ``{"token"}``, written by the before-suspend hook: igni gets ready.
    suspending: Path
    #: ``{"token", "ms", "trimmed_kb"}``, igni's answer to ``suspending``.
    suspend_ready: Path
    #: The access token igni keeps from the VM credential (``producer.env`` names it).
    token: Path
    #: igni's managed policy, written by the node agent at every start.
    managed_settings: Path
    #: The VM's credential, on the producer's volume (``producer.env`` names it).
    vm_credential: Path

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> GuestPaths:
        env = os.environ if environ is None else environ
        run_dir = Path(env.get(cls.RUN_DIR_ENV, "").strip() or cls.DEFAULT_RUN_DIR)

        def named(variable: str, default: str) -> Path:
            value = env.get(variable, "").strip()
            return Path(value) if value else run_dir / default

        return cls(
            activity=named(cls.ACTIVITY_ENV, "activity.json"),
            ready=named(cls.READY_ENV, "ready"),
            producer_env=named(cls.PRODUCER_ENV_ENV, "producer.env"),
            restored=named(cls.RESTORED_ENV, "restored"),
            suspending=named(cls.SUSPENDING_ENV, "suspending"),
            suspend_ready=named(cls.SUSPEND_READY_ENV, "suspend-ready"),
            token=named(cls.TOKEN_ENV, "token"),
            managed_settings=named(cls.MANAGED_SETTINGS_ENV, "managed-settings.yaml"),
            vm_credential=Path(
                env.get(cls.VM_CREDENTIAL_ENV, "").strip() or cls.DEFAULT_VM_CREDENTIAL
            ),
        )


__all__ = ["GuestPaths"]
