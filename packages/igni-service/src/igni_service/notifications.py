"""Telling other people something, through igni server.

A service says *what happened and who it concerns*; igni works out who
that really is (recipients are resolved to active members of the
organisation, the actor is never told about their own act), stores one
delivery per person, tells each person about an `event_key` once, and
wakes their machines. The service's extension takes no part.

`Notifier` is the wire: it splits what it is given into calls igni
accepts — at most `MAX_BATCH` notifications per call and
`MAX_RECIPIENTS` recipients per notification (a longer list is sent as
several notifications with the same `event_key`, which igni still
delivers once per person) — and retries a call igni could not take.

**Retries are exact only with an `event_key`.** igni drops a repeated
`event_key` per person, so a retry after a reply that was lost on the
way back changes nothing. Without one, the same retry can tell somebody
twice.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from igni_service.errors import IgniUnavailable, NotificationRefused
from igni_service.service_tokens import ServiceTokens
from igni_service.urls import IgniPaths

logger = logging.getLogger(__name__)

#: igni's limits, mirrored so a mistake is caught where it is made.
MAX_KIND = 64
MAX_TITLE = 200
MAX_KEY = 200
MAX_IDENTITY = 320
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


class Notification(BaseModel):
    """One thing that happened, and who it concerns."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The extension's own word for what happened: `comment`, `access_requested`.
    kind: str = Field(min_length=1, max_length=MAX_KIND)
    #: What it is about.
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    #: One line. Whitespace is collapsed here; igni cuts a long one short.
    body: str = ""
    #: Who did it: a user id or an address. Never told about it.
    actor: str = Field(default="", max_length=MAX_IDENTITY)
    #: Where to go: one of this service's extension pages.
    target: Target | None = None
    #: The same event is delivered once per person, however often it is sent.
    event_key: str = Field(default="", max_length=MAX_KEY)
    #: User ids or addresses, in this organisation.
    recipients: list[str] = Field(default_factory=list)

    @field_validator("body")
    @classmethod
    def _one_line(cls, value: str) -> str:
        return " ".join((value or "").split())

    @field_validator("recipients")
    @classmethod
    def _identities(cls, value: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(r.strip() for r in value if r and r.strip()))
        too_long = [r for r in cleaned if len(r) > MAX_IDENTITY]
        if too_long:
            raise ValueError(f"a recipient may be at most {MAX_IDENTITY} characters")
        return cleaned

    def split(self, size: int) -> list[Notification]:
        """This notification with its recipients in groups of at most `size`."""
        return [
            self.model_copy(update={"recipients": self.recipients[start : start + size]})
            for start in range(0, len(self.recipients), size)
        ]


class NotificationBatch(BaseModel):
    """`POST /v1/service-notifications`, as it goes on the wire."""

    notifications: list[Notification]


class Delivered(BaseModel):
    """How many deliveries igni wrote: fewer than named is normal, not an error.

    Somebody who is not an active member, was the actor, or was already
    told about the `event_key` is not delivered to.
    """

    delivered: int = 0


Sleep = Callable[[float], Awaitable[None]]


class Notifier:
    """Sends notifications to igni for one organisation at a time."""

    MAX_BATCH = 100
    MAX_RECIPIENTS = 1000
    #: Calls per batch before giving up, and the first pause between them.
    ATTEMPTS = 3
    BACKOFF = 0.5

    def __init__(
        self,
        tokens: ServiceTokens,
        *,
        attempts: int | None = None,
        backoff: float | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._tokens = tokens
        self._attempts = max(1, attempts if attempts is not None else self.ATTEMPTS)
        self._backoff = self.BACKOFF if backoff is None else backoff
        self._sleep = sleep

    def batches(self, notifications: Iterable[Notification]) -> list[NotificationBatch]:
        """What will be sent, call by call."""
        expanded = [
            part
            for notification in notifications
            if notification.recipients
            for part in notification.split(self.MAX_RECIPIENTS)
        ]
        return [
            NotificationBatch(notifications=expanded[start : start + self.MAX_BATCH])
            for start in range(0, len(expanded), self.MAX_BATCH)
        ]

    async def send(self, org_id: str, notifications: Iterable[Notification]) -> Delivered:
        """Send every notification. Raises `IgniUnavailable` or `NotificationRefused`.

        A notification with no recipients concerns nobody and is not sent.
        """
        delivered = 0
        for batch in self.batches(notifications):
            delivered += await self._send_batch(org_id, batch)
        return Delivered(delivered=delivered)

    async def _send_batch(self, org_id: str, batch: NotificationBatch) -> int:
        body = batch.model_dump(mode="json", exclude_none=True)
        delay = self._backoff
        failure = IgniUnavailable("igni did not take the notifications")
        for attempt in range(1, self._attempts + 1):
            try:
                response = await self._tokens.request(
                    "POST", IgniPaths.NOTIFICATIONS, org_id=org_id, json=body
                )
            except IgniUnavailable as exc:
                failure = exc
            else:
                if response.status_code == 200:
                    try:
                        return Delivered.model_validate(response.json()).delivered
                    except (ValidationError, ValueError) as exc:
                        raise IgniUnavailable(
                            "igni answered the notifications with something else"
                        ) from exc
                if response.status_code != 429 and 400 <= response.status_code < 500:
                    raise NotificationRefused(response.status_code, response.text)
                failure = IgniUnavailable(f"igni answered the notifications {response.status_code}")
            if attempt < self._attempts:
                logger.warning(
                    "igni: %d notification(s) not taken (%s); retrying in %.1fs",
                    len(batch.notifications),
                    failure,
                    delay,
                )
                await self._sleep(delay)
                delay *= 2
        raise failure
