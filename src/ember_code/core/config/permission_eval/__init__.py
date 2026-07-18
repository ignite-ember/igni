"""Claude Code-style 6-mode tool permission evaluator.

Package: parses ``Tool(pattern)`` rules, holds a
:class:`PermissionMode`, and walks the 6-step evaluation pipeline
(``hooks → deny → ask → mode → allow → defer``). No I/O, no
network, no interactive prompts — those happen in the layer that
calls this evaluator (the tool-event hook, eventually a UI bridge).

Modelled on ``code.claude.com/docs/en/agent-sdk/permissions``. The
TS-only ``auto`` mode (model classifier) is intentionally absent
from the Python surface.

The key safety invariant: a deny rule with a scope pattern (e.g.
``Bash(rm *)``) STILL blocks matching invocations in
``bypassPermissions`` mode. Only bare-name denies (e.g. plain
``Bash``) follow the "remove the tool from context" shortcut and
that lives at a different layer.

Layout:

* :mod:`.schemas`      — :class:`PermissionMode`,
                         :class:`PermissionDecision`,
                         :class:`PermissionOutcome`,
                         :class:`PermissionRule` (Pydantic).
* :mod:`.resolver`     — :class:`FriendlyToolNameResolver` (owns the
                         friendly ↔ internal reverse index; used to
                         live as a module-level cache).
* :mod:`.catalog`      — :class:`ToolCategoryCatalog` and
                         :class:`BashCommand` (categories + shell
                         mutation heuristic).
* :mod:`.strategies`   — :class:`PermissionModeStrategy` hierarchy
                         (one subclass per :class:`PermissionMode`
                         value; replaces the two if/elif ladders).
* :mod:`.pipeline`     — :class:`PermissionEvaluator` — the 6-step
                         pipeline itself.

Out of scope for this refactor (documented follow-up): consolidating
this package's :class:`PermissionRule` with
:class:`ember_code.core.config.tool_permissions.schemas.PermissionRule`.
The two types serve different purposes (this one drives the six-step
pipeline; that one persists to the settings store with an extra
``level`` field). Full consolidation touches hitl_controller,
tests, and the regex divergence — a separate ticket.
"""

from __future__ import annotations

from typing import Any

# Import order matters — schemas → catalog + resolver → strategies →
# pipeline. Anything earlier in that chain must not depend on
# anything later. (Verified: schemas only imports resolver *inside*
# a helper function to break the cycle.)
from ember_code.core.config.permission_eval.catalog import (
    FILE_EDIT_TOOLS,
    FILE_READ_TOOLS,
    SHELL_TOOLS,
    BashCommand,
    ToolCategoryCatalog,
)
from ember_code.core.config.permission_eval.pipeline import PermissionEvaluator
from ember_code.core.config.permission_eval.resolver import FriendlyToolNameResolver
from ember_code.core.config.permission_eval.schemas import (
    PermissionDecision,
    PermissionMode,
    PermissionOutcome,
    PermissionOutcomeSource,
    PermissionRule,
)
from ember_code.core.config.permission_eval.strategies import (
    AcceptEditsMode,
    BypassPermissionsMode,
    DefaultMode,
    DontAskMode,
    PermissionModeStrategy,
    PlanMode,
)


def explain_deny(
    evaluator: PermissionEvaluator,
    tool_name: str,
    tool_args: dict[str, Any],
) -> str:
    """Backwards-compatible shim.

    Forwards to :meth:`PermissionEvaluator.explain_deny` so the
    existing import (``from ember_code.core.config.permission_eval
    import explain_deny``) keeps working. The real implementation
    is the method — call sites should migrate to
    ``evaluator.explain_deny(...)`` when touched.
    """
    return evaluator.explain_deny(tool_name, tool_args)


__all__ = [
    # Core public surface
    "PermissionMode",
    "PermissionDecision",
    "PermissionOutcome",
    "PermissionOutcomeSource",
    "PermissionRule",
    "PermissionEvaluator",
    "explain_deny",
    # Catalog / heuristics — re-exported for legacy imports
    "FILE_EDIT_TOOLS",
    "FILE_READ_TOOLS",
    "SHELL_TOOLS",
    "BashCommand",
    "ToolCategoryCatalog",
    # Resolver
    "FriendlyToolNameResolver",
    # Strategy hierarchy (mainly for tests that want to assert the
    # concrete strategy type for a given mode)
    "PermissionModeStrategy",
    "DefaultMode",
    "DontAskMode",
    "AcceptEditsMode",
    "BypassPermissionsMode",
    "PlanMode",
]
