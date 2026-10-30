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
import re
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


class Target(BaseModel):
    """Where in the extension to go: one of its pages, and that page's params.

    `params` are strings, as a page's parameters are everywhere in igni
    (`NotificationTarget` in the host schema, a page's route): a page
    reads them from its location, where everything is a string.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    page: str = Field(min_length=1, max_length=MAX_KEY)
    params: dict[str, str] = Field(default_factory=dict, max_length=MAX_TARGET_PARAMS)

    @field_validator("params")
    @classmethod
    def _small(cls, value: dict[str, str]) -> dict[str, str]:
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


#: The MCP method an extension says which of its conditions hold with.
CONDITIONS_METHOD: Final = "notifications/igni/conditions"

#: How many conditions one message may carry — igni's limit.
MAX_CONDITIONS = 64

#: A condition's name, as a manifest's `when` spells it.
CONDITION = r"^[a-z][a-z0-9-]*$"


class ConditionsParams(types.NotificationParams):
    """`params` of `notifications/igni/conditions`: `{condition: holds}`.

    `refresh` asks igni to fetch this extension's `source: tool` prompt
    texts again — they describe something that changed (a new commit's
    project map), not something that comes and goes.
    """

    conditions: dict[str, bool] = Field(max_length=MAX_CONDITIONS)
    #: Present only when asking; absent, it is just conditions.
    refresh: bool | None = None

    @field_validator("conditions")
    @classmethod
    def _named_as_a_manifest_names_them(cls, value: dict[str, bool]) -> dict[str, bool]:
        for name in value:
            if not re.fullmatch(CONDITION, name):
                raise ValueError(f"{name!r} is not a condition name a manifest's `when` can use")
        return value


class ConditionsNotification(
    types.Notification[ConditionsParams, Literal["notifications/igni/conditions"]]
):
    """Which of this extension's conditions hold now."""

    method: Literal["notifications/igni/conditions"] = CONDITIONS_METHOD
    params: ConditionsParams
