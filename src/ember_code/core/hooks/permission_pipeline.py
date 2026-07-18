"""Permission pipeline — ordered gates that run BEFORE a tool call.

Replaces the numbered-comment 5-step procedural pipeline that
used to live inside :meth:`ToolEventHook.__call__`. Each gate is
a :class:`PermissionStage` subclass with one ``evaluate`` method
that returns a :class:`StageOutcome` — a tagged union of

* :class:`Continue` — this stage passes, run the next stage.
* :class:`Block` — short-circuit the whole pipeline with this
  user-facing message.
* :class:`AllowSkip` — the ``PreToolUse`` hook returned ``allow``,
  so subsequent permission-evaluator gates should be skipped.
  Legacy safety-list gates (:class:`ProtectedPathStage` /
  :class:`BlockedCommandStage`) still run — a hook ``allow``
  cannot unlock a write to ``.env``.

The pipeline order is fixed by :class:`PermissionPipeline` and
mirrors the CC-compatible ordering: hooks first (so plugins can
allow/deny/ask), then hard-coded safety lists (defense in depth),
then the 6-mode evaluator (skippable by an ``allow`` hook).
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ember_code.core.config.permission_eval import (
    PermissionDecision,
    PermissionEvaluator,
)
from ember_code.core.hooks.events import HookEvent
from ember_code.core.hooks.hook_firer import HookFirer
from ember_code.core.hooks.schemas import SafetyCheckResult
from ember_code.core.hooks.tool_events import (
    PermissionDeniedPayload,
    PermissionRequestPayload,
    PreToolUsePayload,
)

logger = logging.getLogger(__name__)


# ── StageOutcome tagged union ──────────────────────────────────────
#
# Plain ``@dataclass(frozen=True)`` sum types — no Pydantic on the
# hot path. ``isinstance(...)`` checks in the pipeline coordinator
# make the invariants type-level rather than "did we set the right
# combination of Optionals?".


@dataclass(frozen=True)
class Continue:
    """Stage passed — run the next stage."""


@dataclass(frozen=True)
class Block:
    """Stage short-circuits the pipeline with a user-facing
    ``message``. :class:`PermissionPipeline` returns ``message``
    directly to the model."""

    message: str


@dataclass(frozen=True)
class AllowSkip:
    """A ``PreToolUse`` hook explicitly allowed the call — the
    permission-evaluator stage should be skipped (matches CC's
    ``permissionDecision: allow`` semantics). Safety-list stages
    still run BEFORE this decision reaches the evaluator gate."""


StageOutcome = Continue | Block | AllowSkip
"""Union alias used by every stage's ``evaluate`` return type."""


@dataclass(frozen=True)
class ToolCallContext:
    """Immutable per-call context threaded through every stage /
    invoker / suffixer. Replaces the ``(name, func, args)``
    positional tuple that used to hop between helpers on the
    ``ToolEventHook`` — a single object closes AP5 (typed at
    every internal boundary) while accepting Agno's untyped
    ``__call__`` seam at the outer edge.
    """

    name: str
    func: Any  # Callable | None — ``Any`` skips Pydantic introspection.
    args: dict[str, Any]
    agent: Any = None

    @classmethod
    def for_check(cls, name: str, args: dict[str, Any]) -> ToolCallContext:
        """Test / sync-check factory — build a context with
        ``func=None, agent=None`` for callers that want to invoke
        a stage's pure ``check_sync`` predicate without the Agno
        ``__call__`` machinery.

        The synchronous safety-list checks
        (:meth:`ProtectedPathStage.check_sync`,
        :meth:`BlockedCommandStage.check_sync`) never look at
        ``ctx.func`` / ``ctx.agent``, so both defaulting to ``None``
        is safe. Production ``ToolEventHook.__call__`` still
        constructs the full context with every field populated.
        """
        return cls(name=name, func=None, args=args, agent=None)


# ── PermissionStage ABC + concrete gates ───────────────────────────


