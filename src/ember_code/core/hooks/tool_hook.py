"""Tool event hook — fires PreToolUse/PostToolUse/PostToolUseFailure events.

Agno fires ``tool_hooks`` around every tool call. This async hook works
in Agno's async execution chain (``aexecute``). For sync tools, Agno
wraps them in the async chain and our hook properly handles both sync
and async ``func`` via ``inspect.isawaitable``.
"""

import asyncio
import contextlib
import inspect
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ember_code.core.config.permission_eval import (
    PermissionDecision,
    PermissionEvaluator,
)
from ember_code.core.hooks.events import HookEvent
from ember_code.core.hooks.executor import HookExecutor
from ember_code.core.hooks.safety_lists import (
    _is_protected_path,
    check_blocked_commands,
    check_protected_paths,
)
from ember_code.core.hooks.schemas import HookResult
from ember_code.core.utils.rules_index import RulesIndex

logger = logging.getLogger(__name__)

# Argument names from which we'll harvest a path to consult the
# rules index after a successful tool call. Covers every
# file-tool entrypoint in the toolkit (read / edit / save /
# create / list-dir / grep) without needing per-tool wiring.
_PATH_ARG_NAMES = ("file_path", "path", "filename", "directory", "dir")

__all__ = ["ToolEventHook", "_is_protected_path"]


