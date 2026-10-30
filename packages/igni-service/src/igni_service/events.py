"""Waking a producer's agent, through igni server (service-identity §15).

A service names *who the event is for* — a person or an app, its
producer — and *what happened*. igni stores the event and hands it to
this service's extension in that producer's igni (on their desktop, or in
their Fleet VM, woken for it). **igni posts nothing for it**: the
extension's `@ext.on_service_event` handler acts through its own host
calls, and the agent runs only if it posts. An event no handler took
reads `not_handled`. To put something in front of the agent directly,
send a message instead (`igni_service.agents`).

`EventSender` is the wire: one item per call, retried when igni could
not take it. **Retries are exact only with an `event_key`**: igni stores
one item per key and producer, so a retry after a lost reply is
answered with the item already stored.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from igni_service.errors import EventRefused, IgniUnavailable
from igni_service.service_tokens import ServiceTokens
from igni_service.urls import IgniPaths

logger = logging.getLogger(__name__)

#: igni's limits, mirrored so a mistake is caught where it is made.
MAX_TYPE = 100
MAX_KEY = 200
MAX_PAYLOAD_BYTES = 65536

ProducerKind = Literal["person", "app"]


class Producer(BaseModel):
    """Whose agent the event is for: a person or an app of the organisation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ProducerKind
    id: str = Field(min_length=1)

    @classmethod
    def person(cls, user_id: str) -> Producer:
        return cls(kind="person", id=user_id)

    @classmethod
    def app(cls, app_id: str) -> Producer:
        return cls(kind="app", id=app_id)


class ServiceEventRequest(BaseModel):
    """`POST /v1/service-events`, as it goes on the wire."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    producer: Producer
    #: The extension's own word for what happened: `claim.created`.
    type: str = Field(min_length=1, max_length=MAX_TYPE)
    payload: dict[str, object] = Field(default_factory=dict)
    event_key: str | None = Field(default=None, min_length=1, max_length=MAX_KEY)
    #: Not delivered after this; igni keeps at most 14 days.
    expires_at: datetime | None = None

    @field_validator("payload")
    @classmethod
    def _small(cls, value: dict[str, object]) -> dict[str, object]:
        if len(json.dumps(value, default=str)) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"an event payload may be at most {MAX_PAYLOAD_BYTES} bytes as JSON")
        return value


class EventAccepted(BaseModel):
    """igni stored the event. `expires_at` is the time igni kept."""

    event_id: str
    expires_at: datetime | None = None
    #: True when `event_key` matched an event already stored.
    duplicate: bool = False


Sleep = Callable[[float], Awaitable[None]]


class EventSender:
    """Sends events to igni for one organisation at a time."""

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

    async def send(self, org_id: str, event: ServiceEventRequest) -> EventAccepted:
        """Send one event. Raises `IgniUnavailable` or `EventRefused`."""
        return await self.post(org_id, IgniPaths.SERVICE_EVENTS, event, what=event.type)

    async def post(self, org_id: str, path: str, item: BaseModel, *, what: str) -> EventAccepted:
        """Send one item to `path`. Raises `IgniUnavailable` or `EventRefused`."""
        body = item.model_dump(mode="json", exclude_none=True)
        if body.get("attachments") == []:
            body.pop("attachments")
        delay = self._backoff
        failure = IgniUnavailable("igni did not take the event")
        for attempt in range(1, self._attempts + 1):
            try:
                response = await self._tokens.request("POST", path, org_id=org_id, json=body)
            except IgniUnavailable as exc:
                failure = exc
            else:
                if response.status_code in (200, 202):
                    try:
                        return EventAccepted.model_validate(response.json())
                    except (ValidationError, ValueError) as exc:
                        raise IgniUnavailable(
                            "igni answered the event with something else"
                        ) from exc
                if response.status_code != 429 and 400 <= response.status_code < 500:
                    raise EventRefused(response.status_code, response.text)
                failure = IgniUnavailable(f"igni answered the event {response.status_code}")
            if attempt < self._attempts:
                logger.warning("igni: %s not taken (%s); retrying in %.1fs", what, failure, delay)
                await self._sleep(delay)
                delay *= 2
        raise failure