class PermissionStage:
    """Abstract base — every gate implements ``evaluate``.

    Stages are ordered by :class:`PermissionPipeline`; each one
    returns a :class:`StageOutcome` that drives the coordinator's
    next step.
    """

    async def evaluate(self, ctx: ToolCallContext) -> StageOutcome:
        raise NotImplementedError


class PreToolUseHookStage(PermissionStage):
    """Fires ``PreToolUse`` and interprets the merged
    :class:`HookResult`.

    Emits :class:`AllowSkip` on ``allow`` (evaluator gate is
    later skipped), :class:`Block` on ``deny`` / legacy
    ``should_continue=False`` (with the deny event fired), and
    :class:`Continue` otherwise. On ``ask`` we fire the
    ``PermissionRequest`` event for observability then fall
    through — Agno's HITL dialog has already resolved.
    """

    def __init__(self, firer: HookFirer) -> None:
        self._firer = firer

    async def evaluate(self, ctx: ToolCallContext) -> StageOutcome:
        pre_result = await self._firer.fire(
            HookEvent.PRE_TOOL_USE,
            ctx.name,
            PreToolUsePayload.from_call(ctx.name, ctx.args),
        )
        pre_decision = (pre_result.permission_decision or "").lower()
        if pre_decision == "deny":
            await self._firer.fire(
                HookEvent.PERMISSION_DENIED,
                ctx.name,
                PermissionDeniedPayload.from_call(ctx.name, ctx.args, "pre_tool_use_hook"),
            )
            return Block(pre_result.message or f"Blocked: '{ctx.name}' denied by PreToolUse hook")
        if pre_decision == "ask":
            await self._firer.fire(
                HookEvent.PERMISSION_REQUEST,
                ctx.name,
                PermissionRequestPayload.from_call(ctx.name, ctx.args),
            )
            if pre_result.message:
                return Block(pre_result.message)
            logger.info(
                "PreToolUse hook returned ASK for %s — falling through to HITL",
                ctx.name,
            )
            return Continue()
        if pre_decision == "allow":
            return AllowSkip()
        # Legacy ``should_continue=False`` block (no permission_decision).
        if not pre_result.should_continue:
            return Block(pre_result.message or "Blocked by PreToolUse hook")
        return Continue()


class ProtectedPathStage(PermissionStage):
    """Hard-coded defense-in-depth: block writes to protected
    paths. ALWAYS runs — a ``PreToolUse`` ``allow`` cannot unlock
    this, matching CC's bypass-resistant scoped-deny threat
    model.
    """

    #: Function names that mutate the filesystem via the file-toolkit
    #: family. The protected-paths check gates writes to any of these.
    WRITE_TOOL_FUNCTIONS: frozenset[str] = frozenset(
        {
            "save_file",
            "edit_file",
            "edit_file_replace_all",
            "create_file",
        }
    )

    def __init__(self, protected_paths: list[str]) -> None:
        self._protected_paths = protected_paths

    @classmethod
    def applies_to(cls, tool_name: str) -> bool:
        """Return ``True`` when this stage's protected-path check
        gates ``tool_name``.

        Encapsulates the ``in WRITE_TOOL_FUNCTIONS`` membership test
        so external callers don't reach into the classvar frozenset
        from outside. The frozenset itself stays public for
        reflection uses (e.g. a test iterating every gated write
        tool).
        """
        return tool_name in cls.WRITE_TOOL_FUNCTIONS

    @staticmethod
    def matches_pattern(path: str, protected_patterns: list[str]) -> bool:
        """Return ``True`` when ``path`` matches any pattern in
        ``protected_patterns`` — basename OR full-path match, so
        both a ``".env"`` basename pattern and a
        ``"**/credentials.json"`` full-path pattern trip.
        """
        filename = Path(path).name
        for pattern in protected_patterns:
            if fnmatch.fnmatch(filename, pattern):
                return True
            if fnmatch.fnmatch(path, pattern):
                return True
        return False

    def check_sync(self, ctx: ToolCallContext) -> SafetyCheckResult:
        """Pure synchronous predicate: does this tool call violate
        the protected-paths list?

        Kept as a sync method (no ``async def``) so callers that
        already have a :class:`ToolCallContext` can gate on the
        result without spinning up an event loop. The async
        :meth:`evaluate` wraps this with logging + StageOutcome
        translation.

        Returns :meth:`SafetyCheckResult.no_block` when the check
        does not apply (empty protected list, non-write tool,
        no ``file_path`` arg) OR when the file_path is not
        protected. Returns :meth:`SafetyCheckResult.block` with a
        user-facing message when the write is blocked.
        """
        if not self._protected_paths:
            return SafetyCheckResult.no_block()
        if not self.applies_to(ctx.name):
            return SafetyCheckResult.no_block()
        file_path = ctx.args.get("file_path", "")
        if not file_path:
            return SafetyCheckResult.no_block()
        if self.matches_pattern(file_path, self._protected_paths):
            return SafetyCheckResult.block(
                f"Blocked: '{file_path}' is a protected path and cannot be written to."
            )
        return SafetyCheckResult.no_block()

    async def evaluate(self, ctx: ToolCallContext) -> StageOutcome:
        """Async stage entry — delegates to :meth:`check_sync` for
        the pure predicate, then logs + translates the
        :class:`SafetyCheckResult` into a :class:`StageOutcome`.

        Logging lives HERE (not in ``check_sync``) so tests that
        exercise the sync seam directly don't spam WARNING logs,
        and so observability stays on the wire path where
        :meth:`evaluate` actually runs.
        """
        result = self.check_sync(ctx)
        if result.blocked:
            logger.warning(
                "Protected path blocked: %s via %s",
                ctx.args.get("file_path", ""),
                ctx.name,
            )
            return Block(result.block_message)
        return Continue()


