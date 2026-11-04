"""The producer's chats as a service's items reach them, for the delivery tests.

Not a test module. :class:`Desk` stands in for the backend's
``PoolChatDesk``: a chat on screen the person has open (``"open"``), and
every chat an item opens, with what was posted, queued, kept, shown,
renamed and cancelled in each. :class:`Inbox` builds the real inbox and
lander over a real database — the thread map, the held posts and the
delivered ids are the real stores.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ember_code.core.db.database import Database
from ember_code.core.db.urls import DatabaseUrl
from ember_code.extensions.service_events import (
    DeliveredEvents,
    HandEvent,
    ServiceEvent,
    ServiceEventInbox,
    UnattendedPost,
)
from ember_code.extensions.service_items import ItemAttachment
from ember_code.extensions.service_landing import Lander
from ember_code.extensions.service_threads import HeldPosts, ServiceThreads
from tests.storage_backends import Scopes

#: The chat a person has open: nothing a service sends may land there.
OPEN = "open"


class Post(BaseModel):
    session_id: str
    extension_id: str
    #: The service that sent it.
    from_service: str
    text: str
    #: Queued for the run going rather than starting one.
    queued: bool
    #: Queued for the chat's next run, starting nothing (``start_run`` false).
    next_run: bool = False


class Desk:
    def __init__(self) -> None:
        self.titles: dict[str, str] = {OPEN: "what the person is doing"}
        self.running: set[str] = set()
        self.waiting: set[str] = set()
        self.posts: list[Post] = []
        self.files: dict[str, bytes] = {}
        self.views: list[tuple[str, str, dict[str, Any]]] = []
        self.cancelled: list[str] = []
        self.refuse = False

    async def open_chat(self, title: str) -> str:
        session_id = f"s{len(self.titles)}"
        self.titles[session_id] = title
        return session_id

    def busy(self, session_id: str) -> bool:
        return session_id in self.running or session_id in self.waiting

    def waiting_on_approval(self, session_id: str) -> bool:
        return session_id in self.waiting

    async def deliver(
        self, *, session_id: str, extension_id: str, from_service: str, text: str
    ) -> UnattendedPost:
        if self.refuse:
            return UnattendedPost(ok=False, reason="the session is gone")
        queued = self.busy(session_id)
        self.posts.append(
            Post(
                session_id=session_id,
                extension_id=extension_id,
                from_service=from_service,
                text=text,
                queued=queued,
            )
        )
        return UnattendedPost(queued=queued)

    async def queue(
        self, *, session_id: str, extension_id: str, from_service: str, text: str
    ) -> None:
        self.posts.append(
            Post(
                session_id=session_id,
                extension_id=extension_id,
                from_service=from_service,
                text=text,
                queued=True,
                next_run=True,
            )
        )

    async def keep_file(self, *, session_id: str, filename: str, content: bytes) -> str:
        ref = f"/project/.igni/attachments/{session_id}/{filename}"
        self.files[ref] = content
        return ref

    async def show(
        self, *, session_id: str, extension_id: str, view_id: str, view: dict[str, Any], title: str
    ) -> None:
        self.views.append((session_id, view_id, view))

    async def rename(self, *, session_id: str, title: str) -> None:
        self.titles[session_id] = title

    async def cancel(self, *, session_id: str) -> None:
        self.cancelled.append(session_id)

    def in_chat(self, session_id: str) -> list[str]:
        return [p.text for p in self.posts if p.session_id == session_id]


class Files:
    """igni server's copies of the items' files."""

    def __init__(self, **held: bytes) -> None:
        self.held = held
        self.fetched: list[tuple[str, str]] = []

    async def fetch(self, event_id: str, attachment: ItemAttachment) -> bytes:
        self.fetched.append((event_id, attachment.attachment_id))
        return self.held[attachment.attachment_id]


class Inbox:
    """The real inbox and lander, on one producer's database."""

    def __init__(
        self,
        url: DatabaseUrl,
        *,
        desk: Desk | None = None,
        files: Files | None = None,
        hand: HandEvent | None = None,
    ) -> None:
        self.db = Database(url, scope=Scopes.fresh())
        self.desk = desk or Desk()
        self.threads = ServiceThreads(self.db)
        self.held = HeldPosts(self.db)
        self.files = files or Files()
        self.inbox = ServiceEventInbox(
            delivered=DeliveredEvents(self.db),
            lander=Lander(desk=self.desk, threads=self.threads, held=self.held, files=self.files),
            hand_event=hand,
        )

    async def deliver(self, *items: ServiceEvent) -> Any:
        return await self.inbox.deliver(list(items))

    def restarted(self, hand: HandEvent | None = None) -> ServiceEventInbox:
        """The same producer's inbox after a restart: the same stores, read afresh."""
        return ServiceEventInbox(
            delivered=DeliveredEvents(self.db),
            lander=Lander(
                desk=self.desk,
                threads=ServiceThreads(self.db),
                held=HeldPosts(self.db),
                files=self.files,
            ),
            hand_event=hand,
        )


def item(event_id: str = "evt-1", **fields: Any) -> ServiceEvent:
    """An event of ``acme.claims`` unless ``fields`` say otherwise."""
    base: dict[str, Any] = {
        "event_id": event_id,
        "extension_id": "acme.claims",
        "client_id": "svc-claims",
        "type": "claim.created",
        "payload": {"claim": "CL-20431"},
    }
    return ServiceEvent.model_validate({**base, **fields})


def message(event_id: str = "msg-1", **fields: Any) -> ServiceEvent:
    base: dict[str, Any] = {"kind": "message", "type": "message", "payload": {}}
    return item(event_id, **{**base, **fields})


__all__ = ["Desk", "Files", "Inbox", "OPEN", "Post", "item", "message"]
