"""Multi-client session mirroring events.

One BE session, N attached views (web tabs, IDE webviews, the TUI).
Every state change is an event so all views render identically.
These messages are BE→FE *broadcast* semantics — one session,
every attached view receives the same event — distinct from the
per-run BE→FE events in :mod:`.be_events`.
"""

from __future__ import annotations

from typing import Literal

from ember_code.protocol.schemas.envelope import Message


class Welcome(Message):
    """First message the BE sends a newly attached client.

    ``client_id`` lets a view recognise its own broadcasts (e.g. skip
    rendering an echo of a message it already painted locally).
    """

    type: Literal["welcome"] = "welcome"
    client_id: str = ""


class Typing(Message):
    """Live draft of a client's composer, broadcast to every view.

    Carries the FULL draft text (not per-character deltas) so views
    can't drift on a dropped frame; senders throttle. Empty ``text``
    clears the remote draft (sent on submit/clear).
    """

    type: Literal["typing"] = "typing"
    text: str = ""
    client_id: str = ""


class UserMessageReceived(Message):
    """Broadcast echo of an accepted user/queue message.

    The sending view already painted the bubble locally; other views
    paint it from this event (and the sender skips it by client_id).
    """

    type: Literal["user_message_received"] = "user_message_received"
    text: str = ""
    client_id: str = ""
    queued: bool = False


class RequirementResolved(Message):
    """Broadcast when a HITL requirement is decided by any view, so
    the other views dismiss their (now stale) permission dialogs."""

    type: Literal["requirement_resolved"] = "requirement_resolved"
    requirement_id: str = ""


__all__ = [
    "Welcome",
    "Typing",
    "UserMessageReceived",
    "RequirementResolved",
]
