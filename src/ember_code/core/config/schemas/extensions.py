"""``extensions`` config — whether an extension's process runs in an OS sandbox.

Two switches, read by
:class:`~ember_code.extensions.sandbox.settings.SandboxSettings` straight
from the tiers that may set them, never from the merged settings:

* ``sandbox`` (default ``true``) — confine each extension's process to
  its grants (``docs/EXTENSION_READINESS.md``, *Gate B*). ``false`` is
  for developing an extension. Honoured from the **managed** file or
  the **user's** ``~/.igni/config.yaml``; a project's ``.igni`` config
  cannot turn the sandbox off for whoever clones it.
* ``require_sandbox`` (default ``false``) — refuse to start an extension
  on a machine where no sandbox is available, rather than run it
  unconfined with a warning. Honoured from the **managed** file, the
  organisation's **group policy** (the pack igni server signed, once
  verified) or ``IGNI_EXTENSIONS_REQUIRE_SANDBOX=1`` only: it is the
  administrator's decision, and a user or a project turning it off would
  make it no decision at all. When set it also overrides a user's ``sandbox:
  false``.

The merged :class:`~ember_code.core.config.settings.Settings` cannot say
which tier a value came from, so the sandbox reads these tiers itself.
"""

from __future__ import annotations

from pydantic import BaseModel


class ExtensionsConfig(BaseModel):
    #: Confine an extension's process to its grants. Managed or user only.
    sandbox: bool = True
    #: Refuse to start one where no sandbox exists. Managed or env only.
    require_sandbox: bool = False
