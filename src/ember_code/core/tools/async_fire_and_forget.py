"""Fire-and-forget async scheduling from possibly-sync contexts.

Home of :class:`AsyncFireAndForget` — one class whose ``.schedule``
method safely spawns a coroutine when a loop is available and
silently no-ops when it isn't. Collapses the duplicated
``get_running_loop`` / RuntimeError check the persistence hooks
in ``process_supervisor.py`` used to carry twice (once each for
``persist_add`` and ``persist_remove``) into one named class with
the "why this is a workaround" comment attached once.

Standalone rather than a nested helper because "schedule a
coroutine from a possibly-sync caller" is a general pattern; any
subsystem that hits it can compose an instance rather than
copy-pasting the ``try/except RuntimeError`` dance.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)


class AsyncFireAndForget:
    """Schedule coroutines onto the running loop; silently drop
    them when there's no loop.

    ``get_running_loop`` raises ``RuntimeError`` when nothing is
    servicing the loop. ``get_event_loop`` is deprecated and,
    crucially on Python 3.11/3.12, silently CREATES a new loop
    when none is running — ``ensure_future`` would then schedule
    onto that phantom loop and the coroutine would never run,
    leaking DB locks and hanging pytest sessions for the full
    job timeout. So we probe with ``get_running_loop`` and drop
    the coroutine on RuntimeError rather than paper over it.

    A queued task that hasn't run yet when the process exits
    leaks the write in memory but never hits disk; the next
    startup will simply not see whatever change was pending.
    That's fine — we couldn't track it either way.
    """

    def schedule(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any] | None:
        """Schedule ``coro`` onto the running loop if one exists.

        Returns the created :class:`asyncio.Task` on success, or
        ``None`` when no loop was running (in which case the
        coroutine is closed to avoid a "was never awaited"
        warning). Callers treat the return value as opaque — this
        is fire-and-forget.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return None
        return asyncio.ensure_future(coro)
