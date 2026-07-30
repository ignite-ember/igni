"""Abstract base for the four session-startup phases.

The warmup families under this sub-package (knowledge,
codeindex, marketplace, MCP) all share a boot-time
contract:

* they hold a reference to the owning :class:`Session`,
* the background variants must no-op cleanly when no event
  loop is running yet (``asyncio.get_running_loop`` raises),
* transient failures are logged and swallowed so session
  boot doesn't get gated on an offline external dep.

:class:`SessionStartupPhase` factors those three concerns
into a single base class so every subclass focuses on its
own warmup steps instead of duplicating the loop-lookup /
log-and-swallow boilerplate.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ember_code.core.session.core import Session

logger = logging.getLogger(__name__)


class SessionStartupPhase:
    """Shared behaviour for every session-startup phase.

    Constructor stores the owning session reference (some session
    attributes — ``main_team``, ``pool`` — are reassigned mid-
    session, so subclasses read through ``self.session`` at call
    time rather than caching sub-attributes).

    Subclasses inherit two helpers:

    * :meth:`_schedule_on_loop` — encapsulates the ``try:
      get_running_loop() except RuntimeError: return`` +
      ``loop.create_task`` pattern the three background warmups
      share. Callers pass a zero-arg factory (``lambda: _warmup()``)
      so the coroutine is only constructed when a loop exists.
    * :meth:`_log_swallowed` — uniform DEBUG-level "warmup step
      failed, continuing" logging so every catch-and-continue
      inside a warmup body reads the same shape.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        """Return the owning :class:`Session`."""
        return self._session

    def _schedule_on_loop(
        self, coro_factory: Callable[[], Coroutine[object, object, None]]
    ) -> bool:
        """Schedule ``coro_factory()`` on the running loop.

        Returns ``True`` when the task was scheduled, ``False``
        when no loop was running (session's caller retries once
        one exists). The coroutine is only constructed on the
        success branch so we don't leak an un-awaited coroutine
        warning in the no-loop early-return case.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        loop.create_task(coro_factory())
        return True

    def _log_swallowed(self, exc: BaseException, action: str) -> None:
        """Uniform DEBUG log for a swallowed warmup-step failure.

        Session boot must not gate on any single warmup step; every
        failure surfaces through this helper so a log-scraping
        tool sees the same pattern (``"<action> failed (<exc>);
        continuing"``) for every phase.
        """
        logger.debug("%s failed (%s); continuing", action, exc)
