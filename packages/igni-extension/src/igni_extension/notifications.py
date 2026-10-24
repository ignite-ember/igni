"""Telling the person at this machine something: `host.notifications.notify`.

The process is an MCP server talking to the igni host over stdio, so it
tells the host with an MCP notification:

    {"jsonrpc": "2.0", "method": "notifications/igni/notify",
     "params": {"kind": ..., "title": ..., "body": ..., "target": {...}, "event_key": ...}}

The host checks that `target.page` is one of this extension's pages,
keeps it and its read state on the machine (never sending it to igni
server), drops a repeated `event_key`, and shows it beside the
notifications from other people.
"""

from __future__ import annotations

import json
from typing import Final, Literal

from mcp import types
from pydantic import BaseModel, ConfigDict, Field, field_validator

#: The MCP method the host listens for.
NOTIFY_METHOD: Final = "notifications/igni/notify"

#: igni's limits, mirrored so a mistake is caught where it is made.
MAX_KIND = 64
MAX_TITLE = 200
MAX_KEY = 200
MAX_BODY = 1000
MAX_TARGET_PARAMS = 32
MAX_TARGET_BYTES = 2048

ParamValue = str | int | float | bool | None


class Target(BaseModel):
    """Where in the extension to go: one of its pages, and that page's params."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page: str = Field(min_length=1, max_length=MAX_KEY)
    params: dict[str, ParamValue] = Field(default_factory=dict, max_length=MAX_TARGET_PARAMS)

    @field_validator("params")
    @classmethod
    def _small(cls, value: dict[str, ParamValue]) -> dict[str, ParamValue]:
        if len(json.dumps(value)) > MAX_TARGET_BYTES:
            raise ValueError(f"target params may be at most {MAX_TARGET_BYTES} bytes as JSON")
        return value


class LocalNotification(BaseModel):
    """Something to tell the person using this igni. No recipients: it is them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The extension's own word for what happened: `export_ready`.
    kind: str = Field(min_length=1, max_length=MAX_KIND)
    #: What it is about.
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    #: One line. Whitespace is collapsed here.
    body: str = ""
    #: Where opening it goes: one of this extension's pages. Required —
    #: the host shows nothing it cannot open.
    target: Target
    #: The same event is shown once, however often it is sent. Required.
    event_key: str = Field(min_length=1, max_length=MAX_KEY)

    @field_validator("body")
    @classmethod
    def _one_line(cls, value: str) -> str:
        line = " ".join((value or "").split())
        if len(line) > MAX_BODY:
            raise ValueError(f"a body may be at most {MAX_BODY} characters")
        return line


class NotifyParams(types.NotificationParams):
    """`params` of the MCP notification: a `LocalNotification`, field for field."""

    kind: str
    title: str
    body: str = ""
    target: Target
    event_key: str


class NotifyNotification(types.Notification[NotifyParams, Literal["notifications/igni/notify"]]):
    """The MCP notification itself."""

    method: Literal["notifications/igni/notify"] = NOTIFY_METHOD
    params: NotifyParams

    @classmethod
    def of(cls, notification: LocalNotification) -> NotifyNotification:
        return cls(params=NotifyParams.model_validate(notification.model_dump()))

    def local(self) -> LocalNotification:
        return LocalNotification.model_validate(self.params.model_dump(exclude={"meta"}))
