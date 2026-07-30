"""Typed index over event → hook-definitions.

Owns the ``dict[str, list[HookDefinition]]`` accumulator that used
to leak out of :class:`HookLoader` as a bare dict. Callers ask
questions in the domain (``for_event``, ``for_event_and_target``,
``foreground``, ``background``) or use the mutation API
(``append``, ``prepend``, :meth:`merge_from_dict`) instead of
poking at the dict directly.

The raw dict is still reachable via :attr:`raw` for the six-plus
downstream call-sites that iterate it as a dict — importantly the
dict is returned by identity (no copy) so downstream mutations via
``session.hooks_map`` continue to reflect in
:class:`HookExecutor.hooks`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ember_code.core.hooks.schemas import (
    HookDefinition,
    HookLoadWarning,
    MergeStrategy,
)

logger = logging.getLogger(__name__)


class HookRegistry:
    """Event-keyed index over :class:`HookDefinition`.

    Kept intentionally thin — no copying, no locking. The raw
    dict is shared with :class:`HookExecutor` (still exposed as
    ``.hooks`` for backward compat with tool_hook.py's
    ``executor.hooks.get(...)`` reads) so mutations upstream
    (e.g. plugin hot-reloads appending events) are visible here
    immediately.
    """

    def __init__(self, hooks: dict[str, list[HookDefinition]] | None = None):
        # Instance-owned to keep the class's mutation API from
        # accidentally sharing state across independent registries.
        self._hooks: dict[str, list[HookDefinition]] = hooks if hooks is not None else {}

    @classmethod
    def from_empty(cls) -> HookRegistry:
        """Fresh, empty registry — the shape :meth:`HookLoader.load`
        starts with. Kept as an explicit constructor so call sites
        that build a registry (session boot, plugin hot-reload, tests)
        read consistently.
        """
        return cls({})

    # ── Read surface ─────────────────────────────────────────────────

    @property
    def raw(self) -> dict[str, list[HookDefinition]]:
        """The underlying dict, exposed for callers still shaped
        around dict access (``executor.hooks.get(event)``,
        ``session.hooks_map[...]``).

        Returns the same dict object on every call — mutations
        made against the returned dict are visible to the registry
        and to any consumer holding a shared reference. That's
        deliberate: session boot hands the dict to
        :class:`HookExecutor` so a plugin hot-reload can append
        events and the executor sees them without a rebuild.
        """
        return self._hooks

    @property
    def total_hooks(self) -> int:
        """Sum of hook counts across every event.

        Replaces the ``sum(len(hl) for hl in hooks_map.values())``
        call-sites in ``session.reload_hooks`` and
        ``plugin_reload.reload`` — keeps the counting logic in one
        place so a future migration to a non-dict backing store
        doesn't need to update every counter.
        """
        return sum(len(hl) for hl in self._hooks.values())

    def for_event(self, event: str) -> list[HookDefinition]:
        """All hooks registered for ``event`` (regardless of matcher)."""
        return self._hooks.get(event, [])

    def for_event_and_target(self, event: str, target: str = "") -> list[HookDefinition]:
        """Hooks registered for ``event`` that match ``target``.

        Empty ``target`` returns every hook on the event — that
        matches the pre-refactor short-circuit where callers
        without a specific target (session lifecycle events,
        scheduler events) skipped matching entirely.
        """
        event_hooks = self.for_event(event)
        if not target:
            return event_hooks
        return [h for h in event_hooks if h.matches(target)]

    @staticmethod
    def foreground(hooks: list[HookDefinition]) -> list[HookDefinition]:
        """The ones that block the tool call (default)."""
        return [h for h in hooks if not h.background]

    @staticmethod
    def background(hooks: list[HookDefinition]) -> list[HookDefinition]:
        """The fire-and-forget ones."""
        return [h for h in hooks if h.background]

    # ── Mutation surface ─────────────────────────────────────────────

    def append(self, event: str, hook: HookDefinition) -> None:
        """Add *hook* to the tail of *event*'s bucket."""
        self._hooks.setdefault(event, []).append(hook)

    def prepend(self, event: str, hook: HookDefinition) -> None:
        """Add *hook* to the head of *event*'s bucket.

        Used for plugin hooks so plugin-supplied behavior fires
        *before* project hooks — letting the project still get
        the final word (e.g. a project ``PreToolUse`` veto runs
        after a plugin's ``PreToolUse`` audit log).
        """
        self._hooks.setdefault(event, []).insert(0, hook)

    def merge_from_dict(
        self,
        hooks_data: dict[str, Any],
        *,
        source: Path,
        strategy: MergeStrategy,
    ) -> list[HookLoadWarning]:
        """Parse an ``{event: [hook, ...]}`` block and merge it in.

        Ownership of the merge lives on the registry (not the
        loader) so both settings-file loads and plugin loads reach
        for the same parser without re-stating the schema.

        ``strategy`` picks between :class:`MergeStrategy.APPEND`
        (settings files layer on top of each other, later wins)
        and :class:`MergeStrategy.PREPEND` (plugins fire before
        project hooks so the project retains the veto).

        Warnings are RETURNED, not raised or printed — the caller
        chooses how to surface them. Malformed hook blocks
        (non-list buckets, non-dict entries, validation errors)
        yield warnings and skip the entry rather than tanking the
        whole file — parity with the pre-refactor
        ``.get(default)`` tolerance.
        """
        warnings: list[HookLoadWarning] = []
        for event_name, hook_list in hooks_data.items():
            if not isinstance(hook_list, list):
                warnings.append(
                    HookLoadWarning.from_path(
                        source,
                        "non_dict_block",
                        f"event {event_name!r} is not a list; skipping",
                    )
                )
                continue
            for hook_data in hook_list:
                if not isinstance(hook_data, dict):
                    warnings.append(
                        HookLoadWarning.from_path(
                            source,
                            "non_dict_hook",
                            f"hook entry in {event_name!r} is not a JSON object",
                        )
                    )
                    continue
                try:
                    hook = HookDefinition.from_wire(hook_data)
                except ValidationError as e:
                    warnings.append(
                        HookLoadWarning.from_path(
                            source,
                            "validation_error",
                            f"malformed hook in {event_name!r}: {e}",
                        )
                    )
                    continue
                if strategy is MergeStrategy.PREPEND:
                    self.prepend(event_name, hook)
                else:
                    self.append(event_name, hook)
        return warnings
