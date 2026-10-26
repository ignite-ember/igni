"""What igni is busy with, and when it next has something to do.

``docs/CLOUD.md`` §9. Whether igni's process may be suspended is decided
in one place, :class:`ActivityRegistry`. Anything working holds a
**busy lease** while it works — a model or HTTP call, an MCP or
extension call, a scheduled task, a background process, post-run work,
an extension that said it is busy (``ext.busy``) — and lets it go when
done. A run waiting on a person holds none: nothing happens until the
decision.

A lease may carry an expiry, after which it no longer counts whether or
not it was let go, so a holder that is stuck — an extension that forgot,
a call that hangs — cannot keep the process awake for ever.

Work that is due later while nothing runs — the scheduler's next task,
a retry — is published as a **wake**: the earliest one is when whoever
suspends igni should wake it.

The registry is the core's and exists everywhere; on the desktop nothing
reads it to suspend anything. Leases are taken with
:meth:`ActivityRegistry.hold` (a context manager, sync or async) and
background work through :class:`~ember_code.core.activity.background.BackgroundTasks`,
which holds one for as long as the task runs.
"""

from __future__ import annotations

import itertools
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import TracebackType
from typing import ClassVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class BusyKind(StrEnum):
    #: Work a run left for after it ended: memory and learning extraction, compaction, titles.
    POST_RUN = "post_run"
    MODEL_CALL = "model_call"
    HTTP_CALL = "http_call"
    MCP_CALL = "mcp_call"
    EXTENSION_CALL = "extension_call"
    SCHEDULED_TASK = "scheduled_task"
    #: A shell process igni started in the background, still running.
    PROCESS = "process"
    MONITOR = "monitor"
    #: An extension that said it is working (``ext.busy``).
    EXTENSION = "extension"
    #: Any other task igni started and has not finished.
    BACKGROUND = "background"


class BusyLease(BaseModel):
    """One thing igni is busy with."""

    id: str
    kind: BusyKind
    #: Who holds it: an extension id, a server name, a task's name.
    holder: str
    reason: str = ""
    since: datetime
    #: After this it no longer counts; ``None`` until let go.
    expires_at: datetime | None = None

    def expired(self, now: datetime) -> bool:
        return self.expires_at is not None and self.expires_at <= now


class Wake(BaseModel):
    """Work due at a time while nothing else may be running."""

    holder: str
    at: datetime
    reason: str = ""


class Lease:
    """A held lease: let it go with :meth:`release` or by leaving its ``with``."""

    def __init__(self, registry: ActivityRegistry, lease_id: str) -> None:
        self._registry = registry
        self.id = lease_id

    def release(self) -> None:
        self._registry.release(self.id)

    def renew(self, ttl: float | None) -> BusyLease | None:
        """Push its expiry ``ttl`` seconds from now (``None``: none)."""
        return self._registry.renew(self.id, ttl)

    def __enter__(self) -> Lease:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    async def __aenter__(self) -> Lease:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


class ActivityRegistry:
    """The process's busy leases and wakes."""

    _shared: ClassVar[ActivityRegistry | None] = None

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._lock = threading.Lock()
        self._leases: dict[str, BusyLease] = {}
        self._wakes: dict[str, Wake] = {}
        self._ids = itertools.count(1)
        self._watchers: list[Callable[[], None]] = []
        self._clock = clock or (lambda: datetime.now(UTC))

    @classmethod
    def shared(cls) -> ActivityRegistry:
        if cls._shared is None:
            cls._shared = cls()
        return cls._shared

    @classmethod
    def reset_shared(cls) -> None:
        """A fresh registry; for tests."""
        cls._shared = None

    # ── Leases ────────────────────────────────────────────────────

    def hold(
        self,
        kind: BusyKind,
        holder: str,
        reason: str = "",
        *,
        ttl: float | None = None,
    ) -> Lease:
        now = self._clock()
        lease = BusyLease(
            id=f"busy-{next(self._ids)}",
            kind=kind,
            holder=holder,
            reason=reason,
            since=now,
            expires_at=now + timedelta(seconds=ttl) if ttl is not None else None,
        )
        with self._lock:
            self._leases[lease.id] = lease
        self._changed()
        return Lease(self, lease.id)

    def release(self, lease_id: str) -> None:
        with self._lock:
            gone = self._leases.pop(lease_id, None)
        if gone is not None:
            self._changed()

    def renew(self, lease_id: str, ttl: float | None) -> BusyLease | None:
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                return None
            expires = self._clock() + timedelta(seconds=ttl) if ttl is not None else None
            lease = lease.model_copy(update={"expires_at": expires})
            self._leases[lease_id] = lease
        self._changed()
        return lease

    def lease(self, lease_id: str) -> BusyLease | None:
        with self._lock:
            return self._leases.get(lease_id)

    def busy(self) -> list[BusyLease]:
        """What counts now, oldest first; expired leases are dropped."""
        now = self._clock()
        with self._lock:
            expired = [i for i, lease in self._leases.items() if lease.expired(now)]
            for lease_id in expired:
                del self._leases[lease_id]
            current = sorted(self._leases.values(), key=lambda lease: lease.since)
        if expired:
            logger.info("busy leases expired without being let go: %s", expired)
        return current

    def is_busy(self) -> bool:
        return bool(self.busy())

    # ── Wakes ─────────────────────────────────────────────────────

    def wake(self, holder: str, at: datetime | None, reason: str = "") -> None:
        """Say when ``holder`` next has work; ``None`` withdraws it."""
        with self._lock:
            if at is None:
                changed = self._wakes.pop(holder, None) is not None
            else:
                wake = Wake(holder=holder, at=at, reason=reason)
                changed = self._wakes.get(holder) != wake
                self._wakes[holder] = wake
        if changed:
            self._changed()

    def wakes(self) -> list[Wake]:
        with self._lock:
            return sorted(self._wakes.values(), key=lambda wake: wake.at)

    def wake_at(self) -> datetime | None:
        """The earliest wake, or ``None``."""
        wakes = self.wakes()
        return wakes[0].at if wakes else None

    # ── Watching ──────────────────────────────────────────────────

    def watch(self, watcher: Callable[[], None]) -> None:
        """Be called whenever a lease or a wake comes or goes."""
        self._watchers.append(watcher)

    def unwatch(self, watcher: Callable[[], None]) -> None:
        if watcher in self._watchers:
            self._watchers.remove(watcher)

    def _changed(self) -> None:
        for watcher in list(self._watchers):
            try:
                watcher()
            except Exception:
                logger.exception("an activity watcher failed")


__all__ = ["ActivityRegistry", "BusyKind", "BusyLease", "Lease", "Wake"]
