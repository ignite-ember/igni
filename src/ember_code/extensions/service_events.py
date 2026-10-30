"""Items a service sends a producer: events for its extension, messages for the agent.

igni server's ``docs/service-identity.md`` §15 and ``docs/CLOUD.md`` §6.
A service sends an item for its own extension and a producer; the
platform hands it to the producer's igni (in the cloud the gateway pulls
it with a lease and acknowledges what igni accepted — delivery is at
least once). Two paths, and nothing in between:

* **An event is the extension's.** It is handed to the extension it is
  for (``ServiceEvent``, a deciding frame on its host socket; the SDK's
  ``@ext.on_service_event(type)``), and igni posts nothing for it. The
  handler acts through explicit host calls — ``host.sessions.create``,
  ``host.sessions.chat(id).post`` — and the agent runs only if it posts.
  An event no handler took — none for its type, the extension not
  running, the handler failing — is **not handled**, and igni server is
  told so (``DeliveryReceipt.not_handled``), so the service reads it.
* **A message, a new chat or an action is the agent's**, and lands
  exactly as the service sent it, in the chat its thread names or in the
  extension's own chat (:class:`~ember_code.extensions.service_landing.Lander`):
  a run starts, or it is queued for the run going — working, or
  **waiting on an approval**, which it never answers. No extension code
  is involved; the extension only sees it, as ``chat.message_added``
  with ``from_service``.

**Once.** An item already delivered — the same ``event_id`` handed out
again because the acknowledgement was lost — is acknowledged and nothing
else: :class:`DeliveredEvents` keeps the ids, in the producer's
database, so a restart does not forget them. An item is recorded only
once its handling is durable — the handler answered, the run started,
the message queued or held — so a crash before then gets it handed out
again. An event not handled is not recorded: handed out again, its
extension is asked again.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, PrimaryKeyConstraint, String, select
from sqlalchemy.orm import Mapped, mapped_column

from ember_code.core.db.base import Base
from ember_code.core.db.database import Database
from ember_code.core.db.scope import ScopedColumns
from ember_code.extensions.service_items import ChatAction, ItemAttachment, ItemKind
from ember_code.extensions.service_landing import Lander

logger = logging.getLogger(__name__)


class ServiceEvent(BaseModel):
    """One item, as the platform hands it out (``/events/pull``): an event unless ``kind`` says otherwise."""

    event_id: str
    extension_id: str
    client_id: str = ""
    kind: ItemKind = ItemKind.EVENT
    #: An event's type, the extension's own word: ``index.ready``. For any
    #: other kind, the kind.
    type: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    #: The service's key for the chat it goes to; none: the extension's own chat.
    thread: str | None = None
    #: A message's text, or a new chat's first message.
    text: str | None = None
    #: The title of the chat it opens.
    title: str | None = None
    #: False: queued for the chat's next run, starting nothing.
    start_run: bool = True
    action: ChatAction | None = None
    attachments: list[ItemAttachment] = Field(default_factory=list)
    event_key: str | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None


class NotHandled(BaseModel):
    """An event no handler took, and why: what its service reads."""

    event_id: str
    reason: str


class EventHandling(BaseModel):
    """What the extension did with an event."""

    handled: bool
    #: Why not, when it was not.
    reason: str = ""


class DeliveryReceipt(BaseModel):
    """What became of a batch: acknowledge ``acknowledge``; report ``not_handled``."""

    delivered: list[str] = Field(default_factory=list)
    #: Delivered before: acknowledge, nothing else happened.
    duplicate: list[str] = Field(default_factory=list)
    #: Not delivered (the session could not take it): leave unacknowledged,
    #: so it is handed out again.
    failed: list[str] = Field(default_factory=list)
    #: Events no handler took: acknowledged — nothing will take them by
    #: being handed out again — and reported as not handled.
    not_handled: list[NotHandled] = Field(default_factory=list)

    @property
    def acknowledge(self) -> list[str]:
        return [*self.delivered, *(n.event_id for n in self.not_handled), *self.duplicate]


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


class UnattendedPost(BaseModel):
    """What became of an extension's message for a session nobody may be watching.

    Step 3 above, which ``host.sessions.chat(id).post`` follows too: it
    started a run, or it was queued for the run already going.
    """

    ok: bool = True
    #: Queued for the run already going, rather than starting one.
    queued: bool = False
    #: Why it was not posted, when ``ok`` is false.
    reason: str = ""


#: Hands an event to the extension it is for; what it did with it.
HandEvent = Callable[[ServiceEvent], Awaitable[EventHandling]]


class ServiceEventInbox:
    """Takes delivered items: once each — an event to its extension, anything else into its chat."""

    def __init__(
        self,
        *,
        delivered: DeliveredEvents,
        lander: Lander,
        hand_event: HandEvent | None = None,
    ) -> None:
        self._delivered = delivered
        self._lander = lander
        self._hand = hand_event

    async def deliver(self, events: list[ServiceEvent]) -> DeliveryReceipt:
        receipt = DeliveryReceipt()
        fresh: list[ServiceEvent] = []
        for event in events:
            if self._delivered.seen(event.event_id) or event.event_id in {
                e.event_id for e in fresh
            }:
                receipt.duplicate.append(event.event_id)
            else:
                fresh.append(event)
        # Every extension handed its events at once: a batch takes as long
        # as its slowest handler, not their sum, and stays inside the
        # gateway's deadline for the delivery. Everything else then lands
        # in turn, so two items of one new thread open one chat between them.
        handlings = await asyncio.gather(*(self._handed(item) for item in fresh))
        for item, handling in zip(fresh, handlings, strict=True):
            if handling is not None and not handling.handled:
                logger.info("event %s was not handled: %s", item.event_id, handling.reason)
                receipt.not_handled.append(
                    NotHandled(event_id=item.event_id, reason=handling.reason)
                )
                continue
            if handling is None:
                try:
                    await self._lander.land(item)
                except Exception:
                    logger.exception("item %s could not be delivered", item.event_id)
                    receipt.failed.append(item.event_id)
                    continue
            self._delivered.record(item)
            receipt.delivered.append(item.event_id)
        return receipt

    async def _handed(self, item: ServiceEvent) -> EventHandling | None:
        """An event's handling; ``None`` for an item that is not an event."""
        if item.kind is not ItemKind.EVENT:
            return None
        if self._hand is None:
            return EventHandling(handled=False, reason="this igni hands events to no extension")
        try:
            return await self._hand(item)
        except Exception as exc:
            return EventHandling(
                handled=False, reason=f"{item.extension_id} was not reached: {exc}"
            )


__all__ = [
    "DeliveredEventModel",
    "DeliveredEvents",
    "DeliveryReceipt",
    "EventHandling",
    "HandEvent",
    "NotHandled",
    "ServiceEvent",
    "ServiceEventInbox",
    "UnattendedPost",
]
