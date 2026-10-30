"""igni server's notification stream wakes the host; nothing polls.

igni server's ``docs/service-identity.md`` §10.1. The stream carries a
cursor and nothing else: every event means "read the list again". The
host reads it when a connection opens (start-up, sign-in, reconnect)
and when an event arrives — and at no other time. These tests drive a
fake igni server frame by frame and count what the host asked for.

The sleep the follower waits on between connections is the test's: it
records the delay and returns only when the test lets it, so a follower
with a timer of its own would show up here as a sleep nobody released.
"""

from __future__ import annotations

import asyncio

import pytest

from ember_code.notifications.center import NotificationCenter
from ember_code.notifications.local_store import LocalNotificationStore
from ember_code.notifications.schemas import NotificationCounts
from ember_code.notifications.stream import (
    Backoff,
    NotificationStreamFollower,
    SseParser,
)
from tests.notification_helpers import (
    FakeNotificationServer,
    RecordingSleep,
    server_row,
    settle,
    signed_in,
)

LIST = "/v1/notifications"
COUNTS = "/v1/notifications/counts"
STREAM = "/v1/notifications/stream"


class Host:
    """A center following a fake igni server, with a sleep the test holds."""

    def __init__(self, tmp_path, server: FakeNotificationServer, *, session="s") -> None:
        self.server = server
        self.sleep = RecordingSleep()
        self.announced: list[NotificationCounts] = []
        self.center = NotificationCenter(
            server=signed_in(session=session),
            local=LocalNotificationStore(tmp_path / "notifications.db"),
            announce=self.announced.append,
            transport=server.transport,
            backoff=Backoff(base=1.0, ceiling=8.0, rng=lambda: 0.5),
            sleep=self.sleep,
        )

    async def __aenter__(self) -> Host:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.center.stop()


@pytest.fixture
def server() -> FakeNotificationServer:
    return FakeNotificationServer([server_row("n1")])


# ── The format ──────────────────────────────────────────────────────


class TestTheEventStreamFormat:
    def test_an_event_is_dispatched_by_a_blank_line(self):
        parser = SseParser()
        assert parser.feed("event: waiting") is None
        assert parser.feed("id: c7") is None
        assert parser.feed("data: 1") is None
        event = parser.feed("")
        assert event is not None
        assert (event.event, event.id, event.data) == ("waiting", "c7", "1")

    def test_a_keepalive_is_a_comment_and_says_nothing(self):
        parser = SseParser()
        assert parser.feed(": keepalive") is None
        assert parser.feed("") is None

    def test_retry_and_multi_line_data(self):
        parser = SseParser()
        for line in ("retry: 2500", "data: a", "data:b"):
            parser.feed(line)
        event = parser.feed("")
        assert event is not None
        assert event.retry_ms == 2500
        assert event.data == "a\nb"


# ── Opening reads the list; an idle stream asks nothing ─────────────


async def test_opening_the_stream_reads_the_list_once(tmp_path, server):
    async with Host(tmp_path, server) as host:
        host.center.start()
        await server.wait_connected()
        await settle()

        assert server.calls(LIST) == 1
        assert server.calls(COUNTS) == 1
        assert host.center.counts().by_source == {"igni.artifacts": 1}
        assert host.announced[-1].unread == 1


async def test_the_list_is_read_at_start_even_without_a_stream(tmp_path, server):
    """Correctness never depends on the stream: an igni server that has
    none still has its list read once."""
    server.stream_status = 404
    async with Host(tmp_path, server) as host:
        host.center.start()
        await settle()

        assert server.calls(LIST) == 1
        assert host.center.counts().unread == 1
        assert not host.center.follower.following


async def test_the_opening_frame_of_a_first_connection_is_not_news(tmp_path, server):
    """igni server opens every connection with an event at the cursor it
    was given — an empty one, the first time."""
    async with Host(tmp_path, server) as host:
        host.center.start()
        await server.wait_connected()
        await settle()
        await server.wake("")
        await settle()

        assert server.calls(LIST) == 1


async def test_an_idle_stream_asks_for_nothing(tmp_path, server):
    """No timer: keepalives for a while, and the list is never read again."""
    async with Host(tmp_path, server) as host:
        host.center.start()
        await server.wait_connected()
        await settle()
        for _ in range(10):
            await server.send(": keepalive\n\n")
            await asyncio.sleep(0.03)

        assert server.calls(LIST) == 1
        assert server.calls(STREAM) == 1
        assert host.sleep.delays == []


# ── An event wakes the host ─────────────────────────────────────────


async def test_an_event_reads_the_list_again(tmp_path, server):
    async with Host(tmp_path, server) as host:
        host.center.start()
        await server.wait_connected()
        await settle()

        server.rows.append(server_row("n2", source="acme.scanner"))
        await server.wake("c2")
        await settle()

        assert server.calls(LIST) == 2
        assert host.center.counts().by_source == {"igni.artifacts": 1, "acme.scanner": 1}
        assert host.announced[-1].unread == 2
        assert host.center.follower.cursor == "c2"


