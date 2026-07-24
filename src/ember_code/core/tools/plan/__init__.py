"""Plan mode — Claude Code parity (row 50).

A user-toggled mode where the agent can read / search / think but
**cannot** write files or run mutating shell commands. The
enforcement lives in :class:`PermissionEvaluator` (already
implemented as ``PermissionMode.PLAN`` with row 7); this package
adds the missing pieces:

* :class:`PlanStore` — per-session capture of the latest plan
  the agent submitted via ``exit_plan_mode``. Surfaced to the UI
  via the ``GET_LATEST_PLAN`` RPC.
* :class:`PlanTool` — registers ``exit_plan_mode(plan)`` +
  ``enter_plan_mode(reason, task)`` so the agent can toggle
  into plan mode and signal "I'm done planning" at the end of a
  plan-mode turn. The tool does NOT flip the mode out of plan
  automatically — the user controls that via ``/plan`` so the
  agent can't exit the sandbox on its own.
* :class:`PlanConfidenceValidator` — the "plan must be grounded"
  gate; owns the attempts counter, threshold constants, and
  rejection-feedback prose.
* :class:`PlanResearcherRunner` — spawns the
  ``plan_researcher`` sub-agent via :class:`OrchestrateTools`
  when :meth:`PlanTool.enter_plan_mode` is called with
  ``task=...``.

The complementary half (``/plan`` slash command +
``Session.set_permission_mode``) lives in ``backend/command_handler.py``
and ``core/session/core.py`` respectively.

This module was a single 517-LoC file until the OOP split; every
symbol the old file exported (including private ``_MAX_PLAN_ATTEMPTS``
etc. read by tests) is re-exported here so import paths remain
stable.
"""

from ember_code.core.tools.plan.researcher import PlanResearcherRunner
from ember_code.core.tools.plan.schemas import (
    PermissionModeChangedPayload,
    PlanEnterResult,
    PlanExitInput,
    PlanExitResult,
    PlanSubmittedPayload,
    PlanTaskInput,
)
from ember_code.core.tools.plan.store import (
    PlanDecision,
    PlanDecisionsBlob,
    PlanSnapshot,
    PlanStore,
)
from ember_code.core.tools.plan.submission import (
    PlanSessionShapeError,
    PlanTransactionCoordinator,
)
from ember_code.core.tools.plan.tool import PlanTool
from ember_code.core.tools.plan.validator import (
    FILE_PATH_RE,
    MAX_PLAN_ATTEMPTS,
    MIN_FILE_CITATIONS,
    ConfidenceVerdict,
    PlanConfidenceValidator,
)

# Back-compat aliases for the historic private-symbol imports
# (tests read ``_MAX_PLAN_ATTEMPTS`` for the attempt cap). The
# underscored names remain the "wire" spelling; the un-
# underscored ones on ``validator`` are the intra-package API.
_MAX_PLAN_ATTEMPTS = MAX_PLAN_ATTEMPTS
_MIN_FILE_CITATIONS = MIN_FILE_CITATIONS
_FILE_PATH_RE = FILE_PATH_RE


__all__ = [
    "ConfidenceVerdict",
    "FILE_PATH_RE",
    "MAX_PLAN_ATTEMPTS",
    "MIN_FILE_CITATIONS",
    "PermissionModeChangedPayload",
    "PlanConfidenceValidator",
    "PlanDecision",
    "PlanDecisionsBlob",
    "PlanEnterResult",
    "PlanExitInput",
    "PlanExitResult",
    "PlanResearcherRunner",
    "PlanSessionShapeError",
    "PlanSnapshot",
    "PlanStore",
    "PlanSubmittedPayload",
    "PlanTaskInput",
    "PlanTool",
    "PlanTransactionCoordinator",
    "_MAX_PLAN_ATTEMPTS",
    "_MIN_FILE_CITATIONS",
    "_FILE_PATH_RE",
]
