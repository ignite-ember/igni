"""Hook executor — thin orchestrator that fans an event out to
matching hooks and merges their results.

The real work lives in siblings:

* :class:`~ember_code.core.hooks.matcher.HookMatcher` — the
  tri-mode matcher DSL.
* :class:`~ember_code.core.hooks.registry.HookRegistry` — the
  event → hooks index.
* :class:`~ember_code.core.hooks.handlers.HookHandlerRegistry` —
  hook-type → handler dispatch.
* :class:`~ember_code.core.hooks.merger.HookResultMerger` —
  precedence rules for multi-hook merges.

This module keeps the ``HookExecutor.__init__`` /
``get_matching_hooks`` / ``execute`` surface intact so
downstream callers (session/core.py, tool_hook.py,
orchestrate.py, backend/server_scheduler.py) don't move.

Module-level ``_matcher_matches`` and
``_hook_result_from_envelope`` shims stay for the two test
modules that import them directly — each is a two-line
delegator to the new class.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ember_code.core.hooks.envelope import HookEnvelope
from ember_code.core.hooks.events import HookEvent
from ember_code.core.hooks.handlers import HookHandlerRegistry, MCPResolver
from ember_code.core.hooks.matcher import HookMatcher
from ember_code.core.hooks.merger import HookResultMerger
from ember_code.core.hooks.registry import HookRegistry
from ember_code.core.hooks.schemas import (
    HookDefinition,
    HookPayload,
    HookPayloadBase,
    HookResult,
)

logger = logging.getLogger(__name__)


__all__ = [
    "HookExecutor",
    "MCPResolver",
    # Compat shims for existing test imports — do not remove
    # without also rewriting the corresponding test module.
    "_matcher_matches",
    "_hook_result_from_envelope",
]


class HookExecutor:
    """Fan an event out to matching hooks and merge results."""

    def __init__(
        self,
        hooks: dict[str, list[HookDefinition]],
        mcp_resolver: MCPResolver | None = None,
        rewake_callback: Callable[[str], None] | None = None,
    ):
        # ``.hooks`` stays public as the raw dict — tool_hook.py
        # (and any plugin doing hot-reloads) reads it directly.
        self.hooks = hooks
        self._registry = HookRegistry(hooks)
        self._handler_registry = HookHandlerRegistry(
            mcp_resolver=mcp_resolver,
            rewake_callback=rewake_callback,
        )

    def get_matching_hooks(self, event: str, target: str = "") -> list[HookDefinition]:
        """Delegate to :class:`HookRegistry`.

        Matcher syntax (CC-compatible — see :class:`HookMatcher`):
        empty / ``"*"`` matches all; alphanumeric (with optional
        ``|`` alternatives) is an exact / pipe-list-exact match;
        anything else is a regex.
        """
        return self._registry.for_event_and_target(event, target)

    def has_hooks_for(self, event: HookEvent) -> bool:
        """Fast "does anything subscribe to this event?" check.

        Lets :class:`HookFirer` short-circuit before building a
        typed payload for an event no one listens to. Reads the
        live ``.hooks`` dict every call — a plugin that hot-reloads
        hooks gets picked up immediately (no stale flag caching
        at ``ToolEventHook.__init__``).
        """
        return bool(self.hooks.get(event.value))

    async def execute(
        self,
        event: str,
        payload: HookPayloadBase | dict[str, Any],
        target: str = "",
    ) -> HookResult:
        """Execute all matching hooks for an event.

        Foreground hooks run in parallel and are awaited — if
        ANY hook blocks (exit 2), the tool call is blocked.
        Background hooks are fire-and-forget.
        """
        hooks = self._registry.for_event_and_target(event, target)
        if not hooks:
            return HookResult(should_continue=True)

        typed_payload = HookPayload.coerce(payload)
        fg_hooks = HookRegistry.foreground(hooks)
        bg_hooks = HookRegistry.background(hooks)

        self._launch_background(bg_hooks, event, typed_payload)
        self._launch_rewake(fg_hooks, event, typed_payload)
        return await self._await_foreground(fg_hooks, event, typed_payload)

    # ── Subclass override point (see tests/test_hook_events_new.py) ─

    def _dispatch(
        self,
        hook: HookDefinition,
        event: str,
        payload: HookPayloadBase,
    ) -> Awaitable[HookResult] | None:
        """Dispatch one hook to its handler.

        Returns ``None`` for unknown hook types (parity with the
        pre-refactor debug-log-and-continue). Extension point for
        subclasses / recording test doubles that intercept
        single-hook dispatch.
        """
        if not self._handler_registry.supports(hook.type):
            logger.debug("Unknown hook type %r — skipping", hook.type)
            return None
        return self._run_hook(hook, event, payload)

    async def _run_hook(
        self,
        hook: HookDefinition,
        event: str,
        payload: HookPayloadBase,
    ) -> HookResult:
        """Coroutine wrapper around the handler registry's
        ``.run`` — flattens the ``Optional[HookResult]`` to a
        concrete result so downstream fan-out can gather without
        None-handling in the merge loop."""
        result = await self._handler_registry.run(hook, event, payload)
        if result is None:
            return HookResult(should_continue=True)
        return result

    # ── Internal fan-out ────────────────────────────────────────────

    def _launch_background(
        self,
        bg_hooks: list[HookDefinition],
        event: str,
        payload: HookPayloadBase,
    ) -> None:
        """Fire-and-forget background hooks."""
        for hook in bg_hooks:
            coro = self._dispatch(hook, event, payload)
            if coro is not None:
                asyncio.create_task(coro)

    def _launch_rewake(
        self,
        fg_hooks: list[HookDefinition],
        event: str,
        payload: HookPayloadBase,
    ) -> None:
        """``async_rewake`` implies a background dispatch (a sync
        hook with rewake would be a contradiction — the agent
        would block waiting for itself). Coerce here so users
        don't have to set both flags.
        """
        for hook in fg_hooks:
            if not hook.async_rewake:
                continue
            coro = self._dispatch(hook, event, payload)
            if coro is not None:
                asyncio.create_task(coro)

    async def _await_foreground(
        self,
        fg_hooks: list[HookDefinition],
        event: str,
        payload: HookPayloadBase,
    ) -> HookResult:
        """Run non-rewake foreground hooks in parallel, merge
        their :class:`HookResult` instances via
        :class:`HookResultMerger`."""
        tasks: list[Awaitable[HookResult]] = []
        for hook in fg_hooks:
            if hook.async_rewake:
                continue
            coro = self._dispatch(hook, event, payload)
            if coro is not None:
                tasks.append(coro)
        if not tasks:
            return HookResult(should_continue=True)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        merger = HookResultMerger()
        for result in results:
            if isinstance(result, BaseException):
                logger.debug("Hook raised — skipping in merge: %r", result)
                continue
            merger.absorb(result)
        return merger.finalize()


# ── Backwards-compat module-level shims ─────────────────────────────
#
# ``tests/test_hook_matcher.py`` imports ``_matcher_matches`` and
# ``tests/test_hook_envelope_parser.py`` imports
# ``_hook_result_from_envelope`` from this module. Both are now
# thin delegators to the new classes — the OOP mandate treats
# these as "compat shim, not a free function on state" because
# they take primitive inputs (a pattern string / a raw value)
# and produce primitive outputs.


def _matcher_matches(pattern: str, target: str) -> bool:
    """Compat shim — delegates to :class:`HookMatcher`."""
    return HookMatcher(pattern).matches(target)


def _hook_result_from_envelope(result: Any) -> HookResult:
    """Compat shim — delegates to :class:`HookEnvelope`.

    Preserves the pre-refactor tolerance:

    * ``dict`` → parse via :meth:`HookEnvelope.from_raw` and
      :meth:`HookEnvelope.to_result`.
    * ``None`` → non-blocking empty.
    * Anything else (str, list, int, ...) → non-blocking with
      ``message = str(result)``.
    """
    envelope = HookEnvelope.from_raw(result)
    if envelope is not None:
        return envelope.to_result()
    if result is None:
        return HookResult(should_continue=True)
    return HookResult(should_continue=True, message=str(result))
