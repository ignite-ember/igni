"""Polymorphic handlers for Agno streaming events.

One :class:`EventHandler` subclass per Agno event kind. The base
class owns the ``isinstance`` dispatch via its
:attr:`event_types` class attribute; the ``AgnoEventSerializer``
walks its handler list once per event and delegates to the first
match's :meth:`build`. Adding a new event kind means adding a new
subclass and appending it to the serializer's handler list — no
if/elif chain edit, no dispatch dict.

Each handler receives its collaborators (formatter, error
detector) via constructor so composition is explicit and the
handlers are testable in isolation with fakes.

Layering
--------

This module imports both :mod:`agno.run.*` (via ``agno_taxonomy``)
and :mod:`ember_code.protocol.messages`. It's the ONLY module in
the protocol package that spans that boundary — the serializer
just composes handlers, and the message + taxonomy modules stay
Agno-free / message-free respectively. This keeps
``permission_eval``'s lazy-import trick working: nothing in
handler-land is imported at package init time.
"""

from __future__ import annotations

import logging
from typing import Any

from ember_code.protocol import messages as msg
from ember_code.protocol.agno_taxonomy import (
    CONTENT_EVENTS,
    MODEL_COMPLETED_EVENTS,
    REASONING_CONTENT_EVENTS,
    REASONING_EVENTS,
    RUN_COMPLETED_EVENTS,
    RUN_CONTENT_COMPLETED_EVENTS,
    RUN_ERROR_EVENTS,
    RUN_PAUSED_EVENTS,
    RUN_STARTED_EVENTS,
    TASK_CREATED_EVENTS,
    TASK_ITERATION_EVENTS,
    TASK_STATE_UPDATED_EVENTS,
    TASK_UPDATED_EVENTS,
    TOOL_COMPLETED_EVENTS,
    TOOL_ERROR_EVENTS,
    TOOL_STARTED_EVENTS,
)
from ember_code.protocol.agno_tool_formatter import AgnoToolEventFormatter
from ember_code.protocol.tool_error_conventions import ToolResultErrorDetector

logger = logging.getLogger(__name__)


# ── Base class ────────────────────────────────────────────────────


class EventHandler:
    """ABC for one-Agno-event-kind → one-protocol-message.

    Subclasses set :attr:`event_types` to the Agno event tuple they
    handle and override :meth:`build`. The base class owns the
    ``isinstance`` matching so the serializer's dispatch loop is a
    plain "first matching handler wins" walk.

    Handlers take their collaborators via constructor
    (``formatter`` + ``error_detector``) — no module globals, no
    reach-through to a parent serializer. This makes each handler
    unit-testable with a fake formatter / detector.
    """

    #: Tuple of Agno event classes this handler recognises. Empty
    #: is a bug — every concrete handler must set this.
    event_types: tuple[type, ...] = ()

    def __init__(
        self,
        formatter: AgnoToolEventFormatter,
        error_detector: ToolResultErrorDetector,
    ) -> None:
        self._formatter = formatter
        self._error_detector = error_detector

    def matches(self, event: Any) -> bool:
        """Return True when this handler should build ``event``.

        Default implementation is ``isinstance(event,
        self.event_types)``. Fallback handlers (e.g.
        :class:`FallbackContentHandler`) override this to inspect
        the event's duck-typed shape.
        """
        return isinstance(event, self.event_types)

    def build(self, event: Any) -> msg.Message | None:  # pragma: no cover - ABC
        """Translate ``event`` to a protocol message (or ``None``
        when the event should not cross the BE→FE boundary)."""
        raise NotImplementedError


# ── Concrete handlers ────────────────────────────────────────────


class ReasoningContentHandler(EventHandler):
    """Native reasoning-content delta.

    Strips the MiniMax ``<think>...</think>`` wrapper in
    :meth:`_unwrap_provider_reasoning` — the FE already renders
    this delta inside a thinking bubble that styles it as
    reasoning, so leaving the literal tags in would show them as
    raw text.
    """

    event_types = REASONING_CONTENT_EVENTS

    def build(self, event: Any) -> msg.Message | None:
        rc = getattr(event, "reasoning_content", "") or ""
        if not rc:
            return None
        rc = self._unwrap_provider_reasoning(rc)
        return msg.ContentDelta(text=rc, is_thinking=True)

    @staticmethod
    def _unwrap_provider_reasoning(rc: str) -> str:
        """Strip literal ``<think>``/``</think>`` tags that some
        providers (notably MiniMax) emit inside a reasoning-content
        event even though Agno already surfaces the delta on the
        dedicated event stream.

        Named as its own method so the workaround comment lives at
        the operation it describes — otherwise the "why" drifts
        away from the "what" over time.
        """
        return rc.replace("<think>", "").replace("</think>", "")


