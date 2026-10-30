"""On a person's desktop, igni fetches their services' events itself.

igni server's service-identity §15. In the cloud the gateway pulls a
producer's events and hands them over on its control stream; on the
desktop igni pulls the person's own, with their session, while it is
online — the same shapes and the same two-minute lease:

* ``POST /v1/me/events/pull`` → ``{"events": [...]}``, oldest first, at
  most :attr:`PersonEventPull.PAGE`;
* each goes through the :class:`~ember_code.extensions.service_events.ServiceEventInbox`
  — once, through its extension, into the agent's run;
* ``POST /v1/me/events/ack`` ``{"event_ids": receipt.acknowledge,
  "not_handled": [{event_id, reason}]}`` — delivered now or before, and
  the events no handler took, which their service reads as
  ``not_handled``. One the session could not take is left to be handed
  out again when its lease runs out.

A full page is followed at once by the next; otherwise igni asks again
after :attr:`PersonEventPull.INTERVAL_SECONDS`. Nobody signed in, or no
server: it waits and asks again. An app's token is refused (403): an
app's events reach it through the gateway, so the pull stops.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, Field, ValidationError

from ember_code.core.activity import background
from ember_code.core.activity.http import busy_call
from ember_code.extensions.service_events import (
    DeliveryReceipt,
    NotHandled,
    ServiceEvent,
    ServiceEventInbox,
)
from ember_code.extensions.service_token import NotSignedIn
from ember_code.notifications.server_client import SignedInServer

logger = logging.getLogger(__name__)


class PulledEvents(BaseModel):
    events: list[ServiceEvent] = Field(default_factory=list)


class Acknowledgement(BaseModel):
    """``POST /v1/me/events/ack``."""

    event_ids: list[str]
    not_handled: list[NotHandled] = Field(default_factory=list)


class PullRefused(Exception):
    """igni server will not hand this caller events (an app's token: 403)."""


class PullFailed(Exception):
    """igni server could not be reached, or answered in a shape not understood."""


class PersonEventPull:
    """Pulls the signed-in person's events, delivers them, acknowledges what was taken."""

    PULL_PATH: ClassVar[str] = "/v1/me/events/pull"
    ACK_PATH: ClassVar[str] = "/v1/me/events/ack"
    #: igni server hands out at most this many at once.
    PAGE: ClassVar[int] = 100
    #: Between pulls when the last one was not a full page.
    INTERVAL_SECONDS: ClassVar[float] = 60.0
    TIMEOUT_SECONDS: ClassVar[float] = 15.0

    def __init__(
        self,
        *,
        server: SignedInServer,
        inbox: ServiceEventInbox,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._server = server
        self._inbox = inbox
        self._transport = transport
        self._task: asyncio.Future[None] | None = None

    async def once(self) -> tuple[int, DeliveryReceipt]:
        """One pull: ``(events pulled, what became of them)``.

        Raises:
            NotSignedIn: nobody is signed in, or no server is configured.
            PullRefused: this caller gets no events.
            PullFailed: igni server could not be reached.
        """
        answer = await self._call(self.PULL_PATH, {})
        try:
            pulled = PulledEvents.model_validate_json(answer.content)
        except ValidationError as exc:
            raise PullFailed(f"the events are malformed: {exc}") from exc
        receipt = await self._inbox.deliver(pulled.events)
        if receipt.acknowledge:
            await self._call(
                self.ACK_PATH,
                Acknowledgement(
                    event_ids=receipt.acknowledge, not_handled=receipt.not_handled
                ).model_dump(),
            )
        return len(pulled.events), receipt

    def start(self) -> None:
        if self._task is None:
            self._task = background.daemon(self._run(), name="event-pull")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            try:
                pulled, _ = await self.once()
            except PullRefused as exc:
                logger.info("events: igni server hands this caller none (%s); not pulling", exc)
                return
            except (NotSignedIn, PullFailed) as exc:
                logger.debug("events: not pulled now: %s", exc)
                pulled = 0
            if pulled < self.PAGE:
                await asyncio.sleep(self.INTERVAL_SECONDS)

    async def _call(self, path: str, body: dict[str, Any]) -> httpx.Response:
        api_url, session = self._server.current()
        try:
            async with (
                busy_call("igni-server", "service events"),
                httpx.AsyncClient(
                    timeout=self.TIMEOUT_SECONDS, transport=self._transport
                ) as client,
            ):
                response = await client.post(
                    f"{api_url}{path}",
                    json=body,
                    headers={"Authorization": f"Bearer {session.access_token}"},
                )
        except httpx.HTTPError as exc:
            raise PullFailed(f"igni server could not be reached: {exc}") from exc
        if response.status_code == 403:
            raise PullRefused(f"{path} answered 403")
        if response.status_code != 200:
            raise PullFailed(f"{path} answered {response.status_code}")
        return response


__all__ = ["Acknowledgement", "PersonEventPull", "PullFailed", "PullRefused", "PulledEvents"]
