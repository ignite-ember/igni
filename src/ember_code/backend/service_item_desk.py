"""The producer's chats, as a service's items reach them in a running backend.

``docs/CLOUD.md`` §6.1. :class:`~ember_code.extensions.service_landing.Lander`
decides where an item lands; this is what it needs from the session pool:

* posting goes through :class:`~ember_code.backend.service_event_delivery.PoolSession`
  — the one path every unattended post takes (``host.sessions.chat(id).post``
  too): a run starts, or the text is queued for the run going;
* a file is kept by ``BackendServer.upload_attachment``, the composer's
  own upload, in the chat it lands in;
* a new chat is a session attached as a window's ``+ New chat`` is, then
  titled before anything has run in it.

:class:`HeldPostFlusher` posts what was held for a chat whose run was
going, once that run has ended.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ember_code.backend.activity import ActivityState
from ember_code.backend.service_event_delivery import PoolSession
from ember_code.core.activity import background
from ember_code.extensions.service_events import UnattendedPost
from ember_code.extensions.service_threads import HeldPosts
from ember_code.protocol import messages as msg

logger = logging.getLogger(__name__)


class FileNotKept(Exception):
    """The chat would not keep an item's file."""


class ServiceView(BaseModel):
    """A view a service shows in a chat: the ``visualization_delta`` an extension's ``chat.append`` sends."""

    model_config = ConfigDict(populate_by_name=True)

    type: str = Field(default="visualization_delta", frozen=True)
    agent_path: str
    spec_id: str
    spec_json: str = Field(alias="json")
    final: bool = True
    title: str = ""
    source: str


class PoolChatDesk:
    """:class:`~ember_code.extensions.service_landing.ChatDesk` over the session pool."""

    def __init__(self, orchestrator: Any) -> None:
        self._orchestrator = orchestrator

    async def _runtime(self, session_id: str) -> Any:
        return await self._orchestrator.pool.get_or_create(session_id)

    async def open_chat(self, title: str) -> str:
        attached = await self._orchestrator.attach("", None)
        await self.rename(session_id=attached.session_id, title=title)
        return str(attached.session_id)

    def busy(self, session_id: str) -> bool:
        return PoolSession(self._orchestrator, session_id).busy()

    def waiting_on_approval(self, session_id: str) -> bool:
        runtime = self._orchestrator.pool.find(session_id)
        probe = getattr(runtime.backend, "activity", None) if runtime is not None else None
        return probe is not None and probe().state is ActivityState.WAITING

    async def deliver(
        self, *, session_id: str, extension_id: str, from_service: str, text: str
    ) -> UnattendedPost:
        """Posted as the extension's, and marked as its service's: ``chat.message_added``
        carries ``from_service``."""
        return await PoolSession(self._orchestrator, session_id).deliver(
            extension_id=extension_id, text=text, media=None, from_service=from_service
        )

    async def queue(
        self, *, session_id: str, extension_id: str, from_service: str, text: str
    ) -> None:
        await PoolSession(self._orchestrator, session_id).post(
            extension_id=extension_id, text=text, queued=True, from_service=from_service
        )

    async def keep_file(self, *, session_id: str, filename: str, content: bytes) -> str:
        runtime = await self._runtime(session_id)
        kept = runtime.backend.upload_attachment(filename, base64.b64encode(content).decode())
        if kept.error or not kept.path:
            raise FileNotKept(f"{filename}: {kept.error or 'not kept'}")
        return str(kept.path)

    async def show(
        self, *, session_id: str, extension_id: str, view_id: str, view: dict[str, Any], title: str
    ) -> None:
        runtime = await self._runtime(session_id)
        payload = ServiceView(
            agent_path=f"ext:{extension_id}",
            spec_id=view_id,
            spec_json=json.dumps(view),
            title=title,
            source=extension_id,
        ).model_dump(by_alias=True)
        await runtime.transport.send(
            msg.PushNotification(channel="orchestrate_event", payload=payload)
        )
        record = getattr(runtime.backend, "record_event", None)
        if record is not None:
            await record("visualization_delta", payload, "")

    async def rename(self, *, session_id: str, title: str) -> None:
        titled = await PoolSession(self._orchestrator, session_id).title(title)
        if not titled.ok:
            logger.info("chat %s not titled %r: %s", session_id, title, titled.error)

    async def cancel(self, *, session_id: str) -> None:
        runtime = self._orchestrator.pool.find(session_id)
        if runtime is not None:
            runtime.backend.cancel_run()


class HeldPostFlusher:
    """Posts each held item once its chat's run has ended (``extensions/service_threads.py``)."""

    #: How often the held posts are looked at.
    INTERVAL_SECONDS: ClassVar[float] = 2.0

    def __init__(self, *, held: HeldPosts, desk: PoolChatDesk) -> None:
        self._held = held
        self._desk = desk
        self._task: asyncio.Future[None] | None = None

    async def flush(self) -> int:
        """Post every held item whose chat is idle; how many went."""
        posted = 0
        for post in self._held.waiting():
            if self._desk.busy(post.session_id):
                continue
            answer = await self._desk.deliver(
                session_id=post.session_id,
                extension_id=post.extension_id,
                from_service=post.client_id,
                text=post.text,
            )
            if answer.ok:
                self._held.release(post.event_id)
                posted += 1
        return posted

    def start(self) -> None:
        if self._task is None:
            self._task = background.daemon(self._run(), name="held-service-posts")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            try:
                await self.flush()
            except Exception:
                logger.exception("held service posts: not posted this round")
            await asyncio.sleep(self.INTERVAL_SECONDS)


__all__ = ["FileNotKept", "HeldPostFlusher", "PoolChatDesk", "ServiceView"]