class ToolEventHook:
    """Async Agno tool_hook for pre/post events and protected paths."""

    def __init__(
        self,
        executor: HookExecutor,
        session_id: str = "",
        protected_paths: list[str] | None = None,
        blocked_commands: list[str] | None = None,
        rules_index: RulesIndex | None = None,
        project_dir: Path | None = None,
        permission_evaluator: PermissionEvaluator | None = None,
    ):
        # Mark instance as coroutine function so Agno uses aexecute() path
        if hasattr(inspect, "markcoroutinefunction"):
            inspect.markcoroutinefunction(self)
        else:
            # Python 3.11 fallback — set the flag manually
            self._is_coroutine = asyncio.coroutines._is_coroutine
        self._executor = executor
        self._session_id = session_id
        self._protected_paths = protected_paths or []
        self._blocked_commands = blocked_commands or []
        self._rules_index = rules_index
        self._project_dir = project_dir
        # Claude Code-style permission system runs ALONGSIDE the
        # legacy ``protected_paths``/``blocked_commands`` lists —
        # both contribute denies. A None evaluator skips the
        # 6-step pipeline entirely (parity with pre-evaluator
        # behavior).
        self._permission_evaluator = permission_evaluator
        self._has_pre = bool(executor.hooks.get(HookEvent.PRE_TOOL_USE.value))
        self._has_post = bool(executor.hooks.get(HookEvent.POST_TOOL_USE.value))
        self._has_fail = bool(executor.hooks.get(HookEvent.POST_TOOL_USE_FAILURE.value))

    async def _run_pre_hook(
        self, name: str, args: dict[str, Any]
    ) -> tuple[str, str | None]:
        """Fire ``PreToolUse`` and interpret its result.

        Returns ``(pre_decision, block_message)``:

        * ``pre_decision`` — normalised decision string
          (``"allow" / "deny" / "ask" / ""``). Callers read this
          to decide whether the permission evaluator step should
          be skipped.
        * ``block_message`` — non-None when the request MUST be
          short-circuited. ``__call__`` returns it directly to the
          model.

        CC's permission pipeline puts hooks FIRST so they can
        ``permissionDecision: allow`` or ``deny`` and short-
        circuit the rest of the checks. The legacy
        protected-paths/blocked-commands lists run AFTER as a
        defense-in-depth safety net (a hook saying ``allow`` can't
        unlock a hard-blocked ``.env`` write — that's a safety
        bug magnet we won't ship).
        """
        if not self._has_pre:
            return "", None
        pre_result = await self._fire(
            HookEvent.PRE_TOOL_USE.value,
            name,
            {"tool_name": name, "tool_args": _safe_args(args)},
        )
        pre_decision = (pre_result.permission_decision or "").lower()
        if pre_decision == "deny":
            await self._fire(
                HookEvent.PERMISSION_DENIED.value,
                name,
                {
                    "session_id": self._session_id,
                    "tool_name": name,
                    "tool_args": _safe_args(args),
                    "reason": "pre_tool_use_hook",
                },
            )
            return pre_decision, (
                pre_result.message or f"Blocked: '{name}' denied by PreToolUse hook"
            )
        if pre_decision == "ask":
            # Same rationale as the evaluator's ASK branch below —
            # Agno's PauseRequirement handles ASK via the HITL
            # dialog; re-blocking here would undo a user's
            # just-clicked approval. Fire the event for
            # observability, then fall through. If the hook set
            # an explicit ``message``, honour it as a block
            # (custom hook semantics stay intact).
            await self._fire(
                HookEvent.PERMISSION_REQUEST.value,
                name,
                {
                    "session_id": self._session_id,
                    "tool_name": name,
                    "tool_args": _safe_args(args),
                },
            )
            if pre_result.message:
                return pre_decision, pre_result.message
            logger.info(
                "PreToolUse hook returned ASK for %s — falling through to HITL",
                name,
            )
        # Legacy ``should_continue=False`` block (no
        # permission_decision) — keep the existing semantics.
        if pre_decision == "" and not pre_result.should_continue:
            return pre_decision, (pre_result.message or "Blocked by PreToolUse hook")
        return pre_decision, None

    async def _apply_permission_evaluator(
        self, name: str, args: dict[str, Any]
    ) -> str | None:
        """Run the 6-mode permission evaluator. Returns a block
        message on DENY (with the deny event fired), None on
        ALLOW / DEFER / ASK.

        ASK is Agno's territory now: tools at ASK level carry
        ``requires_confirmation=True`` (wired in v0.8.1's toolkit
        init), so by the time the tool call reaches this hook
        Agno's PauseRequirement has already fired the HITL
        dialog and the user has explicitly approved. Re-blocking
        here would undo that approval — the exact "I clicked
        Allow similar and it still says 'no canUseTool bridge'"
        regression. We keep the ``PERMISSION_REQUEST`` event fire
        so observability hooks / plugins can still see ASK
        traffic, but we FALL THROUGH to the actual tool call.
        """
        if self._permission_evaluator is None:
            return None
        decision = self._permission_evaluator.evaluate(name, args)
        if decision is PermissionDecision.DENY:
            await self._fire(
                HookEvent.PERMISSION_DENIED.value,
                name,
                {
                    "session_id": self._session_id,
                    "tool_name": name,
                    "tool_args": _safe_args(args),
                    "reason": "permission_evaluator",
                },
            )
            logger.info("Permission DENY for %s", name)
            return f"Blocked: permission policy denied '{name}'."
        if decision is PermissionDecision.ASK:
            await self._fire(
                HookEvent.PERMISSION_REQUEST.value,
                name,
                {
                    "session_id": self._session_id,
                    "tool_name": name,
                    "tool_args": _safe_args(args),
                },
            )
            logger.info(
                "Permission ASK for %s — falling through to HITL (already resolved by Agno)",
                name,
            )
        # ALLOW and DEFER both fall through to execution.
        return None

    async def _execute_with_post_hooks(
        self, name: str, func: Callable, args: dict[str, Any]
    ) -> Any:
        """Run the tool, fire PostToolUse or PostToolUseFailure,
        then suffix any newly-discovered subdirectory rules to a
        string result. Raises on tool exception AFTER firing the
        failure hook."""
        error = None
        result = None
        try:
            result = func(**args)
            if inspect.isawaitable(result):
                result = await result
        except Exception as e:
            error = e

        if error is not None:
            if self._has_fail:
                await self._fire(
                    HookEvent.POST_TOOL_USE_FAILURE.value,
                    name,
                    {"tool_name": name, "tool_args": _safe_args(args), "error": str(error)},
                )
            raise error

        if self._has_post:
            await self._fire(
                HookEvent.POST_TOOL_USE.value,
                name,
                {
                    "tool_name": name,
                    "tool_args": _safe_args(args),
                    "result_preview": _preview(result),
                },
            )

        # Discover & inject subdirectory rules for any new directory
        # the agent just touched. Done AFTER PostToolUse so audit
        # logs see the unmodified result, then we suffix the rules
        # block for the model's next reasoning step. Quiet no-op
        # when no rules index is wired or no new rules are pending.
        return await self._maybe_suffix_rules(args, result)

    async def __call__(
        self,
        name: str = "",
        func: Callable | None = None,
        args: dict[str, Any] | None = None,
        agent: Any = None,
        **kwargs: Any,
    ) -> Any:
        if args is None:
            args = {}

        # ── Step 1: PreToolUse hook (may allow / deny / ask) ───
        pre_decision, pre_block = await self._run_pre_hook(name, args)
        if pre_block is not None:
            return pre_block

        # ── Step 2: legacy protected-paths (defense-in-depth) ──
        # ALWAYS runs — a PreToolUse "allow" cannot disarm the
        # hard-coded safety list. Same threat model as CC's
        # bypass-resistant scoped denies: hooks and modes should
        # never be able to silently unlock writes to ``.env`` /
        # ``*.key`` / etc. Helper in ``safety_lists.py``.
        protected_block = check_protected_paths(name, args, self._protected_paths)
        if protected_block is not None:
            return protected_block

        # ── Step 3: legacy blocked-commands (defense-in-depth) ─
        # Same "always runs" property as protected-paths.
        blocked_msg = check_blocked_commands(name, args, self._blocked_commands)
        if blocked_msg is not None:
            return blocked_msg

        # ── Step 4: permission evaluator (6-mode pipeline) ──
        # Skipped when the PreToolUse hook returned "allow" —
        # this is the CC-compatible escape hatch for plugins
        # that want to grant ad-hoc approval. Scoped denies in
        # the evaluator are STILL honoured because they fire via
        # the evaluator's own pipeline; the hook ``allow`` only
        # skips the evaluator's step, not its earlier deny check
        # (which doesn't apply since we skip the whole step here
        # — but the protected-paths above already caught the
        # safety-critical cases).
        if pre_decision != "allow":
            eval_block = await self._apply_permission_evaluator(name, args)
            if eval_block is not None:
                return eval_block

        # ── Step 5: execute tool + post hooks + rules suffix ──
        if func is None:
            return None
        return await self._execute_with_post_hooks(name, func, args)

    async def _maybe_suffix_rules(self, args: dict[str, Any], result: Any) -> Any:
        """Append any newly-discovered rules files for the paths in
        ``args`` to a string ``result``. Non-string results pass
        through untouched (binary returns, structured dicts, …).

        Also fires the ``InstructionsLoaded`` hook with the list of
        rules files surfaced, so plugins can observe which rules
        kicked in for which tool calls (debugging path-scoped /
        subdir rules wiring is the headline use case).
        """
        if self._rules_index is None or not isinstance(result, str):
            return result
        candidate_paths: list[Path] = []
        for key in _PATH_ARG_NAMES:
            v = args.get(key)
            if not isinstance(v, str) or not v:
                continue
            p = Path(v)
            if not p.is_absolute() and self._project_dir is not None:
                p = self._project_dir / p
            candidate_paths.append(p)
        if not candidate_paths:
            return result
        discovered: list[tuple[Path, str]] = []
        for p in candidate_paths:
            discovered.extend(self._rules_index.consume_path(p))
        if not discovered:
            return result
        parts: list[str] = []
        files_payload: list[str] = []
        total_bytes = 0
        for rules_path, content in discovered:
            label = rules_path
            if self._project_dir is not None:
                with contextlib.suppress(ValueError):
                    label = rules_path.relative_to(self._project_dir)
            files_payload.append(str(label))
            total_bytes += len(content.encode("utf-8"))
            parts.append(
                f'<discovered-rules path="{label}">\n{content.strip()}\n</discovered-rules>'
            )
        # InstructionsLoaded — observers see exactly which rules
        # files surfaced for this tool call. Non-blocking by design;
        # we don't honour ``should_continue`` here because the rules
        # have already been inlined into the tool result.
        await self._fire(
            HookEvent.INSTRUCTIONS_LOADED.value,
            "",
            {
                "session_id": self._session_id,
                "source": "rules_index",
                "files": files_payload,
                "bytes": total_bytes,
            },
        )
        block = "\n".join(parts)
        return f"{result}\n\n{block}"

    async def _fire(self, event: str, target: str, payload: dict[str, Any]) -> HookResult:
        hooks = self._executor.get_matching_hooks(event, target)
        if not hooks:
            return HookResult(should_continue=True)
        payload["session_id"] = self._session_id
        try:
            return await self._executor.execute(event=event, payload=payload, target=target)
        except Exception as exc:
            logger.debug("Hook %s/%s failed: %s", event, target, exc)
            return HookResult(should_continue=True)


def _safe_args(args: dict[str, Any]) -> dict[str, str]:
    safe = {}
    for k, v in args.items():
        s = str(v)
        safe[k] = s[:500] if len(s) > 500 else s
    return safe


def _preview(result: Any) -> str:
    if result is None:
        return ""
    s = str(result)
    return s[:500] if len(s) > 500 else s
