"""``prompt`` hook handler — no side effect, just injects the
configured text back to the agent as a system reminder.
"""

from __future__ import annotations

from typing import ClassVar

from ember_code.core.hooks.handlers.base import HookHandler
from ember_code.core.hooks.schemas import (
    HookDefinition,
    HookPayload,
    HookResult,
    HookType,
)


class PromptHookHandler(HookHandler):
    """The cheapest handler in the catalog — a single ``return``.

    ``event`` / ``payload`` are ignored on purpose: signature
    uniformity across handlers is the whole reason the ABC
    exists, and per-handler narrowing was the source of the
    lambda adapters in the pre-refactor dispatch dict.
    """

    handles: ClassVar[HookType] = "prompt"

    async def run(
        self,
        hook: HookDefinition,
        event: str,
        payload: HookPayload,
    ) -> HookResult:
        return HookResult(should_continue=True, message=hook.text)
