"""Events a service sends its extension, for a producer: they start or resume the agent's run.

igni server's ``docs/service-identity.md`` §15 and ``docs/CLOUD.md`` §6.
A service posts an event for its own extension and a producer; the
platform hands it to the producer's igni (in the cloud the gateway pulls
it with a lease and acknowledges what igni accepted — delivery is at
least once). Here, for each event:

1. **Once.** An event already delivered — the same ``event_id`` handed
   out again because the acknowledgement was lost — is acknowledged and
   nothing else: :class:`DeliveredEvents` keeps the ids, in the
   producer's database, so a restart does not forget them.
2. **The extension's handler runs.** The extension it is for is asked
   (``ServiceEvent``, a deciding frame on its host socket; the SDK's
   ``@ext.on_service_event``) and may answer with the text the agent
   should see. Without an answer — no handler, not running, too slow —
   igni writes the text itself from the event's type and payload.
3. **The agent's run, unattended.** The text goes to the producer's
   main session as the extension's message (``host.chat.post``, labelled
   as the extension's, never the person's). With nothing going on it
   **starts a run**. With a run going — working, or **waiting on an
   approval** — it is **queued for that run**, as a message typed during
   a run is (durably, ``queued_messages``): the run takes it after its
   next step, and a run paused on an approval takes it once the person
   has decided and the run goes on. Not a second run beside it: two runs
   in one conversation would race over its history, and an event must
   never answer, or go round, a decision that is the person's. Approvals
   the run then hits wait as usual.

Recorded as delivered only after step 3 is durable — the run started
(its message journalled) or the message queued — so a crash before then
gets the event handed out again, and a crash after acknowledges the
repeat.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, PrimaryKeyConstraint, String, select
from sqlalchemy.orm import Mapped, mapped_column

from ember_code.core.db.base import Base
from ember_code.core.db.database import Database
from ember_code.core.db.scope import ScopedColumns

logger = logging.getLogger(__name__)


class ServiceEvent(BaseModel):
    """One event, as the platform hands it out (``/events/pull``)."""

    event_id: str
    extension_id: str
    client_id: str = ""
    #: The extension's own word: ``index.ready``.
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    event_key: str | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None


class DeliveryReceipt(BaseModel):
    """What became of a batch: acknowledge ``delivered`` and ``duplicate``."""

    delivered: list[str] = Field(default_factory=list)
    #: Delivered before: acknowledge, nothing else happened.
    duplicate: list[str] = Field(default_factory=list)
    #: Not delivered (the session could not take it): leave unacknowledged,
    #: so it is handed out again.
    failed: list[str] = Field(default_factory=list)

    @property
    def acknowledge(self) -> list[str]:
        return [*self.delivered, *self.duplicate]


class DeliveredEventModel(ScopedColumns, Base):
    __tablename__ = "delivered_events"
    __table_args__ = (
        PrimaryKeyConstraint("org", "producer", "project", "event_id", name="pk_delivered_events"),
    )

    event_id: Mapped[str] = mapped_column(String, nullable=False)
    extension_id: Mapped[str] = mapped_column(String, nullable=False)
    #: Unix milliseconds.
    delivered_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class DeliveredEvents:
    """The ids of the events this producer was handed, kept across restarts."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def seen(self, event_id: str) -> bool:
        with self._db.sync_session() as session:
            found = session.execute(
                select(DeliveredEventModel.event_id).where(
                    self._db.scope.where(DeliveredEventModel),
                    DeliveredEventModel.event_id == event_id,
                )
            ).first()
        return found is not None

    def record(self, event: ServiceEvent) -> None:
        with self._db.sync_session() as session, session.begin():
            exists = session.execute(
                select(DeliveredEventModel.event_id).where(
                    self._db.scope.where(DeliveredEventModel),
                    DeliveredEventModel.event_id == event.event_id,
                )
            ).first()
            if exists is None:
                session.add(
                    DeliveredEventModel(
                        **self._db.scope.stamp(),
                        event_id=event.event_id,
                        extension_id=event.extension_id,
                        delivered_at=int(time.time() * 1000),
                    )
                )


class EventPrompt:
    """The text the agent sees for an event the extension said nothing about."""

    #: Payloads are shown whole up to this, then cut.
    MAX_PAYLOAD_CHARS: ClassVar[int] = 4000

    @classmethod
    def of(cls, event: ServiceEvent) -> str:
        payload = json.dumps(event.payload, ensure_ascii=False, sort_keys=True)
        if len(payload) > cls.MAX_PAYLOAD_CHARS:
            payload = payload[: cls.MAX_PAYLOAD_CHARS] + " …(cut)"
        return (
            f"An event arrived from {event.extension_id}'s service: `{event.type}`.\n"
            f"Payload: {payload}\n"
            "Nobody may be watching: act on it if it needs acting on, and say what you did."
        )


#: Asks the extension's process what the agent should see; ``None`` for nothing.
AskExtension = Callable[[ServiceEvent], Awaitable[str | None]]


class EventSession(Protocol):
    """Where an event goes: the producer's main session."""

    def busy(self) -> bool:
        """A run is going in it — working, or waiting on an approval."""
        ...

    async def post(self, *, extension_id: str, text: str, queued: bool) -> None: ...


class ServiceEventInbox:
    """Takes delivered events: once each, through the extension, into the agent's run."""

    def __init__(
        self,
        *,
        delivered: DeliveredEvents,
        session: EventSession,
        ask_extension: AskExtension | None = None,
    ) -> None:
        self._delivered = delivered
        self._session = session
        self._ask = ask_extension

    async def deliver(self, events: list[ServiceEvent]) -> DeliveryReceipt:
        receipt = DeliveryReceipt()
        for event in events:
            if self._delivered.seen(event.event_id):
                receipt.duplicate.append(event.event_id)
                continue
            try:
                text = await self._prompt(event)
                await self._session.post(
                    extension_id=event.extension_id, text=text, queued=self._session.busy()
                )
            except Exception:
                logger.exception("event %s could not be delivered", event.event_id)
                receipt.failed.append(event.event_id)
                continue
            self._delivered.record(event)
            receipt.delivered.append(event.event_id)
        return receipt

    async def _prompt(self, event: ServiceEvent) -> str:
        if self._ask is not None:
            try:
                answered = await self._ask(event)
            except Exception as exc:
                logger.info("%s did not handle event %s: %s", event.extension_id, event.type, exc)
                answered = None
            if answered and answered.strip():
                return answered
        return EventPrompt.of(event)


__all__ = [
    "AskExtension",
    "DeliveredEventModel",
    "DeliveredEvents",
    "DeliveryReceipt",
    "EventPrompt",
    "EventSession",
    "ServiceEvent",
    "ServiceEventInbox",
]
