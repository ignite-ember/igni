"""How many people this backend serves, and what that forbids.

EXTENSIBILITY §3 and §6 have said since they were written that a
sidecar is single-user only and that a shared backend serves no
extension UI. Nothing checked either. The rules were real, the
reasoning was right, and an administrator could do both anyway.

## Why one process cannot serve a group

A sidecar — and the development packaging, which is the same thing
without a container — is **one supervised process**. Everything igni
tells it is singular: one ``IGNI_WORKSPACE``, one resolution of each
``IGNI_SECRET_*``, one settings file. On a backend serving several
principals there is no answer to *whose*, and the process holds its
own state besides, with nothing in the contract making that state
per-caller.

**And there is no principal on this path at all.** ``RPCRequest``
carries a ``caller``, and a caller is an *extension id* — the check
is which extension is asking, never which person. So a shared
process could not be made safe by being careful; it would need an
identity that does not exist yet. That is written up in
``docs/EXTENSION_READINESS.md`` rather than guessed at here, because
inventing one is an architecture decision and not an install check.

A **remote endpoint** is the shape that fits a group: somebody else
runs it, it scales as it likes, and it is multi-tenant if it wants
to be. The same missing principal limits it — igni sends no identity,
so such a service cannot tell two users apart — but that is a gap to
close rather than a reason to refuse the install.
"""

from __future__ import annotations

import logging
import os
from enum import StrEnum

logger = logging.getLogger(__name__)

#: Set to ``shared`` on a backend serving more than one principal.
ENV_VAR = "IGNI_DEPLOYMENT"


class DeploymentProfile(StrEnum):
    """Who this backend is for."""

    #: One person, on their own machine. The default, because it is
    #: what a laptop is and because the permissive answer should not
    #: be the one you get by forgetting to configure anything.
    SINGLE_USER = "single-user"

    #: Several principals through one backend.
    SHARED = "shared"

    @classmethod
    def current(cls) -> DeploymentProfile:
        """What this process is running as.

        An environment variable for now, like development mode, and
        for the same reason: it is a deployment decision, it is not
        something a bundle can reach, and a setting is a bigger
        surface than one flag deserves until there are several.
        """
        raw = os.environ.get(ENV_VAR, "").strip().lower()
        if raw in {"shared", "multi", "multi-user"}:
            return cls.SHARED
        if raw and raw not in {"single", "single-user", "local"}:
            logger.warning(
                "%s=%r is not a deployment profile; treating this backend as single-user",
                ENV_VAR,
                raw,
            )
        return cls.SINGLE_USER

    @property
    def is_shared(self) -> bool:
        return self is DeploymentProfile.SHARED
