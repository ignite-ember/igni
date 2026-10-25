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
from ember_code.core.config.permission_eval.catalog import BashCommand
from ember_code.core.hooks.events import HookEvent
from ember_code.core.hooks.hook_firer import HookFirer
from ember_code.core.hooks.schemas import SafetyCheckResult
from ember_code.core.hooks.tool_events import (
    PermissionDeniedPayload,
    PermissionRequestPayload,
    PreToolUsePayload,
)
from ember_code.core.mcp.hints import ToolHintsRegistry

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
    later skipped), :class:`Block` on ``deny`` / ``ask`` / legacy
    ``should_continue=False``, and :class:`Continue` otherwise.
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
            # A hook's ``ask`` cannot reach a human from here, and the
            # two ways it used to end were chosen by whether the hook
            # happened to also set ``systemMessage``: set → block,
            # unset → run the tool. So the same plugin, written to CC's
            # semantics ("ask" = prompt me), was a guard or a rubber
            # stamp depending on a field that says nothing about intent.
            #
            # Fails closed now. This is NOT the v0.8.3 case: that
            # regression was the *evaluator*'s ASK re-firing after
            # Agno's HITL dialog had already resolved a pause (see
            # :class:`PermissionEvaluatorStage`). Agno's pause is driven
            # by ``requires_confirmation``, which no hook can set, so a
            # hook returning ``ask`` is a fresh request nobody answered.
            await self._firer.fire(
                HookEvent.PERMISSION_REQUEST,
                ctx.name,
                PermissionRequestPayload.from_call(ctx.name, ctx.args),
            )
            await self._firer.fire(
                HookEvent.PERMISSION_DENIED,
                ctx.name,
                PermissionDeniedPayload.from_call(ctx.name, ctx.args, "pre_tool_use_hook"),
            )
            logger.info(
                "PreToolUse hook returned ASK for %s — no prompt surface at this "
                "stage, so it is denied rather than auto-approved",
                ctx.name,
            )
            return Block(
                pre_result.message
                or (
                    f"Blocked: a PreToolUse hook asked for confirmation of '{ctx.name}', "
                    "and hooks have no way to prompt the user. Ask the user directly, or "
                    "have the hook return allow/deny."
                )
            )
        if pre_decision == "allow":
            return AllowSkip()
        # Legacy ``should_continue=False`` block (no permission_decision).
        if not pre_result.should_continue:
            return Block(pre_result.message or "Blocked by PreToolUse hook")
        return Continue()


#: The keys :func:`_target_path` will look under, named so a test can hold them
#: against the real function signatures. The bug this guards was a lookup key
#: the tool did not have; a test that restates the keys instead of importing
#: them cannot see that happen again.
PATH_ARG_KEYS: tuple[str, ...] = ("file_path", "file_name", "path")

#: Likewise for the shell stage. ``BashCommand.from_args`` reads both, in this
#: order, and this is the declaration of that.
SHELL_ARG_KEYS: tuple[str, ...] = ("command", "args")


def _target_path(args: dict[str, Any]) -> str:
    """The path a write tool is about to write to.

    Three spellings because the write family does not agree on one:
    ``edit_file`` / ``edit_file_replace_all`` / ``create_file`` take
    ``file_path``, and agno's ``FileTools.save_file`` takes ``file_name``.
    ``ProtectedPathStage`` listed ``save_file`` as gated and then looked only
    for ``file_path``, so the lookup missed and every ``save_file`` write went
    through unguarded — ``.env``, ``*.pem``, ``*.key``, ``credentials.*``
    included. The other three worked, which is why three of four paths working
    hid it.

    ``path`` is here for tools that have not needed gating yet; a guard that
    has to be edited to cover the next one is how this happened.
    """
    for key in PATH_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