class BlockedCommandStage(PermissionStage):
    """Hard-coded defense-in-depth: block shell commands matching
    a configured deny list. ALWAYS runs — same threat model as
    :class:`ProtectedPathStage`.
    """

    #: Function names that spawn shell commands. The blocked-commands
    #: check gates every call to these against the configured deny list.
    SHELL_TOOL_FUNCTIONS: frozenset[str] = frozenset({"run_shell_command"})

    def __init__(self, blocked_commands: list[str]) -> None:
        self._blocked_commands = blocked_commands

    @classmethod
    def applies_to(cls, tool_name: str) -> bool:
        """Return ``True`` when this stage's blocked-command check
        gates ``tool_name``.

        Symmetric with :meth:`ProtectedPathStage.applies_to` — the
        classvar frozenset stays public for reflection while the
        membership test becomes a proper method.
        """
        return tool_name in cls.SHELL_TOOL_FUNCTIONS

    @staticmethod
    def _join_args(args: dict[str, Any]) -> str:
        """Join the ``args`` field of a shell tool call into a
        single string for substring matching.

        Callers may pass a list (typical) or a single string
        (some paths pre-join); both are normalised to the same
        space-joined string form.
        """
        cmd_args = args.get("args", [])
        if isinstance(cmd_args, list):
            return " ".join(str(a) for a in cmd_args)
        return str(cmd_args)

    def check_sync(self, ctx: ToolCallContext) -> SafetyCheckResult:
        """Pure synchronous predicate: does this shell call
        contain any blocked command pattern?

        Returns :meth:`SafetyCheckResult.no_block` when the check
        does not apply (empty block list, non-shell tool) OR when
        no blocked pattern matches. Returns
        :meth:`SafetyCheckResult.block` with a user-facing message
        on the first pattern match.
        """
        if not self._blocked_commands:
            return SafetyCheckResult.no_block()
        if not self.applies_to(ctx.name):
            return SafetyCheckResult.no_block()
        cmd_str = self._join_args(ctx.args)
        for blocked in self._blocked_commands:
            if blocked in cmd_str:
                return SafetyCheckResult.block(
                    f"Blocked: command matches blocked pattern '{blocked}'."
                )
        return SafetyCheckResult.no_block()

    async def evaluate(self, ctx: ToolCallContext) -> StageOutcome:
        """Async stage entry — delegates to :meth:`check_sync` for
        the pure predicate, then logs + translates. Logging stays
        HERE so ``check_sync`` remains a quiet pure predicate.
        """
        result = self.check_sync(ctx)
        if result.blocked:
            logger.warning("Blocked command: %s", self._join_args(ctx.args))
            return Block(result.block_message)
        return Continue()


