"""The 6-step evaluation pipeline.

Kept as a lean dataclass — the class holds live rule lists that
:mod:`ember_code.backend.hitl_controller` mutates at runtime, and
:mod:`ember_code.core.session.state_ops` reassigns
:attr:`PermissionEvaluator.mode` after a slash-command switches
posture. Pydantic frozen model isn't the right fit for that call
site; the dataclass shape matches what every existing caller
expects.

Composition: the evaluator holds a
:class:`FriendlyToolNameResolver`, a :class:`ToolCategoryCatalog`,
and looks up the current :class:`PermissionModeStrategy` per-call
so mutating ``.mode`` picks up the new strategy on the very next
:meth:`evaluate` call.

Concurrency: ``_mode_lock`` (``threading.RLock``) serializes the
mode-flip write with the per-tool read in :meth:`evaluate_outcome`.
The BE's WS orchestrator dispatches the slash-command handler
and the agent's tool-check hook as parallel ``asyncio`` tasks;
without the lock, a mode flip can land AFTER the agent's hook
has already read the old mode and decided to prompt the user.
Holding the lock for both sides (one write, one read) closes
the window. ``RLock`` (re-entrant) so the public ``set_mode``
wrapper can be called from a context that already holds the
lock without deadlocking.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from ember_code.core.config.permission_eval.catalog import ToolCategoryCatalog
from ember_code.core.config.permission_eval.resolver import FriendlyToolNameResolver
from ember_code.core.config.permission_eval.schemas import (
    PermissionDecision,
    PermissionMode,
    PermissionOutcome,
    PermissionRule,
)
from ember_code.core.config.permission_eval.strategies import PermissionModeStrategy


@dataclass
class PermissionEvaluator:
    """The 6-step evaluation pipeline.

    Order (deliberately different from Claude Code's contract —
    see note below):
      1. ``hooks`` — fired by the tool-event hook BEFORE this
         evaluator runs; not modelled here.
      2. ``deny`` — any matching deny rule → ``DENY``. Bypass-
         resistant: still wins in ``bypassPermissions`` mode.
      3. ``mode`` — mode-specific shortcut: ``acceptEdits``
         auto-allows file-edit tools, ``plan`` denies them,
         ``bypassPermissions`` allows everything not already
         denied, ``dontAsk`` denies anything not allowed.
      4. ``ask`` — any matching ask rule → ``ASK`` (caller asks
         the user / canUseTool).
      5. ``allow`` — any matching allow rule → ``ALLOW``.
      6. ``defer`` — return ``DEFER`` so the caller routes to its
         interactive/UI/canUseTool fallback.

    Note on the order swap (mode before ask): Claude Code runs
    ask BEFORE mode — that means an explicit ``ask: Bash`` rule
    wins even in ``bypassPermissions`` mode, which defeats the
    user's intent when they toggle bypass on. We swap the two so
    the mode shortcut takes priority over the ask list — deny is
    still the only "always wins" rule (the safety floor). Rationale:
    if the user typed ``/bypass on`` they want EVERYTHING auto-
    approved except explicit denials. If they want a specific
    tool to always ask, they should remove it from bypass mode
    (the footer chip is one click) — not add an ask rule that
    silently overrides the mode they just chose.
      5. ``allow`` — any matching allow rule → ``ALLOW``.
      6. ``defer`` — return ``DEFER`` so the caller routes to its
         interactive/UI/canUseTool fallback.

    ``mode`` is a read-through property backed by ``_mode`` so
    every read pairs with any concurrent ``set_mode`` write via
    ``_mode_lock``. Same instance across calls — the agent's hook
    pipeline holds a reference to the same evaluator object, so
    attribute reads observe the latest write.
    """

    _mode: PermissionMode = PermissionMode.DEFAULT
    _mode_lock: threading.RLock = field(default_factory=threading.RLock)
    deny: list[PermissionRule] = field(default_factory=list)
    ask: list[PermissionRule] = field(default_factory=list)
    allow: list[PermissionRule] = field(default_factory=list)
    catalog: ToolCategoryCatalog = field(default_factory=ToolCategoryCatalog.default)
    resolver: FriendlyToolNameResolver = field(default_factory=FriendlyToolNameResolver.default)

    @property
    def mode(self) -> PermissionMode:
        """Current mode — read under ``_mode_lock`` so it pairs
        with any concurrent ``set_mode`` write. Hot path (called
        per tool call) so the lock is held for μs, not blocking."""
        with self._mode_lock:
            return self._mode

    @mode.setter
    def mode(self, value: PermissionMode) -> None:
        """Direct setter — preserved for back-compat with the
        ``Session.set_permission_mode`` callers (which mutate
        the attribute directly). Holds ``_mode_lock`` so the read
        in :meth:`evaluate_outcome` sees the new value."""
        with self._mode_lock:
            self._mode = value

    def set_mode(self, value: PermissionMode) -> None:
        """Atomic mode flip — the preferred API. Acquires
        ``_mode_lock`` so the write is serialised against
        in-flight reads. Used by the HITL atomic-flip path on
        the BE (see ``HitlController.resolve_batch``)."""
        with self._mode_lock:
            self._mode = value

    @classmethod
    def from_strings(
        cls,
        mode: str | PermissionMode = PermissionMode.DEFAULT,
        deny: list[str] | None = None,
        ask: list[str] | None = None,
        allow: list[str] | None = None,
    ) -> PermissionEvaluator:
        """Convenience constructor — accepts raw strings from
        ``settings.permissions`` and parses them into
        :class:`PermissionRule` objects, silently dropping malformed
        entries (caller can check the lengths if it cares)."""
        return cls(
            _mode=PermissionMode(mode) if isinstance(mode, str) else mode,
            deny=PermissionRule.parse_many(deny or []),
            ask=PermissionRule.parse_many(ask or []),
            allow=PermissionRule.parse_many(allow or []),
        )

    # ── Pipeline entry points ─────────────────────────────────────

    def evaluate(self, tool_name: str, tool_args: dict[str, Any]) -> PermissionDecision:
        """Run the six-step pipeline and return just the
        :class:`PermissionDecision`. Compatibility shim over
        :meth:`evaluate_outcome` — callers that want the reason too
        should use :meth:`evaluate_outcome` (or the ``explain_deny``
        convenience method)."""
        return self.evaluate_outcome(tool_name, tool_args).decision

    def evaluate_outcome(self, tool_name: str, tool_args: dict[str, Any]) -> PermissionOutcome:
        """Run the six-step pipeline and return the full outcome
        (decision + reason + source-step). Produces the reason
        string in the same pass that produced the decision so
        :meth:`explain_deny` doesn't need a second scan of the
        deny list (kills the two-scan drift the audit flagged).

        The whole body runs under :attr:`_mode_lock` so the mode
        read in :meth:`_strategy` is atomic with any concurrent
        ``set_mode`` / ``mode = …`` write. Without this lock the
        BE's WS orchestrator could dispatch a slash-command
        mode-flip concurrently with the agent's tool-check hook —
        the hook would read the OLD mode and decide to prompt
        the user even though the user just flipped to bypass.
        """
        with self._mode_lock:
            return self._evaluate_outcome_locked(tool_name, tool_args)

    def _evaluate_outcome_locked(
        self, tool_name: str, tool_args: dict[str, Any]
    ) -> PermissionOutcome:
        """Locked body — split out so the lock acquire/release
        is the only thing in :meth:`evaluate_outcome`."""
        strategy = self._strategy()

        # Step 2: deny
        deny_rule = self._first_match(self.deny, tool_name, tool_args)
        if deny_rule is not None:
            return PermissionOutcome(
                decision=PermissionDecision.DENY,
                reason=deny_rule.deny_reason(),
                source="deny",
            )

        # Step 3: mode-specific shortcuts. Runs BEFORE the ask
        # list so ``/bypass on`` actually bypasses the user's
        # ``ask: Bash`` config (see class docstring for rationale).
        mode_decision = strategy.mode_step(tool_name, tool_args, self.catalog)
        if mode_decision is PermissionDecision.DENY:
            reason = (
                strategy.deny_reason(tool_name, tool_args, self.catalog)
                or f"{tool_name} is denied by policy"
            )
            return PermissionOutcome(
                decision=PermissionDecision.DENY,
                reason=reason,
                source="mode",
            )
        if mode_decision is PermissionDecision.ALLOW:
            return PermissionOutcome(
                decision=PermissionDecision.ALLOW,
                reason=f"{self.mode.value} mode auto-allowed {tool_name}",
                source="mode",
            )
        if mode_decision is PermissionDecision.ASK:
            return PermissionOutcome(
                decision=PermissionDecision.ASK,
                reason=f"{self.mode.value} mode asks about {tool_name}",
                source="mode",
            )

        # Step 4: ask
        ask_rule = self._first_match(self.ask, tool_name, tool_args)
        if ask_rule is not None:
            return PermissionOutcome(
                decision=PermissionDecision.ASK,
                reason=f"ask rule '{ask_rule.display()}' matched",
                source="ask",
            )

        # Step 5: allow
        allow_rule = self._first_match(self.allow, tool_name, tool_args)
        if allow_rule is not None:
            return PermissionOutcome(
                decision=PermissionDecision.ALLOW,
                reason=f"allow rule '{allow_rule.display()}' matched",
                source="allow",
            )

        # Step 6: defer (caller's canUseTool / interactive prompt)
        if strategy.dont_ask_fallback():
            # Headless mode: no prompts means anything unmatched
            # at this point is a deny, not a defer.
            reason = (
                strategy.deny_reason(tool_name, tool_args, self.catalog)
                or f"{tool_name} is denied by policy"
            )
            return PermissionOutcome(
                decision=PermissionDecision.DENY,
                reason=reason,
                source="mode",
            )
        return PermissionOutcome(
            decision=PermissionDecision.DEFER,
            reason=f"{tool_name} deferred to caller",
            source="defer",
        )

    def explain_deny(self, tool_name: str, tool_args: dict[str, Any]) -> str:
        """Human-readable reason ``evaluate(...)`` came back DENY.

        Threaded into the tool-rejection note so the **agent** — not
        just the user reading the dialog — knows what to do next.
        Without context the model treated a generic "Blocked by
        permission policy" as a hostile environment and asked the
        user to run the command manually; with this, it sees
        "plan mode blocks edits, call exit_plan_mode(plan) when
        ready" and routes correctly.

        Reads the reason produced by :meth:`evaluate_outcome` — no
        second scan of the deny list.
        """
        outcome = self.evaluate_outcome(tool_name, tool_args)
        if outcome.decision is PermissionDecision.DENY:
            return outcome.reason
        # Not actually a deny — fall back to the generic string so
        # callers that mistakenly ask for a deny reason on an
        # allow/ask/defer path still get a stable answer.
        return f"{tool_name} is denied by policy"

    # ── Helpers ───────────────────────────────────────────────────

    def _strategy(self) -> PermissionModeStrategy:
        """Look up the strategy for the current ``self.mode``.

        Not cached on the instance — ``state_ops.set_permission_mode``
        reassigns ``self.mode`` at runtime and a cached-property here
        would go stale. One dict lookup per evaluate() call is
        cheap; correctness first."""
        return PermissionModeStrategy.for_mode(self.mode)

    def _first_match(
        self,
        rules: list[PermissionRule],
        tool_name: str,
        tool_args: dict[str, Any],
    ) -> PermissionRule | None:
        """Return the first rule that matches, or ``None``. Same
        traversal as the old free ``_any_match`` but returns the
        matching rule so :meth:`evaluate_outcome` can build the
        reason string in-line."""
        for rule in rules:
            if rule.matches(tool_name, tool_args, self.resolver):
                return rule
        return None