class ContentHandler(EventHandler):
    """Streamed visible-content delta."""

    event_types = CONTENT_EVENTS

    def build(self, event: Any) -> msg.Message | None:
        content = event.content or ""
        if not content:
            return None
        return msg.ContentDelta(text=content, is_thinking=False)


class ToolStartedHandler(EventHandler):
    """A tool call has begun."""

    event_types = TOOL_STARTED_EVENTS

    def build(self, event: Any) -> msg.Message | None:
        tool_exec = event.tool
        raw_name = (tool_exec.tool_name or "tool") if tool_exec else "tool"
        friendly = self._formatter.friendly_name(raw_name)
        args_summary = self._formatter.args_summary(
            raw_name,
            tool_exec.tool_args if tool_exec else None,
        )
        header = msg.RunHeader.from_event(event)
        return msg.ToolStarted(
            tool_name=raw_name,
            friendly_name=friendly,
            args_summary=args_summary,
            run_id=header.run_id,
        )


class ToolCompletedHandler(EventHandler):
    """A tool call finished (successfully or with a caught error).

    Delegates error detection to the injected
    :class:`ToolResultErrorDetector` — Agno raises
    ``TOOL_ERROR_EVENTS`` only for uncaught exceptions, so
    tool-side failure conventions (``Error:`` prefix, shell exit
    codes) need this second pass to keep the TUI honest.
    """

    event_types = TOOL_COMPLETED_EVENTS

    def build(self, event: Any) -> msg.Message | None:
        data = self._formatter.extract_result(event)
        is_error = self._error_detector.is_error(data.full_result)
        header = msg.RunHeader.from_event(event)
        return msg.ToolCompleted(
            summary=data.summary,
            full_result=data.full_result,
            has_markup=data.has_markup,
            diff_rows=data.diff_rows,
            run_id=header.run_id,
            is_error=is_error,
        )


class ToolErrorHandler(EventHandler):
    """A tool call raised an uncaught exception."""

    event_types = TOOL_ERROR_EVENTS

    def build(self, event: Any) -> msg.Message | None:
        header = msg.RunHeader.from_event(event)
        return msg.ToolError(
            error=str(getattr(event, "error", "Unknown error")),
            run_id=header.run_id,
        )


class ModelCompletedHandler(EventHandler):
    """Model response finished — token counts available."""

    event_types = MODEL_COMPLETED_EVENTS

    def build(self, event: Any) -> msg.Message | None:
        header = msg.RunHeader.from_event(event)
        return msg.ModelCompleted(
            input_tokens=getattr(event, "input_tokens", 0) or 0,
            output_tokens=getattr(event, "output_tokens", 0) or 0,
            run_id=header.run_id,
            parent_run_id=header.parent_run_id,
        )


class RunStartedHandler(EventHandler):
    """An agent/team run has begun.

    Skips emission when the event carries neither an agent/team
    name nor a run id — the pre-refactor behaviour, preserved for
    the tail of pause/resume runs where Agno re-emits a partial
    started event.
    """

    event_types = RUN_STARTED_EVENTS

    def build(self, event: Any) -> msg.Message | None:
        name = getattr(event, "agent_name", None) or getattr(event, "team_name", None) or ""
        run_id = getattr(event, "run_id", None) or ""
        if not (name and run_id):
            return None
        header = msg.RunHeader.from_event(event)
        return msg.RunStarted(
            agent_name=str(name),
            run_id=str(run_id),
            parent_run_id=header.parent_run_id,
            model=str(getattr(event, "model", "") or ""),
        )


class RunCompletedHandler(EventHandler):
    """An agent/team run has finished — carries token / duration
    metrics."""

    event_types = RUN_COMPLETED_EVENTS

    def build(self, event: Any) -> msg.Message | None:
        header = msg.RunHeader.from_event(event)
        evt_metrics = getattr(event, "metrics", None)
        input_tokens = (getattr(evt_metrics, "input_tokens", 0) or 0) if evt_metrics else 0
        output_tokens = (getattr(evt_metrics, "output_tokens", 0) or 0) if evt_metrics else 0
        reasoning_tokens = (getattr(evt_metrics, "reasoning_tokens", 0) or 0) if evt_metrics else 0
        duration = float(getattr(evt_metrics, "duration", 0) or 0) if evt_metrics else 0.0
        return msg.RunCompleted(
            run_id=header.run_id,
            parent_run_id=header.parent_run_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            duration=duration,
        )


class StreamingDoneHandler(EventHandler):
    """Visible-content stream ended (Agno tail may still be
    running).

    Fires when Agno finishes streaming model content but before
    the post-stream tail (memory / learning extraction,
    compression, persistence) completes. The FE uses this to
    unblock user input ~immediately after the visible response
    ends — without it the queue panel stays visible for the full
    Agno tail (5-15s observed). Distinct from
    :class:`RunCompletedHandler` which marks the *whole* run done.
    """

    event_types = RUN_CONTENT_COMPLETED_EVENTS

    def build(self, event: Any) -> msg.Message | None:
        header = msg.RunHeader.from_event(event)
        return msg.StreamingDone(run_id=header.run_id)


