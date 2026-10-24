"""Following ``GET /v1/notifications/stream``: igni server saying "something changed".

igni server's ``docs/service-identity.md`` §10.1. The stream carries a
cursor and nothing else. Every event means "go and read the list
again", and the host does exactly that; it never builds state out of
what the stream said. So a dropped connection costs latency, not
correctness — which is why the follower is allowed to be simple.

**Nothing is asked on a timer.** The list is read at start-up and on
sign-in (by :class:`~ember_code.notifications.center.NotificationCenter`,
whether or not the stream can be opened), when the stream wakes the
host, on every reconnect (anything may have happened while nobody was
listening), and when the person asks. Between those, an idle stream
costs a keepalive line every few seconds and no request at all.

**Reconnecting.** A connection that ends is reopened with
``Last-Event-ID`` set to the last cursor seen, after a delay that
doubles from :attr:`Backoff.base` to :attr:`Backoff.ceiling` while
connections keep failing or ending quickly, and drops back once one
has stayed up. A ``retry:`` field from the server is honoured as the
shortest delay. The server ending a connection that had stayed up on purpose (its
``reconnect`` event) is answered promptly rather than as a failure.

**Stopping.** Refused (401, 403) or not there (404), the follower stops
until somebody signs in again: retrying a session igni does not accept
would be polling with extra steps. Signed out, it stops too.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum

import httpx
from pydantic import BaseModel

from ember_code.extensions.service_token import NotSignedIn
from ember_code.notifications.server_client import SignedInServer

logger = logging.getLogger(__name__)


class ServerSentEvent(BaseModel):
    """One dispatched event."""

    event: str = "message"
    data: str = ""
    id: str | None = None
    retry_ms: int | None = None


class SseParser:
    """The ``text/event-stream`` format, a line at a time (WHATWG HTML §9.2)."""

    def __init__(self) -> None:
        self._event = ""
        self._data: list[str] = []
        self._id: str | None = None
        self._retry: int | None = None

    def feed(self, line: str) -> ServerSentEvent | None:
        """Take one line (without its terminator). Returns an event when a
        blank line completes one; ``None`` otherwise."""
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return None  # a comment: keepalives look like this
        name, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if name == "event":
            self._event = value
        elif name == "data":
            self._data.append(value)
        elif name == "id" and "\0" not in value:
            self._id = value
        elif name == "retry" and value.isdigit():
            self._retry = int(value)
        return None

    def _dispatch(self) -> ServerSentEvent | None:
        event = ServerSentEvent(
            event=self._event or "message",
            data="\n".join(self._data),
            id=self._id,
            retry_ms=self._retry,
        )
        had_data = bool(self._data)
        had_fields = had_data or self._id is not None or self._retry is not None
        self._event, self._data, self._id, self._retry = "", [], None, None
        # An event with no data is not dispatched, but its id and retry
        # still count — so it is returned for those and marked empty.
        return event if had_fields else None


class Backoff:
    """Doubling delays with jitter, from ``base`` to ``ceiling`` seconds."""

    def __init__(
        self,
        *,
        base: float = 1.0,
        ceiling: float = 60.0,
        jitter: float = 0.2,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.base = base
        self.ceiling = ceiling
        self._jitter = jitter
        self._rng = rng
        self._failures = 0

    def next(self) -> float:
        delay = min(self.ceiling, self.base * (2**self._failures))
        self._failures += 1
        spread = delay * self._jitter
        return max(0.0, delay - spread + 2 * spread * self._rng())

    def reset(self) -> None:
        self._failures = 0


class StreamOutcome(StrEnum):
    """How one connection ended."""

    #: The server ended it on purpose and asked to be called back.
    RECONNECT = "reconnect"
    #: It ended, or broke, or could not be opened.
    DROPPED = "dropped"
    #: igni refused the session or has no stream: stop until sign-in.
    REFUSED = "refused"
    #: Nobody is signed in.
    SIGNED_OUT = "signed_out"


class NotificationStreamFollower:
    """Keeps one connection to the stream open while somebody is signed in."""

    PATH = "/v1/notifications/stream"
    #: The server's ``reconnect`` event: "this connection is spent".
    RECONNECT_EVENT = "reconnect"
    #: A connection that stayed up this long resets the backoff.
    STABLE_SECONDS = 30.0
    #: Silence longer than this means a dead connection. igni server
    #: sends a keepalive every few seconds, so this is never reached on
    #: a healthy one.
    READ_TIMEOUT = 60.0

    def __init__(
        self,
        server: SignedInServer,
        *,
        on_reconnect: Callable[[], None],
        on_wake: Callable[[], None],
        transport: httpx.AsyncBaseTransport | None = None,
        backoff: Backoff | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        Args:
            on_reconnect: A connection opened again, after the first since
                :meth:`start`. Anything may have changed while nobody was
                listening, so the list is read again. (The first open
                needs no read of its own: whoever started following read
                the list as they did.)
            on_wake: An event arrived: something changed.
        """
        self._server = server
        self._on_reconnect = on_reconnect
        #: Connections opened since :meth:`start`.
        self._opens = 0
        self._on_wake = on_wake
        self._transport = transport
        self._backoff = backoff or Backoff()
        self._sleep = sleep
        self._clock = clock
        self._cursor: str | None = None
        self._retry_seconds: float | None = None
        #: Whether the last connection stayed up long enough to count
        #: as working. A server that ends every connection at once is
        #: backed off from like one that refuses them.
        self._was_stable = False
        self._task: asyncio.Task[None] | None = None

    @property
    def cursor(self) -> str | None:
        """The last cursor seen, sent as ``Last-Event-ID`` on reconnect."""
        return self._cursor

    @property
    def following(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Follow, unless already following."""
        if self.following:
            return
        self._opens = 0
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def restart(self) -> None:
        """Follow afresh — after a sign-in, as whoever is signed in now.

        The cursor is forgotten: it was the last person's.
        """
        await self.stop()
        self._cursor = None
        self._backoff.reset()
        self.start()

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        while True:
            outcome = await self._follow_once()
            if outcome in (StreamOutcome.REFUSED, StreamOutcome.SIGNED_OUT):
                logger.info("notification stream stopped: %s", outcome.value)
                return
            if outcome is StreamOutcome.RECONNECT and self._was_stable:
                delay = self._retry_seconds if self._retry_seconds is not None else 0.0
            else:
                delay = self._backoff.next()
                if self._retry_seconds is not None:
                    delay = max(delay, self._retry_seconds)
            await self._sleep(delay)

    async def _follow_once(self) -> StreamOutcome:
        try:
            api_url, session = self._server.current()
        except NotSignedIn:
            return StreamOutcome.SIGNED_OUT
        headers = {
            "Authorization": f"Bearer {session.access_token}",
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        }
        if self._cursor is not None:
            headers["Last-Event-ID"] = self._cursor
        connected_with = self._cursor
        opened_at: float | None = None
        timeout = httpx.Timeout(15.0, read=self.READ_TIMEOUT)
        try:
            async with (
                httpx.AsyncClient(timeout=timeout, transport=self._transport) as client,
                client.stream("GET", f"{api_url}{self.PATH}", headers=headers) as response,
            ):
                if response.status_code in (401, 403, 404):
                    logger.warning(
                        "igni refused the notification stream (%s)", response.status_code
                    )
                    return StreamOutcome.REFUSED
                if response.status_code != 200:
                    logger.info("notification stream answered %s", response.status_code)
                    return StreamOutcome.DROPPED
                opened_at = self._clock()
                if self._opens:
                    self._on_reconnect()
                self._opens += 1
                parser = SseParser()
                async for line in response.aiter_lines():
                    event = parser.feed(line)
                    if event is None:
                        continue
                    if event.retry_ms is not None:
                        self._retry_seconds = event.retry_ms / 1000
                    if event.id is not None:
                        self._cursor = event.id
                    if event.event == self.RECONNECT_EVENT:
                        return StreamOutcome.RECONNECT
                    if not event.data:
                        continue
                    # igni server opens every connection with an event
                    # repeating the cursor it was given ("" for none). The
                    # list was read as following started or resumed, so
                    # an event at that same cursor says nothing newer.
                    if event.id is not None and event.id == (connected_with or ""):
                        continue
                    self._on_wake()
        except httpx.HTTPError as exc:
            logger.info("notification stream dropped: %s", exc)
        except Exception:  # noqa: BLE001 — following must outlive one bad connection
            logger.exception("notification stream failed")
        finally:
            self._was_stable = (
                opened_at is not None and self._clock() - opened_at >= self.STABLE_SECONDS
            )
            if self._was_stable:
                self._backoff.reset()
        return StreamOutcome.DROPPED
