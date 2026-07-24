"""Serialize Agno streaming events into protocol messages.

This is the ONLY module that imports both Agno event types and
protocol messages. It translates Agno's internal event model into
the transport-agnostic protocol.

Design
------

:class:`AgnoEventSerializer` is the class-based surface. It owns:

* an :class:`AgnoToolEventFormatter` (composed with an
  :class:`EditDiffComputer` so the ``edit_file`` branch of
  ``extract_result`` produces the diff rows from live disk),
* a :class:`ToolResultErrorDetector` for tool-side failure
  conventions,
* an ordered list of :class:`EventHandler` instances, one per
  Agno event kind — polymorphism replaces the 17-branch
  ``isinstance`` chain the module used to carry.

Adding a new event kind = add a new ``EventHandler`` subclass in
:mod:`event_handlers` and append an instance to
:meth:`AgnoEventSerializer._build_default_handlers`. No edit here
past that.

Compatibility shim
------------------

:func:`serialize_event` remains a module-level function so
existing callers (``backend.hitl_stream_mux`` line 220 and the
five test callsites in ``tests/test_protocol_messages.py``) keep
working without touching them. It delegates to a lazily-built
default :class:`AgnoEventSerializer` — the default is built via
:meth:`AgnoEventSerializer.default` (a classmethod, not a module
global) so no hidden shared mutable state re-enters through the
back door. Callers that need isolation construct their own
serializer.
"""

from __future__ import annotations

import logging
from typing import Any

from ember_code.protocol import messages as msg
from ember_code.protocol.agno_tool_formatter import AgnoToolEventFormatter
from ember_code.protocol.edit_diff_computer import EditDiffComputer
from ember_code.protocol.event_handlers import (
    ContentHandler,
    EventHandler,
    FallbackContentHandler,
    ModelCompletedHandler,
    ReasoningContentHandler,
    ReasoningStartedHandler,
    RunCompletedHandler,
    RunErrorHandler,
    RunPausedHandler,
    RunStartedHandler,
    StreamingDoneHandler,
    TaskCreatedHandler,
    TaskIterationHandler,
    TaskStateUpdatedHandler,
    TaskUpdatedHandler,
    ToolCompletedHandler,
    ToolErrorHandler,
    ToolStartedHandler,
)
from ember_code.protocol.tool_error_conventions import ToolResultErrorDetector

logger = logging.getLogger(__name__)


class AgnoEventSerializer:
    """Translate Agno streaming events into protocol messages.

    Composes a :class:`AgnoToolEventFormatter`, a
    :class:`ToolResultErrorDetector`, and a list of
    :class:`EventHandler` instances. :meth:`serialize` walks the
    handler list once per event and returns the first match's
    build result — polymorphic dispatch replaces the pre-refactor
    17-branch ``isinstance`` chain.

    Construction is explicit; callers that want the production
    shape (default registry + on-disk diff computer + default
    error detector) call :meth:`default`. Tests can inject
    bespoke collaborators via the constructor.
    """

    def __init__(
        self,
        formatter: AgnoToolEventFormatter,
        error_detector: ToolResultErrorDetector,
        handlers: list[EventHandler] | None = None,
    ) -> None:
        self._formatter = formatter
        self._error_detector = error_detector
        self._handlers: tuple[EventHandler, ...] = tuple(
            handlers if handlers is not None else self._build_default_handlers()
        )

    @classmethod
    def default(cls) -> AgnoEventSerializer:
        """Build the production serializer.

        The formatter is composed with an :class:`EditDiffComputer`
        so the ``edit_file`` branch of ``extract_result`` produces
        the diff rows from live disk. The error detector uses the
        stock ``Error:`` prefix + shell exit-code conventions.

        Returned instance is fresh — callers own it. No module
        singleton lives behind this factory.
        """
        formatter = AgnoToolEventFormatter(diff_computer=EditDiffComputer())
        error_detector = ToolResultErrorDetector.default()
        return cls(formatter=formatter, error_detector=error_detector)

    def _build_default_handlers(self) -> list[EventHandler]:
        """Assemble the ordered handler list.

        Order matters: the more-specific typed handlers run before
        the duck-typed :class:`FallbackContentHandler` at the tail
        so an event that carries a ``.content`` attribute but is
        also a known kind (e.g. ``RunContentEvent``) hits its
        typed branch first.
        """
        f = self._formatter
        d = self._error_detector
        return [
            ReasoningContentHandler(f, d),
            ContentHandler(f, d),
            ToolStartedHandler(f, d),
            ToolCompletedHandler(f, d),
            ToolErrorHandler(f, d),
            ModelCompletedHandler(f, d),
            RunStartedHandler(f, d),
            RunCompletedHandler(f, d),
            StreamingDoneHandler(f, d),
            RunErrorHandler(f, d),
            ReasoningStartedHandler(f, d),
            TaskCreatedHandler(f, d),
            TaskUpdatedHandler(f, d),
            TaskIterationHandler(f, d),
            TaskStateUpdatedHandler(f, d),
            RunPausedHandler(f, d),
            FallbackContentHandler(f, d),
        ]

    def serialize(self, event: Any) -> msg.Message | None:
        """Convert an Agno streaming event to a protocol message.

        Returns ``None`` for events that don't need to cross the
        BE→FE boundary (e.g. pre-hook events handled internally by
        the BE), or when no handler matches.

        ``event: Any`` because the closed union of Agno event
        classes is scattered across two modules
        (``agno.run.agent`` / ``agno.run.team``) with no shared
        base class we can name. Tightening this to an
        ``AgnoEvent`` alias is a cross-cutting change deferred to
        the taxonomy module.
        """
        for handler in self._handlers:
            if handler.matches(event):
                return handler.build(event)

        logger.debug("Unserializable Agno event: %s", type(event).__name__)
        return None


# ── Module-level compatibility shim ───────────────────────────────
#
# Existing callers (``backend.hitl_stream_mux`` and the tests) call
# :func:`serialize_event` as a free function. We keep that API by
# constructing a fresh :class:`AgnoEventSerializer` via
# :meth:`.default` per call — no module singleton escapes. New code
# should construct one serializer per session for testability.


def serialize_event(event: Any) -> msg.Message | None:
    """Convert an Agno streaming event to a protocol message.

    Thin compatibility shim over
    :meth:`AgnoEventSerializer.default().serialize`. Kept as a
    free function so pre-refactor callers
    (``backend/hitl_stream_mux.py`` and the five test callsites
    in ``tests/test_protocol_messages.py``) don't need to change.
    """
    return AgnoEventSerializer.default().serialize(event)


__all__ = ["AgnoEventSerializer", "serialize_event"]
