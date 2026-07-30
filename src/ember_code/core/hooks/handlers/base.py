"""Abstract base for hook handlers — one subclass per hook type.

Uniform signature across every handler:

    async def run(hook, event, payload) -> HookResult

so the fan-out in :class:`HookExecutor` is a plain
polymorphic dispatch (registry lookup by ``hook.type`` → call
``.run(...)``) rather than a dict-of-lambdas that has to adapt
per-handler narrower signatures.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from ember_code.core.hooks.schemas import (
    HookDefinition,
    HookPayload,
    HookResult,
    HookType,
)


class HookHandler(ABC):
    """Base class for every hook-type handler.

    Subclasses set the :attr:`handles` ClassVar to the
    :data:`HookType` they cover — the registry uses that to
    self-index instances without a hand-maintained map.
    """

    handles: ClassVar[HookType]

    @abstractmethod
    async def run(
        self,
        hook: HookDefinition,
        event: str,
        payload: HookPayload,
    ) -> HookResult:
        """Execute one hook. Concrete implementations translate
        the hook's side effect (subprocess, HTTP call, MCP
        invoker, static text) into a :class:`HookResult`.
        """

    @staticmethod
    def _non_blocking(message: str = "") -> HookResult:
        """Shared shortcut for the "log and move on" branches.

        Every handler has half a dozen degraded-path returns
        (timeout, unknown server, malformed JSON, ...) and they
        all say the same thing: ``should_continue=True`` with an
        optional advisory message.
        """
        return HookResult(should_continue=True, message=message)
