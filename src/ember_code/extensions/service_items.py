"""What a service sends a producer's agent.

``docs/CLOUD.md`` §6.1 and igni server's service-identity §15. A service
sends **items** — an ``event``, a ``message``, a new ``chat`` or an
``action`` on a chat — through igni server, which hands them to the
producer's igni with the same lease and acknowledgement an event has.

An event is its extension's: igni hands it to the handler and posts
nothing. A message, a chat or an action is the agent's: igni posts it as
the service sent it, and no extension code is involved.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class ItemKind(StrEnum):
    EVENT = "event"
    MESSAGE = "message"
    #: Opens the thread's chat with a title; may carry its first message.
    CHAT = "chat"
    #: Acts on a thread's chat: show a view, rename it, cancel its run.
    ACTION = "action"


class ItemAttachment(BaseModel):
    """A file the service uploaded for the item, as igni server describes it."""

    model_config = ConfigDict(extra="ignore")

    attachment_id: str
    filename: str
    content_type: str = "application/octet-stream"
    size: int = 0
    sha256: str = ""


class ChatAction(BaseModel):
    """An ``action`` item's operation on its thread's chat."""

    model_config = ConfigDict(extra="ignore")

    op: Literal["show", "rename", "cancel"]
    #: ``show``: a json-render spec — shown in the chat, never sent to the model.
    view: dict[str, Any] | None = None
    #: ``rename``: the chat's new title; ``show``: the view's title.
    title: str | None = None


__all__ = ["ChatAction", "ItemAttachment", "ItemKind"]
