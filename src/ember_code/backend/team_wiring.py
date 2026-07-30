"""Team-wiring collaborator — extracted from :class:`BackendServer`.

The previous ``BackendServer.wire_queue_hook`` and
``wire_orchestrate_progress`` methods each reached into
``session.main_team`` to mutate its ``tool_hooks`` / ``post_hooks``
lists or the ``OrchestrateTools`` callbacks. Both are one-shot
wiring concerns that fire at pool-runtime construction — not part
of the run lifecycle, not part of the session core, but also not
something ``BackendServer`` should own the details of.

Extracting to :class:`TeamWiring` gives us:

* One place to grep when the team's hook attributes change shape.
* A tests-friendly seam (``TeamWiring(session).wire_queue_hook(q)``)
  that doesn't need a full ``BackendServer`` fixture.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ember_code.core.hooks.queue import QueueBridge
from ember_code.core.tools.orchestrate import OrchestrateTools
from ember_code.core.tools.orchestrate_events import EventAppender, OnProgress

if TYPE_CHECKING:
    from ember_code.core.session import Session


class TeamWiring:
    """One-shot wiring of queue hooks + orchestrate-progress
    callbacks onto :attr:`Session.main_team`.

    The class is stateless — every method reads the session's live
    ``main_team`` at call time so a caller who rebuilt the team
    (e.g. after ``switch_model``) still wires onto the fresh
    instance.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def wire_queue_hook(self, queue: list) -> None:
        """Wire queue hooks onto the team.

        - Tool-hook (injector) drains the queue after each tool call
          so the model sees queued text on its next iteration.
        - Post-hook (persister) records those drained items as
          proper user-role history entries before the session is
          saved.
        """
        bridge = QueueBridge(queue=queue)
        bridge.register_on(self._session.main_team)

    def wire_orchestrate_progress(self, callback: OnProgress) -> None:
        """Set a progress callback on the orchestrate tool."""
        for tool in self._orchestrate_tools():
            tool.on_progress = callback

    def wire_orchestrate_event_appender(self, appender: EventAppender) -> None:
        """Set the session event-log appender on the orchestrate
        tool so the visualizer final-delta lands in the persisted
        event log — the ``get_chat_history`` splicer uses that
        entry to place the viz card next to its originating
        ``spawn_agent`` tool turn on reload."""
        for tool in self._orchestrate_tools():
            tool.event_appender = appender

    def _orchestrate_tools(self):
        """Iterate every :class:`OrchestrateTools` instance attached
        to the team (there's usually one, but be defensive for
        tests / plugins that inject more)."""
        for tool in self._session.main_team.tools or []:
            if isinstance(tool, OrchestrateTools):
                yield tool
