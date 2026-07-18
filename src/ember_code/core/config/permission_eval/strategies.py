"""Polymorphic mode dispatch for the permission pipeline.

Replaces the two parallel ``if / elif`` ladders that lived inside
``PermissionEvaluator._mode_step`` and ``explain_deny``. Each
:class:`PermissionMode` value gets its own strategy subclass; the
pipeline routes step 4 through ``strategy.mode_step(...)`` and the
reason-string production through ``strategy.deny_reason(...)``.

Adding a new mode = subclass + one entry in :meth:`_STRATEGY_MAP`.
No more editing two ladders in lockstep and hoping they stay in
sync.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, ClassVar

from ember_code.core.config.permission_eval.catalog import ToolCategoryCatalog
from ember_code.core.config.permission_eval.schemas import (
    PermissionDecision,
    PermissionMode,
)


class PermissionModeStrategy:
    """Base strategy — encapsulates one :class:`PermissionMode` value.

    Subclasses override :meth:`mode_step` (used by the pipeline's
    step 4) and :meth:`deny_reason` (used by
    :meth:`PermissionEvaluator.explain_deny` to produce a
    human-readable reason when this mode is what caused the deny).

    The base defaults are the pass-through shape: mode contributes
    no step-4 decision (returns ``DEFER``) and no mode-specific
    deny reason (returns ``None``). Concrete subclasses override
    where they diverge.
    """

    mode: ClassVar[PermissionMode]

    def mode_step(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        catalog: ToolCategoryCatalog,
    ) -> PermissionDecision:
        """Step 4 of the pipeline for this mode. Returns
        :attr:`PermissionDecision.DEFER` when the mode has nothing to
        say — the pipeline then continues to step 5 (allow) and
        step 6 (defer / dontAsk deny)."""
        return PermissionDecision.DEFER

    def deny_reason(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        catalog: ToolCategoryCatalog,
    ) -> str | None:
        """Mode-specific reason string when the pipeline decided
        DENY due to this mode's rules. Returns ``None`` when the
        deny didn't come from this mode."""
        return None

    def dont_ask_fallback(self) -> bool:
        """Does this mode treat the final step-6 fall-through as a
        DENY (rather than DEFER)? Only :class:`DontAskMode` returns
        ``True`` — headless mode has no user to prompt."""
        return False

    @classmethod
    def for_mode(cls, mode: PermissionMode) -> PermissionModeStrategy:
        """Return the strategy instance for ``mode``. One shared
        instance per mode (strategies are stateless), looked up per
        call so that mutating ``evaluator.mode`` at runtime picks up
        the new strategy on the very next :meth:`evaluate` call —
        no cached-property invalidation gymnastics."""
        return _STRATEGY_MAP[mode]


class DefaultMode(PermissionModeStrategy):
    """``default`` — no auto-decisions from the mode itself. Purely
    pass-through: rules decide, step 6 defers to the user."""

    mode = PermissionMode.DEFAULT


class DontAskMode(PermissionModeStrategy):
    """``dontAsk`` (headless / CI) — never prompts. Anything the
    pipeline can't decide from rules becomes DENY at step 6."""

    mode = PermissionMode.DONT_ASK

    def dont_ask_fallback(self) -> bool:
        return True

    def deny_reason(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        catalog: ToolCategoryCatalog,
    ) -> str | None:
        return f"headless mode (dontAsk) and {tool_name} is not in the allow list"


class AcceptEditsMode(PermissionModeStrategy):
    """``acceptEdits`` — auto-approves file-edit tools; everything
    else falls through to allow / defer."""

    mode = PermissionMode.ACCEPT_EDITS

    def mode_step(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        catalog: ToolCategoryCatalog,
    ) -> PermissionDecision:
        if catalog.is_edit(tool_name):
            return PermissionDecision.ALLOW
        return PermissionDecision.DEFER


class BypassPermissionsMode(PermissionModeStrategy):
    """``bypassPermissions`` — auto-allow anything not already denied
    or asked. Deny rules still win (that's step 2, before this
    step ever runs)."""

    mode = PermissionMode.BYPASS_PERMISSIONS

    def mode_step(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        catalog: ToolCategoryCatalog,
    ) -> PermissionDecision:
        return PermissionDecision.ALLOW


class PlanMode(PermissionModeStrategy):
    """``plan`` — blocks file mutations + mutating shell commands,
    auto-allows reads, defers unknown tools to the user."""

    mode = PermissionMode.PLAN

    _EDIT_REASON = (
        "plan mode blocks file edits. Use exit_plan_mode(plan) "
        "when you're ready for the user to approve execution."
    )
    _SHELL_REASON = (
        "plan mode blocks mutating shell commands (rm, mv, cp, "
        "mkdir, sed -i, > redirect, …). Read-only shell calls "
        "are fine. Use exit_plan_mode(plan) when ready to execute."
    )

    def mode_step(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        catalog: ToolCategoryCatalog,
    ) -> PermissionDecision:
        if catalog.is_edit(tool_name):
            return PermissionDecision.DENY
        if catalog.is_shell(tool_name):
            # Shell commands may or may not mutate. Block the
            # obvious writers (sed -i, > redirect, rm, mv, cp, ...)
            # and let read-only shell calls through.
            if catalog.bash_mutates(tool_args):
                return PermissionDecision.DENY
            return PermissionDecision.ALLOW
        if catalog.is_read(tool_name):
            return PermissionDecision.ALLOW
        # Custom / unknown tool — fall through to step 5/6 so the
        # user can decide. Don't auto-allow what we don't classify.
        return PermissionDecision.DEFER

    def deny_reason(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        catalog: ToolCategoryCatalog,
    ) -> str | None:
        if catalog.is_edit(tool_name):
            return self._EDIT_REASON
        if catalog.is_shell(tool_name):
            return self._SHELL_REASON
        return None


# One instance per mode. Built once at import time — strategies hold
# no per-evaluation state so sharing is safe. Wrapped in
# ``MappingProxyType`` so callers can't monkey-patch the shared table.
_STRATEGY_MAP: Mapping[PermissionMode, PermissionModeStrategy] = MappingProxyType(
    {
        PermissionMode.DEFAULT: DefaultMode(),
        PermissionMode.DONT_ASK: DontAskMode(),
        PermissionMode.ACCEPT_EDITS: AcceptEditsMode(),
        PermissionMode.BYPASS_PERMISSIONS: BypassPermissionsMode(),
        PermissionMode.PLAN: PlanMode(),
    }
)
