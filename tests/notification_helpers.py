"""igni server's notification routes, played by the tests.

Not a test module: no ``test_`` prefix, so pytest collects nothing.

:class:`FakeNotificationServer` answers the four routes of igni
server's ``docs/service-identity.md`` §10.1 — list, counts, mark read
and the stream — as an ``httpx.MockTransport``, and records every
request so a test can say what the host asked and when. The stream is
one connection at a time, fed frame by frame by the test.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ember_code.extensions.service_token import IgniSession
from ember_code.notifications.server_client import SignedInServer

API_URL = "https://igni.test"
SESSION = "session-token-of-the-person"


def signed_in(session: str | None = SESSION, api_url: str | None = API_URL) -> SignedInServer:
    """A :class:`SignedInServer` over fixed values; ``session=None`` is signed out."""
    return SignedInServer(
        api_url=lambda: api_url,
        session=lambda: None if session is None else IgniSession(access_token=session),
    )


def server_row(
    notification_id: str,
    *,
    source: str = "igni.artifacts",
    minutes_ago: int = 0,
    read: bool = False,
    **changes: Any,
) -> dict[str, Any]:
    created = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    row: dict[str, Any] = {
        "id": notification_id,
        "source": source,
        "kind": "comment",
        "title": "Runbook",
        "body": "Ada: can we drop step 3?",
        "actor": "ada@acme.com",
        "target": {"page": "igni.artifacts", "params": {"slug": "runbook", "comment": "c1"}},
        "created_at": created.isoformat(),
        "delivered_at": created.isoformat(),
        "read_at": created.isoformat() if read else None,
    }
    row.update(changes)
    return row


class FakeNotificationServer:
    """The routes, the rows behind them, and a log of who asked."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows: list[dict[str, Any]] = list(rows or [])
        #: ``(method, path)`` of every request, in order.
        self.requests: list[tuple[str, str]] = []
        #: The ``Last-Event-ID`` each stream connection was opened with.
        self.stream_cursors: list[str | None] = []
        #: The JSON bodies ``POST /v1/notifications/read`` received.
        self.read_bodies: list[dict[str, Any]] = []
        #: Authorization headers seen, to check the session is the person's.
        self.authorizations: list[str] = []
        #: Answer the stream with this status instead of opening it.
        self.stream_status = 200
        #: Answer list and counts with this status.
        self.status = 200
        self._connection: asyncio.Queue[bytes | None] | None = None
        self.connected = asyncio.Event()

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def calls(self, path: str) -> int:
        return sum(1 for _, p in self.requests if p == path)

    # ── Driving the stream ───────────────────────────────────────

    async def wait_connected(self, timeout: float = 2.0) -> None:
        await asyncio.wait_for(self.connected.wait(), timeout)

    async def wait_connections(self, count: int, timeout: float = 2.0) -> None:
        """Until the stream has been opened ``count`` times in all."""

        async def opened() -> None:
            while len(self.stream_cursors) < count or self._connection is None:
                await asyncio.sleep(0.005)

        await asyncio.wait_for(opened(), timeout)

    async def send(self, frame: str) -> None:
        assert self._connection is not None, "no stream connection is open"
        await self._connection.put(frame.encode())

    async def wake(self, cursor: str) -> None:
        await self.send(f"event: waiting\nid: {cursor}\ndata: 1\n\n")

    async def end(self) -> None:
        """Close the open connection from the server's side."""
        assert self._connection is not None
        self.connected.clear()
        await self._connection.put(None)

    # ── The routes ──────────────────────────────────────────────

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        self.authorizations.append(request.headers.get("authorization", ""))
        if path == "/v1/notifications/stream":
            return self._stream(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "nope"})
        if path == "/v1/notifications" and request.method == "GET":
            return httpx.Response(200, json={"notifications": self.rows})
        if path == "/v1/notifications/counts":
            return httpx.Response(200, json=self._counts())
        if path == "/v1/notifications/read" and request.method == "POST":
            body = json.loads(request.content or b"{}")
            self.read_bodies.append(body)
            self._mark(body)
            return httpx.Response(200, json={"marked": 1})
        return httpx.Response(404)

    def _counts(self) -> dict[str, Any]:
        by_source: dict[str, int] = {}
        for row in self.rows:
            if row.get("read_at") is None:
                by_source[row["source"]] = by_source.get(row["source"], 0) + 1
        return {"unread": sum(by_source.values()), "by_source": by_source}

    def _mark(self, body: dict[str, Any]) -> None:
        now = datetime.now(UTC).isoformat()
        for row in self.rows:
            if "ids" in body and row["id"] not in body["ids"]:
                continue
            if "source" in body and row["source"] != body["source"]:
                continue
            row["read_at"] = row.get("read_at") or now

    def _stream(self, request: httpx.Request) -> httpx.Response:
        self.stream_cursors.append(request.headers.get("last-event-id"))
        if self.stream_status != 200:
            return httpx.Response(self.stream_status)
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._connection = queue
        self.connected.set()

        async def frames() -> AsyncIterator[bytes]:
            while True:
                frame = await queue.get()
                if frame is None:
                    return
                yield frame

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=frames())


class RecordingSleep:
    """An ``asyncio.sleep`` that records the delay and then waits for the test.

    Each sleep returns only when :meth:`release` lets one through, so a
    test decides exactly when the follower reconnects — and a follower
    that slept on a timer of its own would be seen doing it.
    """

    def __init__(self) -> None:
        self.delays: list[float] = []
        self._tickets = asyncio.Semaphore(0)

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await self._tickets.acquire()

    def release(self, times: int = 1) -> None:
        for _ in range(times):
            self._tickets.release()


async def settle(rounds: int = 20) -> None:
    """Let scheduled tasks run."""
    for _ in range(rounds):
        await asyncio.sleep(0.005)