async def test_the_opening_frame_is_not_read_twice(tmp_path, server):
    """igni server opens every connection with an event repeating the
    cursor it was given. The list was already read as the connection
    opened, so that event says nothing newer."""
    async with Host(tmp_path, server) as host:
        host.center.start()
        await server.wait_connected()
        await server.wake("c1")
        await settle()
        await server.end()
        await settle()
        host.sleep.release()
        await server.wait_connected()
        await settle()
        reads_before = server.calls(LIST)

        await server.wake("c1")
        await settle()

        assert server.calls(LIST) == reads_before


async def test_a_burst_of_events_is_not_a_burst_of_reads(tmp_path, server):
    async with Host(tmp_path, server) as host:
        host.center.start()
        await server.wait_connected()
        await settle()

        for n in range(10):
            await server.wake(f"c{n + 2}")
        await settle()

        assert server.calls(LIST) <= 3


# ── Reconnecting ────────────────────────────────────────────────────


async def test_a_dropped_stream_reconnects_with_its_cursor_and_reads_again(tmp_path, server):
    async with Host(tmp_path, server) as host:
        host.center.start()
        await server.wait_connected()
        await server.wake("c5")
        await settle()
        reads = server.calls(LIST)

        await server.end()
        await settle()
        # Waiting to reconnect, and not before the test says so.
        assert host.sleep.delays == [1.0]
        assert server.calls(STREAM) == 1

        host.sleep.release()
        await server.wait_connected()
        await settle()

        assert server.stream_cursors == [None, "c5"]
        assert server.calls(LIST) == reads + 1


async def test_failures_back_off_and_stop_growing(tmp_path, server):
    server.stream_status = 503
    async with Host(tmp_path, server) as host:
        host.center.start()
        for _ in range(6):
            await settle(5)
            host.sleep.release()
        await settle()

        assert host.sleep.delays[:5] == [1.0, 2.0, 4.0, 8.0, 8.0]
        # The one read at start; a connection that never opened reads nothing.
        assert server.calls(LIST) == 1


async def test_a_refused_session_stops_following(tmp_path, server):
    server.stream_status = 401
    async with Host(tmp_path, server) as host:
        host.center.start()
        await settle()

        assert not host.center.follower.following
        assert host.sleep.delays == []
        assert server.calls(STREAM) == 1


async def test_nobody_signed_in_means_no_connection(tmp_path, server):
    async with Host(tmp_path, server, session=None) as host:
        host.center.start()
        await settle()

        assert server.requests == []
        assert not host.center.follower.following


async def test_signing_in_follows_afresh_without_the_last_cursor(tmp_path, server):
    async with Host(tmp_path, server) as host:
        host.center.start()
        await server.wait_connected()
        await server.wake("c9")
        await settle()

        await host.center.signed_in()
        await server.wait_connected()
        await settle()

        assert server.stream_cursors[-1] is None
        assert server.calls(LIST) >= 2


async def test_signing_out_forgets_the_server_half(tmp_path, server):
    async with Host(tmp_path, server) as host:
        host.center.start()
        await server.wait_connected()
        await settle()
        assert host.center.counts().unread == 1

        await host.center.signed_out()

        assert host.center.counts().unread == 0
        assert host.announced[-1].unread == 0
        assert not host.center.follower.following


# ── The server ending a connection on purpose ───────────────────────


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_a_spent_connection_is_reopened_at_once(server):
    clock = Clock()
    sleep = RecordingSleep()
    opened: list[int] = []
    follower = NotificationStreamFollower(
        signed_in(),
        on_reconnect=lambda: opened.append(1),
        on_wake=lambda: None,
        transport=server.transport,
        backoff=Backoff(base=1.0, rng=lambda: 0.5),
        sleep=sleep,
        clock=clock,
    )
    try:
        follower.start()
        await server.wait_connected()
        clock.now += NotificationStreamFollower.STABLE_SECONDS + 1
        await server.send("event: reconnect\nid: c3\ndata: 1\n\n")
        await settle()

        assert sleep.delays == [0.0]
        sleep.release()
        await server.wait_connections(2)
        assert server.stream_cursors == [None, "c3"]
        # The reconnect reads the list; the first open did not need to.
        await settle()
        assert len(opened) == 1
    finally:
        await follower.stop()


async def test_a_server_that_ends_every_connection_at_once_is_backed_off(server):
    """``reconnect`` straight after opening, every time, must not become a
    loop that lists once a second."""
    sleep = RecordingSleep()
    follower = NotificationStreamFollower(
        signed_in(),
        on_reconnect=lambda: None,
        on_wake=lambda: None,
        transport=server.transport,
        backoff=Backoff(base=1.0, ceiling=60.0, rng=lambda: 0.5),
        sleep=sleep,
        clock=Clock(),
    )
    try:
        follower.start()
        for n in range(3):
            await server.wait_connections(n + 1)
            await server.send("event: reconnect\ndata: 1\n\n")
            await settle()
            sleep.release()
        await settle()

        assert sleep.delays[:3] == [1.0, 2.0, 4.0]
    finally:
        await follower.stop()
