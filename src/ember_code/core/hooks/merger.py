"""Merge multiple parallel :class:`HookResult` instances into one.

Extracted from :class:`HookExecutor` so the precedence rules
(``deny > ask > allow > defer > ""``) live in one place with a
proper class-level table, instead of a bunch of loop-local
booleans + a mid-function dict literal.
"""

from __future__ import annotations

from typing import ClassVar

from ember_code.core.hooks.schemas import HookResult, PermissionDecision


class HookResultMerger:
    """Accumulator for the fan-out merge in :meth:`HookExecutor.execute`.

    Semantics preserved from the pre-refactor loop:

    * Any hook with ``should_continue=False`` flips the merged
      value to False.
    * Non-empty messages are joined with ``\\n``.
    * The strongest ``permission_decision`` wins (deny > ask >
      allow > defer > "" — security beats convenience).
    * Exceptions from hooks are ignored (non-blocking errors).
    """

    # Class-level precedence table — no reason for this to be
    # rebuilt on every merge. Higher rank wins in a merge.
    # Keyed by :class:`PermissionDecision` enum members so a rename
    # of a wire value would fail at import time rather than silently
    # ranking as zero.
    _DECISION_PRIORITY: ClassVar[dict[PermissionDecision, int]] = {
        PermissionDecision.DENY: 4,
        PermissionDecision.ASK: 3,
        PermissionDecision.ALLOW: 2,
        PermissionDecision.DEFER: 1,
        PermissionDecision.NONE: 0,
    }

    def __init__(self) -> None:
        self._should_continue: bool = True
        self._messages: list[str] = []
        self._decision: PermissionDecision = PermissionDecision.NONE

    def absorb(self, result: HookResult) -> None:
        """Fold a single hook's result into the running merge."""
        if not result.should_continue:
            self._should_continue = False
        if result.message:
            self._messages.append(result.message)
        # ``result.permission_decision`` is typed as
        # :class:`PermissionDecision` but tolerate a bare string
        # from a raw-dict caller by round-tripping through
        # :meth:`PermissionDecision.from_wire`.
        pd = PermissionDecision.from_wire(result.permission_decision)
        if self._rank(pd) > self._rank(self._decision):
            self._decision = pd

    def finalize(self) -> HookResult:
        """Emit the merged :class:`HookResult`."""
        return HookResult(
            should_continue=self._should_continue,
            message="\n".join(self._messages),
            permission_decision=self._decision,
        )

    @classmethod
    def _rank(cls, decision: PermissionDecision) -> int:
        return cls._DECISION_PRIORITY.get(decision, 0)