class PermissionEvaluatorStage(PermissionStage):
    """6-mode permission evaluator (allow/ask/deny × plan/normal
    modes). Skipped by :class:`PermissionPipeline` when a
    previous stage emitted :class:`AllowSkip`.

    DENY blocks with the evaluator's message + fires
    ``PermissionDenied``. ASK fires ``PermissionRequest`` for
    observability then falls through — Agno's HITL dialog has
    already resolved. ALLOW / DEFER pass through cleanly.
    """

    def __init__(
        self,
        evaluator: PermissionEvaluator | None,
        firer: HookFirer,
    ) -> None:
        self._evaluator = evaluator
        self._firer = firer

    async def evaluate(self, ctx: ToolCallContext) -> StageOutcome:
        if self._evaluator is None:
            return Continue()
        decision = self._evaluator.evaluate(ctx.name, ctx.args)
        if decision is PermissionDecision.DENY:
            await self._firer.fire(
                HookEvent.PERMISSION_DENIED,
                ctx.name,
                PermissionDeniedPayload.from_call(ctx.name, ctx.args, "permission_evaluator"),
            )
            logger.info("Permission DENY for %s", ctx.name)
            return Block(f"Blocked: permission policy denied '{ctx.name}'.")
        if decision is PermissionDecision.ASK:
            await self._firer.fire(
                HookEvent.PERMISSION_REQUEST,
                ctx.name,
                PermissionRequestPayload.from_call(ctx.name, ctx.args),
            )
            logger.info(
                "Permission ASK for %s — falling through to HITL (already resolved by Agno)",
                ctx.name,
            )
        return Continue()


# ── PermissionPipeline coordinator ─────────────────────────────────


class PermissionPipeline:
    """Ordered runner over :class:`PermissionStage` instances.

    Order (fixed by construction — CC-compatible):

    1. :class:`PreToolUseHookStage` — hooks get first pass.
    2. :class:`ProtectedPathStage` — defense-in-depth, always runs.
    3. :class:`BlockedCommandStage` — defense-in-depth, always runs.
    4. :class:`PermissionEvaluatorStage` — 6-mode rules; skipped
       when stage 1 returned :class:`AllowSkip`.

    :meth:`evaluate` returns the block message on the first
    :class:`Block`, or ``None`` if every stage says continue /
    allow-skip.
    """

    def __init__(
        self,
        firer: HookFirer,
        protected_paths: list[str],
        blocked_commands: list[str],
        permission_evaluator: PermissionEvaluator | None,
    ) -> None:
        self._pre = PreToolUseHookStage(firer)
        self._protected = ProtectedPathStage(protected_paths)
        self._blocked = BlockedCommandStage(blocked_commands)
        self._evaluator_stage = PermissionEvaluatorStage(permission_evaluator, firer)

    async def evaluate(self, ctx: ToolCallContext) -> str | None:
        """Run every stage in order. Return the first
        :class:`Block` message, or ``None`` when the pipeline
        greenlights the call.
        """
        skip_evaluator = False
        pre_outcome = await self._pre.evaluate(ctx)
        if isinstance(pre_outcome, Block):
            return pre_outcome.message
        if isinstance(pre_outcome, AllowSkip):
            skip_evaluator = True

        protected_outcome = await self._protected.evaluate(ctx)
        if isinstance(protected_outcome, Block):
            return protected_outcome.message

        blocked_outcome = await self._blocked.evaluate(ctx)
        if isinstance(blocked_outcome, Block):
            return blocked_outcome.message

        if skip_evaluator:
            return None

        evaluator_outcome = await self._evaluator_stage.evaluate(ctx)
        if isinstance(evaluator_outcome, Block):
            return evaluator_outcome.message
        return None