class RunErrorHandler(EventHandler):
    """Run-level error."""

    event_types = RUN_ERROR_EVENTS

    def build(self, event: Any) -> msg.Message | None:
        return msg.RunError(error=str(getattr(event, "content", "Unknown error")))


class ReasoningStartedHandler(EventHandler):
    """Model entered its reasoning/thinking phase."""

    event_types = REASONING_EVENTS

    def build(self, event: Any) -> msg.Message | None:
        header = msg.RunHeader.from_event(event)
        return msg.ReasoningStarted(run_id=header.run_id)


class TaskCreatedHandler(EventHandler):
    """Team task orchestrator created a new task."""

    event_types = TASK_CREATED_EVENTS

    def build(self, event: Any) -> msg.Message | None:
        return msg.TaskCreated(
            task_id=str(getattr(event, "task_id", "")),
            title=str(getattr(event, "title", "")),
            assignee=str(getattr(event, "assignee", "") or ""),
            status=str(getattr(event, "status", "pending")),
        )


class TaskUpdatedHandler(EventHandler):
    """Team task orchestrator updated a task's status/assignee."""

    event_types = TASK_UPDATED_EVENTS

    def build(self, event: Any) -> msg.Message | None:
        return msg.TaskUpdated(
            task_id=str(getattr(event, "task_id", "")),
            status=str(getattr(event, "status", "")),
            assignee=str(getattr(event, "assignee", "") or ""),
        )


class TaskIterationHandler(EventHandler):
    """Team task iteration progress."""

    event_types = TASK_ITERATION_EVENTS

    def build(self, event: Any) -> msg.Message | None:
        return msg.TaskIteration(
            iteration=getattr(event, "iteration", 0),
            max_iterations=getattr(event, "max_iterations", 0),
        )


class TaskStateUpdatedHandler(EventHandler):
    """Batch task-state update.

    Builds a ``list[TaskSnapshot]`` via
    :meth:`TaskSnapshot.from_agno` — the wire model owns the
    Agno → snapshot mapping, this handler just plumbs.
    """

    event_types = TASK_STATE_UPDATED_EVENTS

    def build(self, event: Any) -> msg.Message | None:
        tasks = getattr(event, "tasks", []) or []
        snapshots = [msg.TaskSnapshot.from_agno(t) for t in tasks]
        return msg.TaskStateUpdated(tasks=snapshots)


class RunPausedHandler(EventHandler):
    """HITL pause — collect every active permission requirement.

    Delegates wire-mapping of each requirement to
    :meth:`HITLRequest.from_agno_requirement`, passing the
    formatter's ``friendly_name`` bound method as the lookup
    callable. Keeps the "how do we turn an Agno pause requirement
    into a protocol row?" logic on the wire model.
    """

    event_types = RUN_PAUSED_EVENTS

    def build(self, event: Any) -> msg.Message | None:
        requirements: list[msg.HITLRequest] = []
        for req in getattr(event, "active_requirements", []) or []:
            requirements.append(
                msg.HITLRequest.from_agno_requirement(
                    req,
                    friendly_name_lookup=self._formatter.friendly_name,
                )
            )
        header = msg.RunHeader.from_event(event)
        return msg.RunPaused(
            run_id=header.run_id,
            requirements=requirements,
        )


class FallbackContentHandler(EventHandler):
    """Terminal handler for content-like events not caught above.

    Some Agno events (and MagicMock-shaped test events) expose a
    plain string ``.content`` attribute without inheriting from
    any known event class. Duck-type match here so the TUI still
    surfaces the payload rather than dropping it. Runs LAST — the
    typed handlers above take priority.
    """

    # No ``event_types`` — matches by duck typing instead.

    def matches(self, event: Any) -> bool:
        return hasattr(event, "content") and isinstance(getattr(event, "content", None), str)

    def build(self, event: Any) -> msg.Message | None:
        content = event.content
        if not content:
            return None
        return msg.ContentDelta(text=content, is_thinking=False)


__all__ = [
    "EventHandler",
    "ReasoningContentHandler",
    "ContentHandler",
    "ToolStartedHandler",
    "ToolCompletedHandler",
    "ToolErrorHandler",
    "ModelCompletedHandler",
    "RunStartedHandler",
    "RunCompletedHandler",
    "StreamingDoneHandler",
    "RunErrorHandler",
    "ReasoningStartedHandler",
    "TaskCreatedHandler",
    "TaskUpdatedHandler",
    "TaskIterationHandler",
    "TaskStateUpdatedHandler",
    "RunPausedHandler",
    "FallbackContentHandler",
]
