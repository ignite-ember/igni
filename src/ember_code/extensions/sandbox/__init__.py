"""An OS sandbox for an extension's own process.

Before this, an extension's declared ``egress`` and ``workspace`` were
enforced on its pages (CSP) and on what igni told its process, while
the process itself ran with the person's full file and network access.
This package confines the process to the same grants:

* :mod:`.launcher` — :class:`ExtensionSandbox`, the one interface the
  installer starts every extension process through;
* :mod:`.plan` — what one extension may read, write and reach;
* :mod:`.proxy` — the egress proxy igni runs per extension, the only
  route out;
* :mod:`.seatbelt` (macOS) and :mod:`.bubblewrap` (Linux, with
  :mod:`.bridge` inside its network namespace) — the platform profiles;
* :mod:`.settings` — ``extensions.sandbox`` and
  ``extensions.require_sandbox``, from the tiers allowed to set them.
"""

from ember_code.extensions.sandbox.launcher import (
    NO_PROXY,
    SANDBOX_ENV,
    Confinement,
    ExtensionSandbox,
    SandboxRefused,
)
from ember_code.extensions.sandbox.plan import SandboxPlan
from ember_code.extensions.sandbox.proxy import EgressProxy, HostAllowList
from ember_code.extensions.sandbox.settings import REQUIRE_SANDBOX_ENV, SandboxSettings

__all__ = [
    "NO_PROXY",
    "REQUIRE_SANDBOX_ENV",
    "SANDBOX_ENV",
    "Confinement",
    "EgressProxy",
    "ExtensionSandbox",
    "HostAllowList",
    "SandboxPlan",
    "SandboxRefused",
    "SandboxSettings",
]
