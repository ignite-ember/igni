"""Where a delivered service item goes in this backend (``extensions/service_events.py``).

The inbox is the core's; this module is what it needs from a running
backend: a session of the pool, as an unattended post reaches it
(:class:`PoolSession` — ``host.sessions.chat(id).post`` and every item a
service sends take it), and the extension's process, asked over its host
socket (:class:`ExtensionEventHandler`). Which session an item goes to is
``extensions/service_landing.py``'s, over ``backend/service_item_desk.py``.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from pydantic_core import to_jsonable_python

from ember_code.backend.activity import ActivityState
from ember_code.backend.schemas_push import SessionNamedPayload
from ember_code.core.session.schemas import PersistResult
from ember_code.extensions.host.hooks import ExtensionHooks, HookFailure
from ember_code.extensions.host.wire import Outcome
from ember_code.extensions.service_events import EventHandling, ServiceEvent, UnattendedPost
from ember_code.protocol import messages as msg

logger = logging.getLogger(__name__)


class PoolSession:
    """One session of the pool, as a message nobody may be watching reaches it.

    ``extensions/service_events.py``, step 3: with nothing going on, the
    message starts a run; with a run going — working, or waiting on an
    approval — it is queued for that run, so it never answers, or goes
    round, a decision that is the person's. A service's items reach their
    chats this way, and an extension's ``host.sessions.chat(id).post``
    reaches any session of the project.
    """

    def __init__(self, orchestrator: Any, session_id: str) -> None:
        self._orchestrator = orchestrator
        self._session_id = session_id

    def _runtime(self) -> Any:
        return self._orchestrator.pool.find(self._session_id)

    @property
    def session_id(self) -> str:
        return self._session_id

    def busy(self) -> bool:
        """Working, or waiting on an approval: anything but idle."""
        runtime = self._runtime()
        probe = getattr(runtime.backend, "activity", None) if runtime is not None else None
        if probe is None:
            return False
        return probe().state is not ActivityState.IDLE

    async def post(
        self,
        *,
        extension_id: str,
        text: str,
        queued: bool,
        media: dict[str, str] | None = None,
        from_service: str = "",
    ) -> None:
        """Posted as ``extension_id``'s; ``from_service`` is its service's client id
        when the service sent it, and marks the post in ``chat.message_added``."""
        await self._orchestrator.post_from_extension(
            session_id=self.session_id,
            extension_id=extension_id,
            text=text,
            media=media,
            queued=queued,
            from_service=from_service,
        )

    async def deliver(
        self,
        *,
        extension_id: str,
        text: str,
        media: dict[str, str] | None,
        from_service: str = "",
    ) -> UnattendedPost:
        """Start a run with it, or queue it for the run going — as an event is."""
        queued = self.busy()
        if queued and media:
            return UnattendedPost(
                ok=False,
                reason=f"a run is going in session {self.session_id}: a message queued for "
                "it cannot carry attachments. Post the text alone, or post again when the "
                "run has finished.",
            )
        await self.post(
            extension_id=extension_id,
            text=text,
            queued=queued,
            media=media,
            from_service=from_service,
        )
        return UnattendedPost(queued=queued)

    async def title(self, title: str) -> PersistResult:
        """Name the session now, before anything has run in it, so no auto-name
        replaces the title; every window is told (``session_named``)."""
        runtime = await self._orchestrator.pool.get_or_create(self.session_id)
        titled: PersistResult = await runtime.backend.title_session(title)
        if titled.ok:
            await runtime.transport.send(
                msg.push_session_named(
                    SessionNamedPayload(session_id=self.session_id, name=title).model_dump()
                )
            )
        return titled


class ExtensionEventHandler:
    """Hands an event to the extension it is for, and learns whether a handler took it.

    The SDK answers ``allow`` once a handler for the event's type ran,
    ``reject`` when it has none, and ``failed`` when the handler raised.
    Anything but ``allow`` — or no answer: not installed, not running,
    too slow — is *not handled*, and the service is told. igni posts
    nothing for an event either way; a handler that wants the agent to
    act posts through its host calls.

    An older SDK answers ``modify`` with ``{"prompt": text}`` when its
    handler returned text: that is handled, and the text is dropped —
    igni no longer posts what a handler returns.
    """

    #: Longer than a hook's — the handler may do its work first — and well
    #: under the gateway's 30 s for the whole delivery.
    TIMEOUT_S: ClassVar[float] = 10.0
    #: How long a process that is not connected yet is waited for. One
    #: not installed or not running here is not waited for at all.
    CONNECT_TIMEOUT_S: ClassVar[float] = 2.0
    #: The frame's event name, the SDK's ``SERVICE_EVENT``.
    EVENT: ClassVar[str] = "ServiceEvent"

    def __init__(self, hooks: ExtensionHooks | None = None) -> None:
        self._hooks = hooks

    async def __call__(self, event: ServiceEvent) -> EventHandling:
        hooks = self._hooks if self._hooks is not None else ExtensionHooks.shared()
        try:
            answer = await hooks.ask_one(
                event.extension_id,
                self.EVENT,
                to_jsonable_python(event.model_dump(mode="json")),
                timeout=self.TIMEOUT_S,
                connect_timeout=self.CONNECT_TIMEOUT_S,
            )
        except HookFailure as exc:
            return EventHandling(handled=False, reason=str(exc))
        if answer.outcome is Outcome.REJECT:
            return EventHandling(
                handled=False, reason=answer.reason or f"no handler for {event.type}"
            )
        if answer.outcome is Outcome.FAILED:
            return EventHandling(handled=False, reason=f"the handler failed: {answer.reason}")
        if answer.outcome is Outcome.MODIFY:
            logger.warning(
                "%s's handler of %s returned text; igni no longer posts it — "
                "post with host.sessions.chat(id).post instead",
                event.extension_id,
                event.type,
            )
        return EventHandling(handled=True)


__all__ = ["ExtensionEventHandler", "PoolSession"]
