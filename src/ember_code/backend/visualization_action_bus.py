"""Visualization action bus — extracted from :class:`BackendServer`.

The previous ``BackendServer.dispatch_visualization_action`` was
a 30-LoC inline method that:

1. Reached into ``session._visualization_actions`` (a private attr
   which the session doesn't declare or manage — a smoking-gun
   Rule 5 offender).
2. Manually maintained a 32-entry ring buffer on the session.
3. Fell back to a ``getattr(session, "broadcast", None)`` reach-in
   for the broadcast dispatch.

Extracting the logic into a dedicated class:

* Owns the ring buffer as an instance attribute — no more session
  private-attr mutation.
* Reads :meth:`Session.broadcast` (the public method that already
  exists on Session) rather than probing for it.

Wire type moves to :mod:`schemas_visualization`.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

from ember_code.backend.schemas_visualization import VisualizationActionResult

if TYPE_CHECKING:
    from ember_code.core.session import Session

logger = logging.getLogger(__name__)


class VisualizationActionBus:
    """Ring buffer + broadcast fan-out for FE-side json-render
    component interactions.

    32-entry ring so a chatty UI (e.g. a Slider firing on every
    drag tick) doesn't grow forever. Generous for the "one-off
    action after the user reviews a card" use case.
    """

    def __init__(self, session: Session, max_entries: int = 32) -> None:
        self._session = session
        self._max_entries = max_entries
        self._actions: list[dict] = []

    @property
    def actions(self) -> list[dict]:
        """The current ring (newest last). Exposed read-only for
        tools that want to introspect the recent interaction
        history."""
        return list(self._actions)

    def dispatch(self, action: str, params: dict | None = None) -> VisualizationActionResult:
        """Record + broadcast one user-driven component action.

        The FE forwards the action name + params here. Two side
        effects:

        1. Stash the event on the ring so a future agent tool can
           query it.
        2. Broadcast a ``visualization_action_dispatched`` push so
           anything else (log panels, dev tools) can observe.
        """
        p = dict(params or {})
        self._actions.append({"action": action, "params": p})
        if len(self._actions) > self._max_entries:
            del self._actions[: len(self._actions) - self._max_entries]
        broadcast = getattr(self._session, "broadcast", None)
        if broadcast is not None:
            with contextlib.suppress(Exception):
                broadcast(
                    "visualization_action_dispatched",
                    {"action": action, "params": p},
                )
        return VisualizationActionResult(ok=True, action=action, params=p)
