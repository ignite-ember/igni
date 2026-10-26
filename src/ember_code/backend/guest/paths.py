"""Where igni and the guest agent meet: the VM's run directory.

``docs/CLOUD.md`` §9. In a producer's VM igni and the guest agent
(``igni__cloudvm``, ``docs/guest-protocol.md``) talk through three files
on the tmpfs ``/run/igni``. Each has its own variable, so a test or an
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
    DEFAULT_RUN_DIR: ClassVar[str] = "/run/igni"

    #: igni's ``app.activity``, written by igni, read by the guest agent's ``status``.
    activity: Path
    #: Present once igni serves; the guest agent's ``ready``.
    ready: Path
    #: Who this VM is for, written by the node agent after every boot and restore.
    producer_env: Path

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
        )


__all__ = ["GuestPaths"]
