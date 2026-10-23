"""The other end of event delivery: a service, not a frame.

Delivery used to finish at an iframe. Everything about that is
right for an extension with a page — and an extension with no page
heard nothing at all, while one with a closed page heard nothing
until the user opened it. CodeIndex is the example that makes it
concrete: its panel can redraw on ``workspace.head-changed``, but
the part that would actually stop polling is its *service*, and no
event could reach it.

MCP runs host→server. A service cannot be pushed to, so the only
way to tell it something is to call it — which is what this does,
on the reserved
:data:`~ember_code.extensions.events.EVENT_SINK_TOOL`.

## A service must not be able to hold the host up

An event producer runs on a timer. A service that takes thirty
seconds to acknowledge one, or never answers at all, must not slow
the watcher, stall the event loop, or accumulate work without
limit. So:

* **one call in flight per extension**, because a slow service
  turned concurrent by a fast producer is the same bug with more
  sockets;
* **a bounded queue**, oldest dropped, because unbounded is just a
  leak with a politer name;
* **a timeout**, because "never answers" is a real thing services
  do.

Dropping is visible — counted, logged, and reported by
:meth:`dropped` — rather than silent. An extension that is missing
events should be able to find out that it is.

## Ordinary silence is not failure

Most extensions will not implement the sink, and that is the
expected case, not a fault: ``notify`` answers ``False`` and
nothing is logged. Nor does a failed delivery mark the extension
unhealthy — see :meth:`ExtensionRpc.notify` for why a timer-driven
event must not drive the health probe.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque

from ember_code.extensions.rpc import ExtensionRpc

logger = logging.getLogger(__name__)


class ServiceEvents:
    """Relay host events into extension services, serially."""

    #: Events held per extension while one is in flight. Deep enough
    #: that an ordinary burst — a commit moves HEAD and changes the
    #: dirty set in the same second — survives a service that takes
    #: a moment, shallow enough that a service which never answers
    #: costs a bounded amount of memory.
    DEPTH = 16

    #: How long one ``igni_on_event`` may take. A sink should
    #: acknowledge and do its work afterwards; a service that treats
    #: being told as an invitation to reindex is one igni waits on.
    TIMEOUT_SECONDS = 5.0

    def __init__(self, *, rpc: ExtensionRpc) -> None:
        self._rpc = rpc
        self._pending: dict[str, deque[tuple[str, dict[str, object]]]] = {}
        self._draining: dict[str, asyncio.Task] = {}
        self._dropped: dict[str, int] = {}
        #: Set by :meth:`stop`, and checked before anything is
        #: scheduled. A cancelled drain runs its ``finally``, and
        #: that ``finally`` reschedules when work is left — so
        #: without this, stopping a relay with a queue behind it
        #: starts a task that outlives the thing that stopped it.
        self._stopped = False

    def tell(self, extension_id: str, topic: str, payload: dict[str, object]) -> None:
        """Queue one event for one service. Never blocks, never raises.

        Called from the router's delivery callback, which is on the
        event loop in the middle of a producer — so the work is
        handed to a task and this returns.
        """
        if self._stopped:
            return
        queue = self._pending.setdefault(extension_id, deque())
        if len(queue) >= self.DEPTH:
            queue.popleft()
            self._dropped[extension_id] = self._dropped.get(extension_id, 0) + 1
            logger.warning(
                "%s is not keeping up with its events; dropped the oldest (%d so far). "
                "Its igni_on_event is slower than the events arrive.",
                extension_id,
                self._dropped[extension_id],
            )
        queue.append((topic, payload))
        self._ensure_draining(extension_id)

    def dropped(self, extension_id: str) -> int:
        """How many events this extension was never told about."""
        return self._dropped.get(extension_id, 0)

    def pending(self, extension_id: str) -> int:
        """How many are waiting. For tests and for an operator."""
        return len(self._pending.get(extension_id, ()))

    async def stop(self) -> None:
        """Cancel every drain and wait for it to go.

        In-flight calls are cancelled rather than awaited: shutdown
        should not be held open by a service that is not answering,
        which is exactly the service most likely to be mid-call.
        """
        self._stopped = True
        tasks = list(self._draining.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._draining.clear()
        self._pending.clear()

    def _ensure_draining(self, extension_id: str) -> None:
        if self._stopped:
            return
        existing = self._draining.get(extension_id)
        if existing is not None and not existing.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No running loop. ``tell`` is called from a registry
            # watcher, and a watcher can fire from a synchronous
            # install — in a test, or in any future caller that
            # installs before the backend is serving. This method
            # promises not to raise, so the event is dropped with a
            # line rather than thrown at whoever changed a state.
            #
            # Asked before building the coroutine, not after: a
            # failed ``create_task`` leaves one un-awaited, which
            # is a warning at a random later garbage collection.
            self._pending.pop(extension_id, None)
            logger.debug("no running loop; dropped queued events for %s", extension_id)
            return
        self._draining[extension_id] = asyncio.create_task(self._drain(extension_id))

    async def _drain(self, extension_id: str) -> None:
        """Send what is queued, one at a time, until it is empty."""
        queue = self._pending.setdefault(extension_id, deque())
        try:
            while queue:
                topic, payload = queue.popleft()
                try:
                    await asyncio.wait_for(
                        self._rpc.notify(extension_id, topic, payload),
                        timeout=self.TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    logger.warning(
                        "%s took longer than %.0fs to acknowledge %s. A sink should "
                        "acknowledge and work afterwards.",
                        extension_id,
                        self.TIMEOUT_SECONDS,
                        topic,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # One event that could not be delivered is not a
                    # reason the next one is not tried.
                    logger.exception("telling %s about %s failed", extension_id, topic)
        finally:
            self._draining.pop(extension_id, None)
            # Something may have arrived while the last call was in
            # flight and after the loop checked, so the queue can be
            # non-empty here with nothing scheduled to drain it.
            if queue:
                self._ensure_draining(extension_id)


__all__ = ["ServiceEvents"]
