"""Best-effort mid-run session persistence.

Extracted from :mod:`ember_code.backend.server_pause` — the pause
module previously held both the HITL multiplexer AND the periodic
session checkpoint concerns even though they share zero code paths.
Splitting them removes the smoking-gun ``del backend`` line the
audit called out (the checkpoint free function never actually used
its ``backend`` argument) and gives each concern its own file.

The class carries no HITL state — it only needs a live Agno
``team`` reference, so its constructor takes exactly that. The
:meth:`snapshot` method forces one save; :meth:`run_forever` is the
background loop the ``RunController`` spawns for the duration of a
run.

Why we care about mid-run saves:

Agno's streaming runs don't write to disk between ``RunStarted`` and
``RunCompleted``. For a pure text-only response (no tools, so no
``ToolCompleted`` event to hook), the in-flight ``RunOutput`` would
never reach SQLite — a crash mid-stream would lose the user's
prompt AND the partial response. The pre-persistence in
``_run_message_locked`` saves the prompt unconditionally; this loop
takes care of the partial response by forcing ``asave_session`` on
a cadence.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


class SessionCheckpointer:
    """Force Agno to persist a live team's session to SQLite."""

    def __init__(self, team: Any) -> None:
        """Store the team reference used by both entry points.

        ``team`` is the live Agno ``Team`` — its ``cached_session``
        and ``asave_session`` are read on every :meth:`snapshot`
        call. Kept as ``Any`` because Agno's ``Team`` is a private
        symbol; we only touch two well-known attributes.
        """
        self._team = team

    async def snapshot(self) -> None:
        """Persist the cached session blob if one exists.

        Best-effort: a transient persistence failure must not abort
        the live stream. If the session blob is unavailable (Agno
        hasn't created ``cached_session`` yet on a very early
        event) we log and move on.
        """
        try:
            session = getattr(self._team, "cached_session", None)
            if session is None:
                return
            await self._team.asave_session(session)
        except Exception as exc:
            logger.debug("incremental session checkpoint failed: %s", exc)

    async def run_forever(
        self,
        interval: float = 3.0,
        checkpoint_hook: Callable[[Any], Awaitable[None]] | None = None,
    ) -> None:
        """Loop that snapshots the session every ``interval`` seconds.

        When ``checkpoint_hook`` is provided, the loop invokes the
        hook with ``self._team`` on each tick instead of
        :meth:`snapshot`. This is the seam
        :class:`~ember_code.backend.server.BackendServer` uses to
        route through its own ``_checkpoint_session`` bound method
        — tests reassign that method to a spy and must see it
        invoked on each tick (see
        ``tests/test_crash_survival.py``).

        Cancellation is the normal stop signal — the caller cancels
        this task in its ``finally``. We swallow ``CancelledError``
        cleanly; any other exception is logged but never
        propagated.
        """
        try:
            while True:
                await asyncio.sleep(interval)
                if checkpoint_hook is not None:
                    await checkpoint_hook(self._team)
                else:
                    await self.snapshot()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.debug("periodic checkpoint task crashed: %s", exc)
