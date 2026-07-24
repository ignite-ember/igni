"""Base :class:`Message` envelope + shared value objects.

Every wire message inherits from :class:`Message`; every run-scoped
event carries the ``(run_id, parent_run_id)`` pair captured by
:class:`RunHeader`.

Design note — MIXIN, not embedded field
---------------------------------------
An earlier iteration proposed embedding a ``header: RunHeader``
sub-object on every run-scoped event. That would have nested the
wire JSON from flat ``{run_id, parent_run_id}`` to
``{header: {run_id, parent_run_id}}`` — breaking every FE client
(React web, Tauri, VSCode, JetBrains, TUI) that parses those
fields at the top level.

The mixin approach avoids that: :class:`RunScopedMessage`
inherits from :class:`Message` and adds ``run_id`` + ``parent_run_id``
directly, so the wire stays flat. The :meth:`RunScopedMessage.header`
convenience method materialises a :class:`RunHeader` on demand for
callers that want the value object.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class Message(BaseModel):
    """Base envelope for all protocol messages."""

    type: str
    id: str = ""  # optional correlation ID
    # Session routing (multi-session BE). FE→BE: which session this
    # message targets — empty routes to the default session, so views
    # that predate the pool (the TUI) keep working unchanged. BE→FE:
    # which session emitted the event — views filter to their bound
    # session; empty means session-agnostic (Welcome, global pushes).
    session_id: str = ""
    # Per-frame sequence number stamped on every BE→FE send by
    # :meth:`WebSocketServerTransport.send`. Resets to 1 at the
    # start of every stream (i.e. after ``stream_end``). The FE
    # dedups by (id, event_seq) so events that arrive twice (e.g.
    # when two WebSocket clients are attached due to a StrictMode
    # double-mount of EmberClient) are dropped; the monotonic
    # counter also pins the canonical ordering of events in a
    # stream. Empty on FE→BE messages.
    event_seq: int = 0


class RunHeader(BaseModel):
    """The ``(run_id, parent_run_id)`` pair carried on most run
    events.

    Pre-refactor the serializer inlined
    ``str(getattr(event, "run_id", "") or "")`` at eight callsites
    (one per branch). Extracting the read into
    :meth:`from_event` centralises the "any-Agno-event →
    normalised str ids" rule so a new event kind only needs the
    call, not another verbatim getattr chain.
    """

    run_id: str = ""
    parent_run_id: str = ""

    @classmethod
    def from_event(cls, event: Any) -> RunHeader:
        """Read ``run_id`` / ``parent_run_id`` off an Agno event.

        Missing attrs / falsy values become empty strings so the
        wire schema stays flat (no Nones leak into the JSON).
        """
        return cls(
            run_id=str(getattr(event, "run_id", "") or ""),
            parent_run_id=str(getattr(event, "parent_run_id", "") or ""),
        )


class RunScopedMessage(Message):
    """Base for every run-scoped event.

    Adds ``run_id`` + ``parent_run_id`` directly on the wire (flat
    JSON, backward-compatible with every FE client). Provides a
    :meth:`header` convenience method to materialise a
    :class:`RunHeader` for callers that want the value object.

    Prefer subclassing this over :class:`Message` for any new
    run-scoped event so the field pair (and the ``header()``
    helper) come from one place.
    """

    run_id: str = ""
    parent_run_id: str = ""

    def header(self) -> RunHeader:
        """Materialise a :class:`RunHeader` from the two flat
        fields. Cheap — Pydantic construction is roughly the cost
        of a dict literal — so callers that want the value object
        can request one without changing the wire shape."""
        return RunHeader(run_id=self.run_id, parent_run_id=self.parent_run_id)


__all__ = ["Message", "RunHeader", "RunScopedMessage"]
