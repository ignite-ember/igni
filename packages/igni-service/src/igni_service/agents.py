"""A producer's agent, as a service reaches it: messages, files, chats (service-identity §15).

    agent = igni.host(org_id).agent(Producer.person("u-marieke"))
    await agent.send_message(
        "Claim CL-20431 arrived; assess the damage.",
        attachments=[Attachment.from_path("damage.jpg"), Attachment.from_bytes("form.pdf", pdf)],
        thread="claim:CL-20431",
    )
    chat = await agent.new_chat("Claim CL-20431", thread="claim:CL-20431")
    await chat.show({"root": "r", "elements": {...}})   # shown, never sent to the model
    status = await agent.status(accepted.event_id)       # pending, delivered, expired, failed

Everything goes through igni server, which stores it until the
producer's igni — on their desktop, or in their Fleet VM, woken for it —
takes it, at least once, for at most 14 days. A **thread** is this
service's own key for a conversation: the first item of a thread opens a
chat for it in the producer's igni, and every later one goes there.
Without one an item goes to this extension's own chat. Nothing a service
sends lands in the chat a person has open, and nothing answers an
approval: that decision stays a person's.

What a service may send beyond a plain event is what its extension
declares in `permissions.service` — `messages`, `attachments`, `chats` —
and igni refuses the rest with `EventRefused` (403).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from igni_service.agent_wire import (
    AgentWire,
    Attachment,
    ChatAction,
    ItemKind,
    ItemStatus,
    ServiceItemRequest,
)
from igni_service.events import EventAccepted, Producer, ServiceEventRequest


class Agent:
    """One producer's agent — a person's or an app's — in one organisation."""

    def __init__(self, org_id: str, producer: Producer, wire: AgentWire) -> None:
        self._org_id = org_id
        self._producer = producer
        self._wire = wire

    @property
    def producer(self) -> Producer:
        return self._producer

    async def send_message(
        self,
        text: str,
        *,
        attachments: list[Attachment] | None = None,
        thread: str | None = None,
        title: str | None = None,
        start_run: bool = True,
        event_key: str | None = None,
        expires_at: datetime | None = None,
    ) -> EventAccepted:
        """Post `text` as this extension's message: it starts a run, or waits for the run going.

        `start_run=False` waits for the chat's next run instead. `title`
        names the chat if this message opens its thread's. Needs
        `service.messages` (and `service.attachments`, `service.chats`
        for files and a thread).
        """
        return await self.send_item(
            "message",
            text=text,
            attachments=attachments,
            thread=thread,
            title=title,
            start_run=start_run,
            event_key=event_key,
            expires_at=expires_at,
        )

    async def send_event(
        self,
        type: str,
        payload: dict[str, object] | None = None,
        *,
        event_key: str | None = None,
        expires_at: datetime | None = None,
    ) -> EventAccepted:
        """Hand this extension an event. igni posts nothing for it: the extension's handler acts.

        One no handler took reads `not_handled` (`status`). Needs no grant.
        """
        event = ServiceEventRequest(
            producer=self._producer,
            type=type,
            payload=payload or {},
            event_key=event_key,
            expires_at=expires_at,
        )
        return await self._wire.event(self._org_id, event)

    async def new_chat(
        self,
        title: str,
        *,
        thread: str | None = None,
        text: str | None = None,
        attachments: list[Attachment] | None = None,
        event_key: str | None = None,
        expires_at: datetime | None = None,
    ) -> Chat:
        """Open a chat titled `title` — for `thread`, or a thread of its own — optionally with a first message."""
        chat = self.chat(thread or f"chat:{uuid.uuid4()}")
        await self.send_item(
            "chat",
            text=text,
            attachments=attachments,
            thread=chat.thread,
            title=title,
            event_key=event_key,
            expires_at=expires_at,
        )
        return chat

    def chat(self, thread: str) -> Chat:
        """The chat of `thread`: opened by its first item, whichever that is."""
        return Chat(self, thread)

    async def status(self, event_id: str) -> ItemStatus:
        """`pending`, `delivered`, `expired`, `dropped` or `failed`, with the reason."""
        return await self._wire.status(self._org_id, event_id)

    async def send_item(
        self,
        kind: ItemKind,
        *,
        text: str | None = None,
        attachments: list[Attachment] | None = None,
        thread: str | None = None,
        title: str | None = None,
        start_run: bool = True,
        action: ChatAction | None = None,
        event_key: str | None = None,
        expires_at: datetime | None = None,
    ) -> EventAccepted:
        """Any item: the methods above and `Chat`'s are this, spelled for one kind."""
        item = ServiceItemRequest(
            producer=self._producer,
            kind=kind,
            thread=thread,
            text=text,
            title=title,
            start_run=start_run,
            action=action,
            attachments=[],
            event_key=event_key,
            expires_at=expires_at,
        )
        ids = await self._wire.uploaded(self._org_id, list(attachments or []))
        return await self._wire.item(self._org_id, item.model_copy(update={"attachments": ids}))


class Chat:
    """The chat of one thread in one producer's igni."""

    def __init__(self, agent: Agent, thread: str) -> None:
        self._agent = agent
        self.thread = thread

    async def send_message(
        self,
        text: str,
        *,
        attachments: list[Attachment] | None = None,
        event_key: str | None = None,
        expires_at: datetime | None = None,
    ) -> EventAccepted:
        """Post it: a run starts, or it waits for the run going."""
        return await self._agent.send_message(
            text,
            attachments=attachments,
            thread=self.thread,
            event_key=event_key,
            expires_at=expires_at,
        )

    async def queue(
        self,
        text: str,
        *,
        attachments: list[Attachment] | None = None,
        event_key: str | None = None,
        expires_at: datetime | None = None,
    ) -> EventAccepted:
        """For the chat's next run; starts nothing."""
        return await self._agent.send_message(
            text,
            attachments=attachments,
            thread=self.thread,
            start_run=False,
            event_key=event_key,
            expires_at=expires_at,
        )

    async def show(
        self, view: dict[str, Any], *, title: str | None = None, event_key: str | None = None
    ) -> EventAccepted:
        """A json-render view in the chat — `{root, elements}` — shown, never sent to the model."""
        return await self._act(ChatAction(op="show", view=view, title=title), event_key)

    async def rename(self, title: str, *, event_key: str | None = None) -> EventAccepted:
        return await self._act(ChatAction(op="rename", title=title), event_key)

    async def cancel(self, *, event_key: str | None = None) -> EventAccepted:
        """Stop the chat's working run. A run waiting on a person's approval is left alone."""
        return await self._act(ChatAction(op="cancel"), event_key)

    async def _act(self, action: ChatAction, event_key: str | None) -> EventAccepted:
        return await self._agent.send_item(
            "action", thread=self.thread, action=action, event_key=event_key
        )


__all__ = ["Agent", "Attachment", "Chat"]
