"""An approval appeared or went away — told to whoever listens.

The backend's :class:`~ember_code.core.approvals.journal.ApprovalJournal`
is where every approval waiting on a person is written, whoever raised
it (the main agent, a sub-agent) and however it ends (answered in a
window, by an app through the host object model, read back after a
restart and answered then, or dropped because its run ended). So that is
where the change is announced: :data:`approval_events` hands each
:class:`ApprovalChange` to its listeners — the host object model turns
it into ``chat.approvals.changed`` for an app subscribed through the API.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from enum import StrEnum

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ApprovalState(StrEnum):
    WAITING = "waiting"
    RESOLVED = "resolved"


class ApprovalChange(BaseModel):
    session_id: str
    requirement_id: str
    run_id: str
    state: ApprovalState
    #: ``main`` or ``sub_agent``.
    origin: str
    tool_name: str = ""


class ApprovalEvents:
    """Listeners for :class:`ApprovalChange`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._listeners: list[Callable[[ApprovalChange], None]] = []

    def watch(self, listener: Callable[[ApprovalChange], None]) -> Callable[[], None]:
        """Listen; returns the call that stops listening."""
        with self._lock:
            self._listeners.append(listener)

        def stop() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return stop

    def clear(self) -> None:
        """Forget every listener (tests start from none)."""
        with self._lock:
            self._listeners.clear()

    def announce(self, change: ApprovalChange) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(change)
            except Exception as exc:
                logger.debug("approval listener failed: %s", exc)


#: The process's approval events.
approval_events = ApprovalEvents()


__all__ = ["ApprovalChange", "ApprovalEvents", "ApprovalState", "approval_events"]
