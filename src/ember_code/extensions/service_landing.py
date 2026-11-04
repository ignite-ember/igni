"""Where a service's message lands, and how: its thread's chat, or the extension's own chat.

``docs/CLOUD.md`` §6.1. A message, a new chat or an action lands exactly
as the service sent it — no extension code is involved; the extension
sees the post as ``chat.message_added`` with ``from_service``, and
nothing more. For one item:

1. **The chat.** The item's thread's; else the extension's own chat. A
   thread seen for the first time opens a new chat, titled by the
   service, else ``"<extension>: <thread>"`` — the extension's own chat
   by the extension's id — and the map (:class:`ServiceThreads`) keeps
   it. Never the chat that happens to be open.
2. **The files.** The item's attachments, fetched and kept in that chat
   by the path a person's upload takes, and named in the text as the
   composer names them (``@<ref>``), so the prompt builder treats them
   exactly as it treats a person's.
3. **The post.** Start a run, or queue it for the run going (working, or
   waiting on an approval — which it never answers): :class:`ChatDesk`
   ``deliver``, the rules every unattended post follows. With
   ``start_run`` false it waits for the chat's next run. With attachments
   and a run going it is **held** until that run ends
   (:class:`~ember_code.extensions.service_threads.HeldPosts`) rather
   than queued into it, where its files would reach the model as names.

An ``action`` acts on its thread's chat — ``show`` a view, ``rename`` it,
``cancel`` its run, never a run waiting on an approval, which would
decide the approval.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from ember_code.extensions.service_attachments import AttachmentSource, AttachmentUnavailable
from ember_code.extensions.service_items import ChatAction, ItemAttachment, ItemKind
from ember_code.extensions.service_threads import HeldPost, HeldPosts, ServiceThreads, ThreadChat

logger = logging.getLogger(__name__)


class PostRefused(Exception):
    """The chat would not take the post: the item is handed out again."""


class Posted(Protocol):
    ok: bool
    reason: str


class ChatDesk(Protocol):
    """The producer's chats, as an item reaches them (the backend's ``PoolChatDesk``)."""

    async def open_chat(self, title: str) -> str:
        """A new chat with this title; its session id."""
        ...

    def busy(self, session_id: str) -> bool:
        """A run is going in it — working, or waiting on an approval."""
        ...

    def waiting_on_approval(self, session_id: str) -> bool: ...

    async def deliver(
        self, *, session_id: str, extension_id: str, from_service: str, text: str
    ) -> Posted:
        """Start a run with it, or queue it for the run going; marked as the service's."""
        ...

    async def queue(
        self, *, session_id: str, extension_id: str, from_service: str, text: str
    ) -> None:
        """For the chat's next run; starts nothing."""
        ...

    async def keep_file(self, *, session_id: str, filename: str, content: bytes) -> str:
        """Keep a file in the chat as a person's upload is kept; the reference to name it by."""
        ...

    async def show(
        self, *, session_id: str, extension_id: str, view_id: str, view: dict[str, Any], title: str
    ) -> None:
        """A view in the chat, shown and never sent to the model."""
        ...

    async def rename(self, *, session_id: str, title: str) -> None: ...

    async def cancel(self, *, session_id: str) -> None: ...


class LandingItem(Protocol):
    """What :class:`Lander` reads of an item (``ServiceEvent``)."""

    event_id: str
    extension_id: str
    #: The service that sent it.
    client_id: str
    kind: ItemKind
    thread: str | None
    text: str | None
    title: str | None
    start_run: bool
    action: ChatAction | None
    attachments: list[ItemAttachment]


class Lander:
    """Lands one item in the producer's chats."""

    def __init__(
        self,
        *,
        desk: ChatDesk,
        threads: ServiceThreads,
        held: HeldPosts,
        files: AttachmentSource | None = None,
    ) -> None:
        self._desk = desk
        self._threads = threads
        self._held = held
        self._files = files

    async def land(self, item: LandingItem) -> None:
        """Land it as it was sent. Raises when it could not."""
        if item.kind is ItemKind.EVENT:
            raise ValueError("an event is its extension's: it does not land in a chat")
        if item.kind is ItemKind.ACTION:
            await self._act(item)
            return
        session_id = (await self._open(item.extension_id, item.thread or "", item.title)).session_id
        if item.kind is ItemKind.CHAT and not item.text:
            return
        refs = [
            await self._desk.keep_file(session_id=session_id, filename=name, content=content)
            for name, content in await self._files_of(item)
        ]
        body = Mentions.append(item.text or "", refs)
        if not item.start_run:
            await self._desk.queue(
                session_id=session_id,
                extension_id=item.extension_id,
                from_service=item.client_id,
                text=body,
            )
        elif refs and self._desk.busy(session_id):
            self._held.hold(
                HeldPost(
                    event_id=item.event_id,
                    session_id=session_id,
                    extension_id=item.extension_id,
                    client_id=item.client_id,
                    text=body,
                )
            )
        else:
            posted = await self._desk.deliver(
                session_id=session_id,
                extension_id=item.extension_id,
                from_service=item.client_id,
                text=body,
            )
            if not posted.ok:
                raise PostRefused(posted.reason)

    async def _open(self, extension_id: str, thread: str, title: str | None) -> ThreadChat:
        """The thread's chat, opened now if it is new."""
        found = self._threads.find(extension_id, thread)
        if found is not None:
            return found
        title = title or (f"{extension_id}: {thread}" if thread else extension_id)
        chat = ThreadChat(
            extension_id=extension_id,
            thread=thread,
            session_id=await self._desk.open_chat(title),
            title=title,
        )
        self._threads.bind(chat)
        return chat

    async def _files_of(self, item: LandingItem) -> list[tuple[str, bytes]]:
        """``(filename, bytes)`` of every file the item names."""
        if item.attachments and self._files is None:
            raise AttachmentUnavailable("this igni cannot fetch an item's files")
        return [
            (attachment.filename, await self._files.fetch(item.event_id, attachment))  # type: ignore[union-attr]
            for attachment in item.attachments
        ]

    async def _act(self, item: LandingItem) -> None:
        action = item.action
        if action is None:
            return
        thread = item.thread or ""
        found = self._threads.find(item.extension_id, thread)
        if action.op == "cancel":
            if found is None or not self._desk.busy(found.session_id):
                return
            if self._desk.waiting_on_approval(found.session_id):
                logger.info("item %s: the run waits on an approval; not cancelled", item.event_id)
                return
            await self._desk.cancel(session_id=found.session_id)
        elif action.op == "rename":
            title = action.title or ""
            if found is None:
                await self._open(item.extension_id, thread, title)
                return
            await self._desk.rename(session_id=found.session_id, title=title)
            self._threads.retitle(item.extension_id, thread, title)
        else:
            chat = found or await self._open(item.extension_id, thread, None)
            await self._desk.show(
                session_id=chat.session_id,
                extension_id=item.extension_id,
                view_id=f"svc-{item.event_id}",
                view=action.view or {},
                title=action.title or "",
            )


class Mentions:
    """Files in a message, named as the composer names an upload: ``@<ref>``."""

    @staticmethod
    def append(text: str, refs: list[str]) -> str:
        if not refs:
            return text
        return f"{text}\n\n" + " ".join(f"@{ref}" for ref in refs)


__all__ = ["ChatDesk", "Lander", "LandingItem", "Mentions", "PostRefused", "Posted"]
