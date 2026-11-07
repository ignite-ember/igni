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
(React web, Tauri, TUI) that parses those
fields at the top level.

The mixin approach avoids that: :class:`RunScopedMessage`
inherits from :class:`Message` and adds ``run_id`` + ``parent_run_id``
directly, so the wire stays flat. The :meth:`RunScopedMessage.header`
convenience method materialises a :class:`RunHeader` on demand for
callers that want the value object.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, PrivateAttr


class Standing(StrEnum):
    """What a connection may do, decided at its handshake (``transport/ws_auth.py``).

    * ``WINDOW`` — igni's own window: it offered the launch secret, which
      only the process that started the backend holds. Everything,
      approving the agent's tool calls and speaking for an extension
      included. A connection to a backend that requires no secret (one
      started by hand, a cloud VM's behind its gateway) and a message from
      a transport without connections (the terminal's) stand here too.
    * ``APPROVER`` — a program the window let answer the agent's approval
      requests, with a token it minted. A program otherwise.
    * ``PROGRAM`` — a program that attached with the lockfile's token. It
      runs sessions, but approves nothing, speaks for no extension and
      loosens no rule (``backend/standing_policy.py``): the agent, which
      can read the lockfile, must not be able to approve itself.
    """

    WINDOW = "window"
    APPROVER = "approver"
    PROGRAM = "program"


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
    # double-mount of IgniClient) are dropped; the monotonic
    # counter also pins the canonical ordering of events in a
    # stream. Empty on FE→BE messages.
    event_seq: int = 0
    #: Arrived on the platform's own control connection (a gateway's): set
    #: by the transport from the connection, never read from the message,
    #: so a client cannot claim it. See :meth:`via_control`.
    _via_control: bool = PrivateAttr(default=False)
    #: The connection this arrived on — set by the transport, never read
    #: from the message, so a client cannot speak as another connection.
    #: Empty for a message from a transport without connections (the
    #: terminal's socket, an in-process client).
    _from_client: str = PrivateAttr(default="")
    #: The one connection this is for — set by whoever replies. Empty
    #: broadcasts, as every message did before; a reply that carries a
    #: secret or one extension's data is addressed instead.
    _to_client: str = PrivateAttr(default="")
    #: What the connection this arrived on may do — set by the transport
    #: from the handshake, never read from the message.
    _standing: Standing = PrivateAttr(default=Standing.WINDOW)

    @property
    def via_control(self) -> bool:
        return self._via_control

    def mark_control(self) -> None:
        self._via_control = True

    @property
    def from_client(self) -> str:
        return self._from_client

    def mark_from(self, client_id: str, standing: Standing = Standing.WINDOW) -> None:
        self._from_client = client_id
        self._standing = standing

    @property
    def standing(self) -> Standing:
        return self._standing

    @property
    def to_client(self) -> str:
        return self._to_client

    def address_to(self, client_id: str) -> Self:
        """Send this only to ``client_id``; an empty id leaves it a broadcast."""
        self._to_client = client_id or ""
        return self


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
