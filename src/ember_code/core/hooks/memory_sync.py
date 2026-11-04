"""Tool hook: after each tool call, store what changed in the memory directory.

The cloud's half of :class:`~ember_code.core.utils.memory_files.MemoryMirror`.
Whatever tool the agent used — ``save_file``, ``edit_file``, a shell
command — a memory file it wrote is in the database before its next
step, so a lost VM loses at most the call in flight.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from ember_code.core.hooks.queue.agno_adapter import AgnoCallableAdapter
from ember_code.core.utils.memory_files import MemoryMirror


class MemorySyncHook(AgnoCallableAdapter):
    """Agno tool_hook: run the tool, then sync the memory mirror."""

    def __init__(self, mirror: MemoryMirror) -> None:
        self._mark_as_coroutine()
        self._mirror = mirror

    async def __call__(
        self,
        name: str = "",
        func: Callable[..., Any] | None = None,
        args: dict[str, Any] | None = None,
        **_: Any,
    ) -> Any:
        # Parameter names are Agno's (see AgnoCallableAdapter).
        result = func(**(args or {})) if func is not None else None
        if inspect.isawaitable(result):
            result = await result
        await asyncio.to_thread(self._mirror.sync)
        return result


__all__ = ["MemorySyncHook"]