class ProtectedPathStage(PermissionStage):
    """Hard-coded defense-in-depth: block writes to protected
    paths. ALWAYS runs — a ``PreToolUse`` ``allow`` cannot unlock
    this, matching CC's bypass-resistant scoped-deny threat
    model.
    """

    #: Function names that mutate the filesystem via the file-toolkit
    #: family. The protected-paths check gates writes to any of these.
    #
    # The ``notebook_*`` writers were missing, and they take the same
    # ``file_path`` argument ``_target_path`` already reads — so a
    # ``.ipynb`` under a protected pattern was the one write this
    # defense-in-depth list did not see.
    WRITE_TOOL_FUNCTIONS: frozenset[str] = frozenset(
        {
            "save_file",
            "edit_file",
            "edit_file_replace_all",
            "create_file",
            "notebook_edit_cell",
            "notebook_add_cell",
            "notebook_remove_cell",
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
        file_path = _target_path(ctx.args)
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
                _target_path(ctx.args),
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
        """The shell command as one string, whatever shape it arrived in.

        This read ``args["args"]`` and nothing else. That was correct for agno's
        ``ShellTools.run_shell_command(args: list[str])``, which is what the
        stage was written against; :class:`EmberShellTools` replaced it with
        ``run_shell_command(command: str)`` and this was not updated. The lookup
        missed on every real call, the join returned ``""``, and no blocked
        pattern could match — so the deny list never blocked anything.

        :meth:`BashCommand.from_args` already absorbs all four shapes and is
        already used by the evaluator, so this defers to it rather than keeping
        a second, narrower answer to the same question in a second place.
        """
        return BashCommand.from_args(args).command

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

    That fall-through is only sound because ASK reaching here means a
    pause happened and a human answered it. In a mode that never pauses
    it would be a silent approval, so the evaluator no longer produces
    ASK there — ``dontAsk`` resolves it to DENY before this stage sees
    it (:meth:`PermissionEvaluator._ask_or_deny`).
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
        # ``evaluate_outcome``, not ``evaluate``: the reason is produced in
        # the same pass as the decision and is the only place the model is
        # told what would unblock it. Discarding it left a plan-mode edit
        # denial reading "Blocked: permission policy denied 'edit_file'."
        # with no mention of ``exit_plan_mode(plan)`` — the one call that
        # lifts the block — in the TUI and in headless ``-p``, where no
        # pause dialog exists to carry the reason instead.
        outcome = self._evaluator.evaluate_outcome(ctx.name, ctx.args)
        if outcome.decision is PermissionDecision.DENY:
            await self._firer.fire(
                HookEvent.PERMISSION_DENIED,
                ctx.name,
                PermissionDeniedPayload.from_call(ctx.name, ctx.args, "permission_evaluator"),
            )
            logger.info("Permission DENY for %s: %s", ctx.name, outcome.reason)
            return Block(
                _denial_message(
                    ctx.name,
                    reason=outcome.reason,
                    tool_is_off_entirely=self._tool_is_off_entirely(ctx.name),
                )
            )
        if outcome.decision is PermissionDecision.ASK:
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

    def _tool_is_off_entirely(self, tool_name: str) -> bool:
        """Is this tool denied for *every* argument list, or just this one?

        Asked by re-evaluating the tool with no arguments, which is the
        question itself rather than a guess at it: a bare ``deny`` rule
        and ``dontAsk``'s step-6 fall-through both still deny, while a
        scoped ``Bash(rm *)`` no longer matches and plan mode answers
        ALLOW because an argument-free shell call does not mutate.

        The distinction decides whether :data:`_DENIAL_ALTERNATIVES` is
        attached, and it was not being made: every ``run_shell_command``
        denial told the model "Shell access is off in this session",
        including the plan-mode one whose own reason says read-only
        shell calls are fine. Two sentences apart, contradicting each
        other, on the path whose job is to stop a retry.

        Cold path — one extra evaluate() per denial, and only for the
        tools that carry an alternatives paragraph at all.
        """
        if self._evaluator is None or tool_name not in _DENIAL_ALTERNATIVES:
            return False
        return self._evaluator.evaluate(tool_name, {}) is PermissionDecision.DENY


#: What to suggest when a denial would otherwise leave the model with no move.
#:
#: ``--read-only`` denies the shell, and igni reads files through the shell — so
#: the first thing an agent tries in a read-only session is the thing it cannot
#: do. A bare "permission policy denied 'run_shell_command'" tells it nothing
#: about whether to stop, rephrase, or try a different tool, and a model with no
#: stated alternative tends to retry the same call in a new costume.
#:
#: Phrased as "if this session has it" on purpose: the same denial arrives under
#: ``--strict``, where the alternatives are denied too, and promising a tool that
#: is also blocked would just move the dead end one step later.
#:
#: Only attached when the tool is denied for every argument list — see
#: :meth:`PermissionEvaluatorStage._tool_is_off_entirely`. In plan mode and under
#: a scoped deny rule the shell still works for the calls that were not denied,
#: and telling the model otherwise costs it the moves it actually had.
_DENIAL_ALTERNATIVES: dict[str, str] = {
    "run_shell_command": (
        " Shell access is off in this session, which also rules out reading "
        "files with cat/sed. If another of your tools can answer the question "
        "without the shell, use it; otherwise say what you would have run and "
        "why, rather than retrying."
    ),
}


def _denial_message(
    tool_name: str,
    reason: str = "",
    tool_is_off_entirely: bool = True,
) -> str:
    """The block text the model receives, plus a way forward where one exists.

    ``reason`` is the evaluator's own explanation for this decision — it
    carries the mode-specific escape hatch (``exit_plan_mode(plan)``,
    "read-only shell calls are fine") that the model otherwise never sees.

    ``tool_is_off_entirely`` defaults to ``True`` so a caller that has not
    measured it gets the old blanket phrasing; the pipeline always measures.
    """
    text = f"Blocked: permission policy denied '{tool_name}'."
    if reason:
        text += f" {reason}"
    if tool_is_off_entirely:
        text += _DENIAL_ALTERNATIVES.get(tool_name, "")
    return text


# ── PermissionPipeline coordinator ─────────────────────────────────


class BypassGuardStage(PermissionStage):
    """Bypass mode's last check — see :mod:`~ember_code.core.hooks.bypass_guard`.

    Runs only after every other stage let the call through, so an
    explicit deny, a protected path or a blocked command still wins
    first, and only in Bypass does it do anything.

    An extension's tool is checked like igni's own: the guard waves
    through igni's local reads, and so an extension tool whose manifest
    says it only reads its own data is waved through here. Every other
    extension tool gets the same check as a shell command.
    """

    def __init__(self, guard: Any | None, tool_hints: ToolHintsRegistry | None = None) -> None:
        self._guard = guard
        self._tool_hints = tool_hints if tool_hints is not None else ToolHintsRegistry.shared()

    async def evaluate(self, ctx: ToolCallContext) -> StageOutcome:
        if self._guard is None:
            return Continue()
        hints = self._tool_hints.get(ctx.name)
        if hints is not None and hints.is_local_read:
            return Continue()
        agent = getattr(ctx.agent, "name", None)
        message = await self._guard.check(ctx.name, ctx.args, [agent] if agent else [])
        return Block(message) if message else Continue()


class PermissionPipeline:
    """Ordered runner over :class:`PermissionStage` instances.

    Order (fixed by construction — CC-compatible):

    1. :class:`PreToolUseHookStage` — hooks get first pass.
    2. :class:`ProtectedPathStage` — defense-in-depth, always runs.
    3. :class:`BlockedCommandStage` — defense-in-depth, always runs.
    4. :class:`PermissionEvaluatorStage` — 6-mode rules; skipped
       when stage 1 returned :class:`AllowSkip`.
    5. :class:`BypassGuardStage` — in Bypass only, puts a possibly
       harmful call to the person before it runs.

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
        bypass_guard: Any | None = None,
    ) -> None:
        self._pre = PreToolUseHookStage(firer)
        self._protected = ProtectedPathStage(protected_paths)
        self._blocked = BlockedCommandStage(blocked_commands)
        self._evaluator_stage = PermissionEvaluatorStage(permission_evaluator, firer)
        self._bypass_guard = BypassGuardStage(bypass_guard)

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

        guard_outcome = await self._bypass_guard.evaluate(ctx)
        if isinstance(guard_outcome, Block):
            return guard_outcome.message
        return None
