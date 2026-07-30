"""Mark a callable-instance as a coroutine function for Agno.

Isolates the private-attr reach-in / ``inspect.markcoroutinefunction``
side effect that used to live inline in
:class:`ToolEventHook.__init__`. Agno's ``aexecute`` chain
detects coroutine functions with ``inspect.iscoroutinefunction``,
which for a plain callable class instance requires marking the
instance explicitly.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any


class AgnoCoroutineMarker:
    """One-liner facade around ``inspect.markcoroutinefunction``
    with a Python 3.11 fallback that pokes the private
    ``asyncio.coroutines._is_coroutine`` sentinel onto the
    instance. The private-attr reach is quarantined to this class
    with a docstring so a future audit doesn't have to rediscover
    the "why" from git blame.
    """

    @staticmethod
    def mark(instance: Any) -> None:
        """Mark ``instance`` so Agno's ``aexecute`` path treats
        its ``__call__`` as a coroutine function.

        On Python 3.12+, ``inspect.markcoroutinefunction`` is
        available and documented. On 3.11 we fall back to setting
        the private ``_is_coroutine`` marker that
        ``asyncio.iscoroutinefunction`` reads — this is the same
        pattern asyncio uses internally, but it IS private, so
        we keep the fallback isolated to one class.
        """
        if hasattr(inspect, "markcoroutinefunction"):
            inspect.markcoroutinefunction(instance)
            return
        instance._is_coroutine = asyncio.coroutines._is_coroutine
