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
"""

from __future__ import annotations

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

    Order (matching Claude Code's contract):
      1. ``hooks`` — fired by the tool-event hook BEFORE this
         evaluator runs; not modelled here.
      2. ``deny`` — any matching deny rule → ``DENY``. Bypass-
         resistant: still wins in ``bypassPermissions`` mode.
      3. ``ask`` — any matching ask rule → ``ASK`` (caller asks
         the user / canUseTool).
      4. ``mode`` — mode-specific shortcut: ``acceptEdits``
         auto-allows file-edit tools, ``plan`` denies them,
         ``bypassPermissions`` allows everything not already
         denied/asked, ``dontAsk`` denies anything not allowed.
      5. ``allow`` — any matching allow rule → ``ALLOW``.
      6. ``defer`` — return ``DEFER`` so the caller routes to its
         interactive/UI/canUseTool fallback.
    """

    mode: PermissionMode = PermissionMode.DEFAULT
    deny: list[PermissionRule] = field(default_factory=list)
    ask: list[PermissionRule] = field(default_factory=list)
    allow: list[PermissionRule] = field(default_factory=list)
    catalog: ToolCategoryCatalog = field(default_factory=ToolCategoryCatalog.default)
    resolver: FriendlyToolNameResolver = field(default_factory=FriendlyToolNameResolver.default)

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
            mode=PermissionMode(mode) if isinstance(mode, str) else mode,
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
        deny list (kills the two-scan drift the audit flagged)."""
        strategy = self._strategy()

        # Step 2: deny
        deny_rule = self._first_match(self.deny, tool_name, tool_args)
        if deny_rule is not None:
            return PermissionOutcome(
                decision=PermissionDecision.DENY,
                reason=deny_rule.deny_reason(),
                source="deny",
            )

        # Step 3: ask
        ask_rule = self._first_match(self.ask, tool_name, tool_args)
        if ask_rule is not None:
            return PermissionOutcome(
                decision=PermissionDecision.ASK,
                reason=f"ask rule '{ask_rule.display()}' matched",
                source="ask",
            )

        # Step 4: mode-specific shortcuts
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
