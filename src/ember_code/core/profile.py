"""Where this process runs: on a person's desktop, or in the cloud.

``docs/CLOUD.md``. The **cloud profile** is one setting:
``IGNI_PROFILE=cloud`` in the process's environment, set by the image the
producer's VM boots. It is an environment variable and not a ``Settings``
field because it decides where settings come from. Everything that
behaves differently in the cloud asks :meth:`RuntimeProfile.current`:

* **state** — kept in ``IGNI_DATABASE_URL``, which the cloud requires
  (:class:`~ember_code.core.db.locator.StorageLocator`);
* **logs** — JSON lines on standard output, collected by the node; no
  log file under ``~/.igni`` — not the backend log, ``debug.log``,
  ``llm_calls.log``, ``hitl_trace.log`` nor the audit log
  (:mod:`ember_code.core.logs`);
* **credentials** — from the environment the gateway sets
  (``IGNI_ACCESS_TOKEN``), never ``credentials.json``
  (:class:`~ember_code.core.auth.credentials.CredentialsStore`);
* **settings** — only what igni server (group policy) and the image
  (managed policy, the command line) deliver; nothing under ``~/.igni``
  or in the workspace's ``.igni`` is read
  (:class:`~ember_code.core.config.merge_plan.SettingsMergePlan`).

Unset, or anything else, is the desktop: every one of those as it was.
"""

from __future__ import annotations

import logging
import os
from enum import StrEnum

logger = logging.getLogger(__name__)

#: The variable the image sets.
PROFILE_ENV = "IGNI_PROFILE"


class RuntimeProfile(StrEnum):
    DESKTOP = "desktop"
    CLOUD = "cloud"

    @classmethod
    def current(cls) -> RuntimeProfile:
        raw = os.environ.get(PROFILE_ENV, "").strip().lower()
        if raw == cls.CLOUD.value:
            return cls.CLOUD
        if raw and raw != cls.DESKTOP.value:
            logger.warning("%s=%r is not a profile; running as the desktop", PROFILE_ENV, raw)
        return cls.DESKTOP

    @property
    def is_cloud(self) -> bool:
        return self is RuntimeProfile.CLOUD


__all__ = ["PROFILE_ENV", "RuntimeProfile"]
