"""Events your service sends: `@ext.on_service_event`.

Your service posts an event for a producer (igni server's
`docs/service-identity.md` §15) — "the index is ready", "the build
failed". igni hands it to that producer's igni, which always starts or
resumes the agent's run with it, whether or not anybody is watching.
Before it does, it asks this extension: a handler registered for the
event's `type` (or for every type) is handed the event and may answer
with the text the agent should see:

    @ext.on_service_event("index.ready")
    async def index_ready(event: ServiceEvent) -> str:
        return f"The code index for {event.payload['repo']} is ready; re-run the review."

Returning `None` lets igni write it from the event's type and payload.
A handler should be quick — igni waits on it before the run goes on —
and may be handed the same event twice (delivery is at least once; igni
drops the repeat afterwards, by `event_id`).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from igni_extension.caller import Caller, CallerScope
from igni_extension.threads import OffTheLoop

logger = logging.getLogger(__name__)

#: The frame's event name on the host socket.
SERVICE_EVENT = "ServiceEvent"


class ServiceEvent(BaseModel):
    """One event from this extension's service."""

    model_config = ConfigDict(extra="allow")

    event_id: str
    extension_id: str = ""
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    event_key: str | None = None
    created_at: str | None = None
    expires_at: str | None = None


ServiceEventHandler = Callable[[ServiceEvent], Awaitable[str | None] | str | None]


class ServiceEventHandlers:
    """One handler per event type, and one for any type."""

    #: Registered for every type the others do not name.
    ANY = "*"

    def __init__(self) -> None:
        self._handlers: dict[str, ServiceEventHandler] = {}

    def on(
        self, event_type: str | None = None
    ) -> Callable[[ServiceEventHandler], ServiceEventHandler]:
        key = event_type or self.ANY

        def register(handler: ServiceEventHandler) -> ServiceEventHandler:
            if key in self._handlers:
                raise ValueError(f"service event {key!r} has a handler already")
            self._handlers[key] = handler
            return handler

        return register

    async def answer(self, hook_id: str, raw: Any, principal: Any = None) -> dict[str, Any]:
        """The `answer` request's parameters: the agent's text, or nothing to add."""
        try:
            event = ServiceEvent.model_validate(raw)
        except Exception as exc:
            return {"hook": hook_id, "outcome": "failed", "reason": f"not an event: {exc}"}
        handler = self._handlers.get(event.type) or self._handlers.get(self.ANY)
        if handler is None:
            return {"hook": hook_id, "outcome": "allow"}
        try:
            with CallerScope.of(Caller.of(principal)):
                text = await OffTheLoop.call(handler, event)
        except Exception as exc:
            logger.exception("igni: the handler of service event %s failed", event.type)
            return {"hook": hook_id, "outcome": "failed", "reason": str(exc)}
        if not text:
            return {"hook": hook_id, "outcome": "allow"}
        return {"hook": hook_id, "outcome": "modify", "value": {"prompt": str(text)}}


__all__ = ["SERVICE_EVENT", "ServiceEvent", "ServiceEventHandler", "ServiceEventHandlers"]
