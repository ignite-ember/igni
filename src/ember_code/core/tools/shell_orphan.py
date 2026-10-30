"""Orphan process types + boot-time rehydration (re-export shim).

Originally the ~240-LoC module that mixed the orphan model,
the Pydantic subprocess stub, and the procedural rehydrate
coordinator. The OOP audit split it into three single-concern
modules:

* :class:`~ember_code.core.tools.orphan_process.OrphanProcess`
  and :class:`~ember_code.core.tools.orphan_process.OrphanProcess.probe_alive`
  live in :mod:`orphan_process`.
* :class:`~ember_code.core.tools.orphan_rehydrator.OrphanRehydrator`
  and :meth:`~ember_code.core.tools.orphan_rehydrator.OrphanRehydrator.run`
  live in :mod:`orphan_rehydrator`.
* :class:`~ember_code.core.tools.shell_orphan_schemas.OrphanProcStub`
  / :class:`OrphanReadResult` /
  :class:`~ember_code.core.tools.shell_orphan_schemas.RehydrateResult`
  live in :mod:`shell_orphan_schemas` (sibling schemas convention).

This module keeps the public re-exports and a thin async
:func:`rehydrate_orphan_processes` wrapper. Nothing in production
imports it any more: :class:`RehydrateController.orphan_processes`
went straight to ``build_rehydrator`` + :meth:`OrphanRehydrator.run`
so it could read the typed :class:`RehydrateResult` instead of this
wrapper's silent ``return 0``. The one remaining importer is
``tests/test_process_orphan_rehydrate.py``. The module survives as
the one-release alias window for ``_OrphanProcess`` /
``_OrphanProcStub``; when that window closes it should be deleted
and the test repointed at ``orphan_rehydrator``.

Backward-compat aliases ``_OrphanProcess`` / ``_OrphanProcStub``
are exposed via a module-level ``__getattr__`` that emits a
:class:`DeprecationWarning` — external callers get a loud hint to
switch to the public names during the one-release deprecation
window.
"""

from __future__ import annotations

import warnings
from pathlib import Path

from ember_code.core.tools.orphan_process import OrphanProcess
from ember_code.core.tools.orphan_rehydrator import (
    OrphanRehydrator,
    build_rehydrator,
)
from ember_code.core.tools.process_supervisor_locator import supervisors
from ember_code.core.tools.shell_orphan_schemas import (
    OrphanProcStub,
    OrphanReadResult,
    RehydrateResult,
)

__all__ = [
    "OrphanProcStub",
    "OrphanProcess",
    "OrphanReadResult",
    "OrphanRehydrator",
    "RehydrateResult",
    "rehydrate_orphan_processes",
]


async def rehydrate_orphan_processes(project_dir: str | Path | None) -> int:
    """Backward-compat wrapper around :meth:`OrphanRehydrator.run`.

    Preserves the ``int``-returning shape. Its only remaining caller
    is the test suite — ``RehydrateController.orphan_processes``
    builds an :class:`OrphanRehydrator` itself precisely to avoid the
    ``return 0`` below. New code should do the same so
    :class:`RehydrateResult` flows through with its populated
    ``reason`` field.

    Store-init failures fall through to the "return 0" branch —
    the caller can't distinguish store-init from "no rows" here,
    which is the gap the typed :class:`RehydrateResult` closes on
    the direct path.
    """
    supervisor = supervisors.default()
    rehydrator, build_failure = build_rehydrator(supervisor, project_dir)
    if rehydrator is None:
        # ``build_failure`` is populated when store construction
        # raised — legacy contract returns ``0`` in that case.
        return 0
    result = await rehydrator.run()
    return result.surfaced


# ── Deprecated names ────────────────────────────────────────────
#
# The old private names (``_OrphanProcess`` / ``_OrphanProcStub``)
# stay reachable for one release so cross-package imports keep
# resolving during the transition. The module-level ``__getattr__``
# emits a :class:`DeprecationWarning` so callers get a loud hint
# to switch instead of silently aliasing.


#: Deprecated name → (public class, module that now owns it). The
#: module is carried alongside because the warning used to name
#: ``shell_orphan`` as the new home — i.e. it told callers to keep
#: importing from the shim they were being moved off.
_DEPRECATED_ALIASES: dict[str, tuple[type, str]] = {
    "_OrphanProcess": (OrphanProcess, "ember_code.core.tools.orphan_process"),
    "_OrphanProcStub": (OrphanProcStub, "ember_code.core.tools.shell_orphan_schemas"),
}


def __getattr__(name: str) -> type:
    """Module-level ``__getattr__`` (PEP 562) for the deprecated
    underscored names. Any access to ``_OrphanProcess`` /
    ``_OrphanProcStub`` returns the public class + emits a warning
    so importers see the transition in their logs.
    """
    alias = _DEPRECATED_ALIASES.get(name)
    if alias is None:
        raise AttributeError(f"module 'shell_orphan' has no attribute {name!r}")
    replacement, home = alias
    warnings.warn(
        f"{name} is deprecated; import {replacement.__name__} from {home} instead",
        DeprecationWarning,
        stacklevel=2,
    )
    return replacement
