"""Session-stamping transport wrapper.

One-class-per-file home for :class:`SessionStampingTransport`, moved
out of :mod:`ember_code.backend.session_pool` to match the sibling
one-class-per-file convention used by :mod:`push_bridge`,
:mod:`hitl_stream_mux`, :mod:`stream_event_dispatcher` etc.

The wrapper stamps outbound events with the emitting runtime's
current session id (unless the sender already stamped one) so views
can filter the broadcast stream to their bound session.

:mod:`ember_code.backend.session_pool` re-exports this symbol for
one release cycle so any lagging in-tree importer keeps working.
"""

from __future__ import annotations

from typing import Any

from ember_code.backend.schemas_sessions import BackendLike, TransportLike
from ember_code.protocol import messages as msg


class SessionStampingTransport:
    """Transport wrapper that stamps outbound events with the
    emitting runtime's CURRENT session id.

    The two constructor args are typed against structural Protocols
    (:class:`BackendLike`, :class:`TransportLike`) from
    :mod:`schemas_sessions` — the ``Any`` on both fields in the
    pre-refactor version was hiding a real seam.

    Unknown attribute lookups delegate to the inner transport via
    :meth:`__getattr__` so richer transport surfaces (``receive``,
    ``close``, and everything :class:`PushNotificationBridge.for_transport`
    relies on) remain reachable through the wrapper without having
    to enumerate them here.
    """

    def __init__(self, inner: TransportLike, backend: BackendLike) -> None:
        self._inner = inner
        self._backend = backend

    async def send(self, message: msg.Message) -> None:
        """Stamp + forward. When the sender already set ``session_id``
        (typical for RPC responses that already know their bound
        session) the message is forwarded verbatim; otherwise the
        backend's current id is stamped in via
        :meth:`Message.model_copy` before dispatch.
        """
        if not message.session_id:
            sid = self._backend.session_id
            if sid:
                message = message.model_copy(update={"session_id": sid})
        await self._inner.send(message)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


__all__ = ["SessionStampingTransport"]
