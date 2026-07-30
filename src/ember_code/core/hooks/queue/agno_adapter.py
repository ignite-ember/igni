"""Shared base for callables that plug into Agno's hook chain.

Agno introspects hook callables in three places we need to satisfy:

1. ``__name__`` — used for telemetry / log tags. Class instances don't
   have one by default; we set it on the subclass so instances inherit.
2. Coroutine marking — Agno's ``aexecute_tool_hooks`` awaits hook
   returns; ``inspect.iscoroutinefunction`` must report ``True`` for
   an instance whose ``__call__`` is async. Python 3.12+ exposes
   ``inspect.markcoroutinefunction``; earlier versions need the
   private ``asyncio.coroutines._is_coroutine`` sentinel. We keep the
   dual-path so we don't drop older Python support.
3. Parameter-name pinning — Agno's ``_build_hook_args`` matches by
   exact param name. The adapter documents the pinned names so
   subclass authors don't rename ``func`` to ``next_func`` etc. and
   silently break dispatch. Crucially, the adapter does NOT wrap
   ``__call__`` — subclasses' own signatures remain the reflection
   surface for ``inspect.signature``.
"""

from __future__ import annotations

import asyncio
import inspect


class AgnoCallableAdapter:
    """Base class encapsulating Agno's callable-introspection quirks.

    Agno's ``_build_hook_args`` matches by exact parameter name, so
    subclasses must use names from the recognised set per hook slot:

    * ``tool_hook`` — ``name`` / ``func`` / ``function`` /
      ``function_call`` / ``args`` / ``agent``
    * ``post_hook`` — ``run_output`` / ``agent`` / ``session`` /
      ``user_id`` / ``run_context``

    Anything else is silently dropped by Agno.

    Subclasses:

    * override ``__call__`` with the pinned Agno parameter names for
      their hook slot.
    * inherit an instance-visible ``__name__`` property that mirrors
      ``type(self).__name__`` so Agno's telemetry sees the subclass
      name without each subclass having to declare ``__name__ =
      "…"`` in its class body.
    * inherit a ``_mark_as_coroutine()`` helper they can call from
      ``__init__`` when their ``__call__`` is ``async``.
    """

    @property
    def __name__(self) -> str:
        """Expose ``type(self).__name__`` to instance-level lookups.

        Python's default ``type.__name__`` slot isn't visible to
        ``instance.__name__`` reads (it's a data descriptor on the
        metaclass), so Agno's ``callable.__name__`` telemetry read
        would ``AttributeError``. Publishing it as a property here
        gives every subclass instance a stable, class-derived name
        without requiring each subclass to declare it manually.
        """
        return type(self).__name__

    def _mark_as_coroutine(self) -> None:
        """Mark this instance so ``inspect.iscoroutinefunction`` returns
        True. Uses the public API on Python 3.12+ and falls back to the
        private sentinel on earlier versions."""
        if hasattr(inspect, "markcoroutinefunction"):
            inspect.markcoroutinefunction(self)
        else:  # pragma: no cover — legacy Python fallback
            self._is_coroutine = asyncio.coroutines._is_coroutine


__all__ = ["AgnoCallableAdapter"]
