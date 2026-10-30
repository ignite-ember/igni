"""``IGNI_AIR_GAPPED`` — the environment half of air-gapped mode.

``air_gapped: true`` in settings is the switch (``core/config/settings.py``).
The environment variable is the same switch for processes that read no
settings: igni's decisions service, which may run on its own on a node, and
any child a backend starts. A backend whose settings say air-gapped exports
the variable to its children (:func:`export_to`), so one switch reaches all
of them.

The variable only turns the mode on. Unset, empty or false leaves the
settings' choice.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping

#: The variable a deployment turns air-gapped mode on with.
AIR_GAPPED_ENV = "IGNI_AIR_GAPPED"

#: What the variable accepts as "on".
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def air_gapped_by_environment(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the environment turns air-gapped mode on."""
    env = os.environ if environ is None else environ
    return env.get(AIR_GAPPED_ENV, "").strip().lower() in _TRUTHY


def export_to(environ: MutableMapping[str, str], air_gapped: bool) -> None:
    """Hand an air-gapped decision to the processes started from ``environ``.

    Only ever sets the variable: a process that is not air-gapped leaves an
    inherited ``IGNI_AIR_GAPPED=1`` where it found it. Only a real ``True``
    counts, so a stand-in settings object cannot switch a process over.
    """
    if air_gapped is True:
        environ[AIR_GAPPED_ENV] = "1"


__all__ = ["AIR_GAPPED_ENV", "air_gapped_by_environment", "export_to"]
