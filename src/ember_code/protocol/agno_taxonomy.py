"""Pure-data taxonomy of Agno event type tuples.

Every group of Agno event classes the rest of the codebase does
``isinstance`` checks against lives here. No behavior, no side
effects, no imports beyond Agno's own event modules — importing
this module is safe from any layer that already tolerates Agno
imports (the FE has its own protocol messages and never touches
this).

Two surfaces:

* :class:`AgnoEventTaxonomy` — named class attributes for callers
  that want a single namespace (``AgnoEventTaxonomy.CONTENT``).
* Module-level tuple constants (``CONTENT_EVENTS``,
  ``TOOL_STARTED_EVENTS``, …) — the hot-path serializer and
  stream mux do many ``isinstance`` checks against these and a
  plain tuple lookup is cheaper than an attribute access on a
  class. The names match the pre-refactor exports so migrating
  callers is a one-line import path swap.
"""

from __future__ import annotations

from agno.run import agent as agent_events
from agno.run import team as team_events


class AgnoEventTaxonomy:
    """Namespace holding every Agno event-tuple grouping.

    Kept as class attributes (not instance) so nothing has to be
    constructed to read them — they're pure metadata. The parallel
    module-level constants below re-export the same tuples for the
    hot path (``isinstance(evt, CONTENT_EVENTS)`` avoids an extra
    attribute-lookup vs. ``isinstance(evt, taxonomy.CONTENT)``).
    """

    CONTENT = (agent_events.RunContentEvent, team_events.RunContentEvent)
    TOOL_STARTED = (
        agent_events.ToolCallStartedEvent,
        team_events.ToolCallStartedEvent,
    )
    TOOL_COMPLETED = (
        agent_events.ToolCallCompletedEvent,
        team_events.ToolCallCompletedEvent,
    )
    TOOL_ERROR = (
        agent_events.ToolCallErrorEvent,
        team_events.ToolCallErrorEvent,
    )
    MODEL_COMPLETED = (
        agent_events.ModelRequestCompletedEvent,
        team_events.ModelRequestCompletedEvent,
    )
    RUN_CONTENT_COMPLETED = (
        agent_events.RunContentCompletedEvent,
        team_events.RunContentCompletedEvent,
    )
    RUN_COMPLETED = (
        agent_events.RunCompletedEvent,
        team_events.RunCompletedEvent,
        agent_events.RunOutput,
        team_events.RunOutput,
    )
    RUN_STARTED = (agent_events.RunStartedEvent, team_events.RunStartedEvent)
    RUN_ERROR = (agent_events.RunErrorEvent, team_events.RunErrorEvent)
    REASONING = (
        agent_events.ReasoningStartedEvent,
        team_events.ReasoningStartedEvent,
    )
    REASONING_CONTENT = (
        agent_events.ReasoningContentDeltaEvent,
        team_events.ReasoningContentDeltaEvent,
    )
    TASK_CREATED = (team_events.TaskCreatedEvent,)
    TASK_UPDATED = (team_events.TaskUpdatedEvent,)
    TASK_ITERATION = (team_events.TaskIterationStartedEvent,)
    TASK_STATE_UPDATED = (team_events.TaskStateUpdatedEvent,)
    RUN_PAUSED = (agent_events.RunPausedEvent, team_events.RunPausedEvent)


# ── Hot-path re-exports ───────────────────────────────────────────
# Callers do heavy ``isinstance`` traffic against these tuples;
# module-level bindings avoid a per-check attribute lookup.

CONTENT_EVENTS = AgnoEventTaxonomy.CONTENT
TOOL_STARTED_EVENTS = AgnoEventTaxonomy.TOOL_STARTED
TOOL_COMPLETED_EVENTS = AgnoEventTaxonomy.TOOL_COMPLETED
TOOL_ERROR_EVENTS = AgnoEventTaxonomy.TOOL_ERROR
MODEL_COMPLETED_EVENTS = AgnoEventTaxonomy.MODEL_COMPLETED
RUN_CONTENT_COMPLETED_EVENTS = AgnoEventTaxonomy.RUN_CONTENT_COMPLETED
RUN_COMPLETED_EVENTS = AgnoEventTaxonomy.RUN_COMPLETED
RUN_STARTED_EVENTS = AgnoEventTaxonomy.RUN_STARTED
RUN_ERROR_EVENTS = AgnoEventTaxonomy.RUN_ERROR
REASONING_EVENTS = AgnoEventTaxonomy.REASONING
REASONING_CONTENT_EVENTS = AgnoEventTaxonomy.REASONING_CONTENT
TASK_CREATED_EVENTS = AgnoEventTaxonomy.TASK_CREATED
TASK_UPDATED_EVENTS = AgnoEventTaxonomy.TASK_UPDATED
TASK_ITERATION_EVENTS = AgnoEventTaxonomy.TASK_ITERATION
TASK_STATE_UPDATED_EVENTS = AgnoEventTaxonomy.TASK_STATE_UPDATED
RUN_PAUSED_EVENTS = AgnoEventTaxonomy.RUN_PAUSED


__all__ = [
    "AgnoEventTaxonomy",
    "CONTENT_EVENTS",
    "TOOL_STARTED_EVENTS",
    "TOOL_COMPLETED_EVENTS",
    "TOOL_ERROR_EVENTS",
    "MODEL_COMPLETED_EVENTS",
    "RUN_CONTENT_COMPLETED_EVENTS",
    "RUN_COMPLETED_EVENTS",
    "RUN_STARTED_EVENTS",
    "RUN_ERROR_EVENTS",
    "REASONING_EVENTS",
    "REASONING_CONTENT_EVENTS",
    "TASK_CREATED_EVENTS",
    "TASK_UPDATED_EVENTS",
    "TASK_ITERATION_EVENTS",
    "TASK_STATE_UPDATED_EVENTS",
    "RUN_PAUSED_EVENTS",
]
