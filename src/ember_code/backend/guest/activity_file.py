"""``/run/igni/activity.json``: igni's ``app.activity``, for the guest agent.

``docs/CLOUD.md`` §9. The node agent suspends an idle VM unless igni
says ``suspendable: false``; it asks the guest agent, whose ``status``
reads this file. The host channel that carries ``app.activity`` admits
only extension processes, so the file is how it leaves igni.

Written whole (:class:`AtomicFile`) when anything in it changes — told
by the host model's ``app.activity_changed`` and checked every second
for what moves without a signal, such as a window connecting — and at
least every :attr:`ActivityFile.HEARTBEAT_SECONDS` while a run is going:
the guest agent treats a file older than ten seconds that still says
``running`` as stale (``suspendable: null``), so an igni that died
mid-run does not keep its VM awake forever. Removed when igni stops.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, Field

from ember_code.backend.activity import Activity, ActivityState, SessionActivity
from ember_code.backend.guest.files import AtomicFile

logger = logging.getLogger(__name__)


class GuestActivity(BaseModel):
    """What the file holds: ``app.activity``, and what else idleness needs."""

    state: ActivityState
    #: Whether the VM may be suspended now: nothing is running.
    suspendable: bool
    waiting_approvals: int = 0
    #: Sessions with a run working (not waiting on a person).
    running: int = 0
    #: Windows and other clients connected to igni.
    clients: int = 0
    sessions: list[SessionActivity] = Field(default_factory=list)
    #: When igni wrote it, in Unix milliseconds.
    written_at_ms: int = 0

    @classmethod
    def of(cls, activity: Activity, *, clients: int, now: float) -> GuestActivity:
        return cls(
            state=activity.state,
            suspendable=activity.suspendable,
            waiting_approvals=activity.waiting_approvals,
            running=sum(1 for s in activity.sessions if s.state is ActivityState.RUNNING),
            clients=clients,
            sessions=activity.sessions,
            written_at_ms=int(now * 1000),
        )

    def same_as(self, other: GuestActivity | None) -> bool:
        """Everything but the time it was written."""
        if other is None:
            return False
        exclude = {"written_at_ms"}
        return self.model_dump(exclude=exclude) == other.model_dump(exclude=exclude)


class ActivityFile:
    """Keeps the activity file true while igni runs."""

    #: The guest agent's staleness limit is ten seconds; the protocol asks for five at most.
    HEARTBEAT_SECONDS = 4.0
    #: How often what moves without a signal (a client connecting) is looked at.
    CHECK_SECONDS = 1.0

    def __init__(
        self,
        path: Path,
        *,
        activity: Callable[[], Activity],
        clients: Callable[[], int],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._file = AtomicFile(path)
        self._activity = activity
        self._clients = clients
        self._clock = clock
        self._written: GuestActivity | None = None
        self._written_at = 0.0
        self._failing = False
        self._task: asyncio.Task[None] | None = None

    @property
    def path(self) -> Path:
        return self._file.path

    @property
    def written(self) -> GuestActivity | None:
        return self._written

    def refresh(self, *, force: bool = False) -> bool:
        """Write the file if it changed, is due a heartbeat, or ``force``. True when written."""
        try:
            current = GuestActivity.of(self._activity(), clients=self._clients(), now=self._clock())
        except Exception as exc:
            logger.debug("activity file: activity unavailable: %s", exc)
            return False
        now = self._clock()
        heartbeat_due = (
            current.state is ActivityState.RUNNING
            and now - self._written_at >= self.HEARTBEAT_SECONDS
        )
        if not force and current.same_as(self._written) and not heartbeat_due:
            return False
        try:
            self._file.write(current.model_dump_json().encode())
        except OSError as exc:
            if not self._failing:
                logger.warning("cannot write %s: %s", self.path, exc)
            self._failing = True
            return False
        self._failing = False
        self._written = current
        self._written_at = now
        return True

    def changed(self, *_: object) -> None:
        """A listener for ``app.activity_changed``."""
        self.refresh()

    def start(self) -> None:
        if self._task is not None:
            return
        self.refresh(force=True)
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # Nobody keeps it true any more: no file is "unknown", which
        # leaves the VM to the node agent's idle timer.
        with contextlib.suppress(OSError):
            self._file.remove()
        self._written = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.CHECK_SECONDS)
            self.refresh()


__all__ = ["ActivityFile", "GuestActivity"]
