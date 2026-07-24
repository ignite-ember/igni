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
:func:`rehydrate_orphan_processes` wrapper for the two existing
call sites (:class:`RehydrateController.orphan_processes` and the
test suite) so this refactor stays a surgical diff.

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

    Preserves the ``int``-returning shape the two existing call
    sites (``RehydrateController.orphan_processes`` and the test
    suite) expect. New code should build an
    :class:`OrphanRehydrator` directly so :class:`RehydrateResult`
    flows through with its populated ``reason`` field.

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


_DEPRECATED_ALIASES: dict[str, type] = {
    "_OrphanProcess": OrphanProcess,
    "_OrphanProcStub": OrphanProcStub,
}


def __getattr__(name: str) -> type:
    """Module-level ``__getattr__`` (PEP 562) for the deprecated
    underscored names. Any access to ``_OrphanProcess`` /
    ``_OrphanProcStub`` returns the public class + emits a warning
    so importers see the transition in their logs.
    """
    replacement = _DEPRECATED_ALIASES.get(name)
    if replacement is None:
        raise AttributeError(f"module 'shell_orphan' has no attribute {name!r}")
    warnings.warn(
        f"{name} is deprecated; import "
        f"{replacement.__name__} from ember_code.core.tools.shell_orphan instead",
        DeprecationWarning,
        stacklevel=2,
    )
    return replacement
