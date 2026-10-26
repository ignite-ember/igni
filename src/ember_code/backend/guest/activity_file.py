"""``/run/igni/activity.json``: igni's ``app.activity``, for the guest agent.

``docs/CLOUD.md`` §9. The node agent suspends an idle VM unless igni
says ``suspendable: false``; it asks the guest agent, whose ``status``
reads this file. The host channel that carries ``app.activity`` admits
only extension processes, so the file is how it leaves igni.

Written whole (:class:`AtomicFile`) when anything in it changes — told
by the host model's ``app.activity_changed`` (which busy leases and wakes
move too) and by windows connecting and leaving — and at
least every :attr:`ActivityFile.HEARTBEAT_SECONDS` while it is not
suspendable (a run going, or anything busy):
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
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from ember_code.backend.activity import Activity, ActivityState, SessionActivity
from ember_code.backend.guest.files import AtomicFile
from ember_code.core.activity import BusyLease, background

logger = logging.getLogger(__name__)


class UnixMs:
    """Times in the file are Unix milliseconds: what the node agent compares with its clock."""

    @staticmethod
    def of(moment: datetime | None) -> int | None:
        return None if moment is None else int(moment.timestamp() * 1000)


class GuestBusy(BaseModel):
    """One busy lease, as the node agent reads it."""

    kind: str
    holder: str
    reason: str = ""
    #: Unix milliseconds.
    since: int
    #: Unix milliseconds; ``None`` until let go.
    expires_at: int | None = None

    @classmethod
    def of(cls, lease: BusyLease) -> GuestBusy:
        since = UnixMs.of(lease.since)
        assert since is not None
        return cls(
            kind=lease.kind.value,
            holder=lease.holder,
            reason=lease.reason,
            since=since,
            expires_at=UnixMs.of(lease.expires_at),
        )


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
    #: What holds igni busy: ``{kind, holder, reason, since, expires_at}`` each.
    busy: list[GuestBusy] = Field(default_factory=list)
    #: When to wake igni if it is suspended, Unix milliseconds: the earliest thing due.
    wake_at: int | None = None
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
            busy=[GuestBusy.of(lease) for lease in activity.busy],
            wake_at=UnixMs.of(activity.wake_at),
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
        self._started = False
        self._heartbeat: asyncio.Future[None] | None = None

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
        heartbeat_due = not current.suspendable and now - self._written_at >= self.HEARTBEAT_SECONDS
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
        self._keep_beating(not current.suspendable)
        return True

    def changed(self, *_: object) -> None:
        """A listener for ``app.activity_changed``."""
        self.refresh()

    def start(self) -> None:
        """Write it now; from then on it is written when told something moved.

        No timer while igni is suspendable: an idle igni wakes for
        nothing, since every wake-up dirties memory the next suspend
        must write. While it is not suspendable a heartbeat rewrites it
        every :attr:`HEARTBEAT_SECONDS`, and that also lets a busy lease
        that expired stop counting.
        """
        self._started = True
        self.refresh(force=True)

    async def stop(self) -> None:
        self._started = False
        beat, self._heartbeat = self._heartbeat, None
        if beat is not None:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
        # Nobody keeps it true any more: no file is "unknown", which
        # leaves the VM to the node agent's idle timer.
        with contextlib.suppress(OSError):
            self._file.remove()
        self._written = None

    def _keep_beating(self, needed: bool) -> None:
        if not needed or not self._started:
            return
        if self._heartbeat is not None and not self._heartbeat.done():
            return
        with contextlib.suppress(RuntimeError):
            # Not busy: it reports busyness, it is not any.
            self._heartbeat = background.daemon(self._beat(), name="activity-heartbeat")

    async def _beat(self) -> None:
        while self._written is not None and not self._written.suspendable:
            await asyncio.sleep(self.HEARTBEAT_SECONDS)
            self.refresh()


__all__ = ["ActivityFile", "GuestActivity"]
