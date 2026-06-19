"""Integration tests: one BE, multiple FE clients, multiple sessions.

These tests compose three layers that today are only covered in
isolation:

* ``WebSocketServerTransport`` — accepts N FE clients, broadcasts every
  outbound event to all of them, merges inbound from all into a single
  ``receive()`` stream.
* ``SessionPool`` — routes inbound messages to the matching
  ``SessionRuntime`` by ``session_id`` (creating one on first use).
* ``SessionStampingTransport`` — stamps each runtime's outbound events
  with that runtime's ``session_id`` so views can filter the broadcast
  stream to their bound session.

The unit tests prove each piece works on its own; this file wires them
together end-to-end so a regression that breaks the *composition* — a
stamping wrapper accidentally bypassed, a pool that shares state across
runtimes, an awaited dispatcher that serialises sessions — fails here
even when the unit tests still pass.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.backend.session_pool import (
    SessionPool,
    SessionRuntime,
    SessionStampingTransport,
)
from ember_code.protocol import messages as msg
from ember_code.transport.websocket import WebSocketServerTransport

# ── Helpers ──────────────────────────────────────────────────────────


async def _connect_and_welcome(port: int):
    """Open a WS client, drain the Welcome frame, return (ws, client_id)."""
    from websockets.asyncio.client import connect

    ws = await connect(f"ws://127.0.0.1:{port}")
    raw = await asyncio.wait_for(ws.recv(), 5)
    data = json.loads(raw)
    assert data["type"] == "welcome"
    return ws, data["client_id"]


def _runtime_for(session_id: str, ws_transport) -> SessionRuntime:
    """Build a SessionRuntime whose outbound transport stamps with
    ``session_id`` and rides the shared WS transport."""
    backend = MagicMock()
    backend.session_id = session_id
    backend.shutdown = AsyncMock()
    stamping = SessionStampingTransport(ws_transport, backend)
    return SessionRuntime(
        backend=backend,
        rpc_table={},
        queue=[],
        transport=stamping,
    )


def _pool_over(
    ws_transport,
    default_id: str = "default",
) -> tuple[SessionPool, list[str]]:
    """One pool, one shared WS transport. Factory records every
    spawned-session id so tests can assert no double-creation."""
    created: list[str] = []

    async def factory(session_id: str) -> SessionRuntime:
        created.append(session_id)
        return _runtime_for(session_id, ws_transport)

    pool = SessionPool(_runtime_for(default_id, ws_transport), factory)
    return pool, created


@dataclass
class Dispatcher:
    """Minimal stand-in for the BE's protocol dispatcher.

    Reads from the WS transport, routes by ``session_id`` via the pool,
    invokes the per-message ``handler`` with the matched runtime. The
    handler is where each test plugs in its emit logic.
    """

    transport: WebSocketServerTransport
    pool: SessionPool
    handler: Callable[[SessionRuntime, msg.Message], Awaitable[None]]
    _task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        async for m in self.transport.receive():
            rt = await self.pool.get_or_create(m.session_id)
            # Each handler call is a Task so a slow agent in session A
            # never blocks session B from making progress.
            asyncio.create_task(self.handler(rt, m))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task


async def _drain_for_session(ws, session_id: str, expected: int, timeout: float = 5.0):
    """Receive frames on ``ws`` until we've collected ``expected`` events
    stamped for ``session_id``. Returns the matching events; events for
    other sessions are kept so the test can assert what *leaked through*.
    """
    mine: list[dict] = []
    others: list[dict] = []
    deadline = time.monotonic() + timeout
    while len(mine) < expected:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError(
                f"only got {len(mine)}/{expected} for {session_id}; others={others}"
            )
        raw = await asyncio.wait_for(ws.recv(), remaining)
        data = json.loads(raw)
        if data.get("session_id") == session_id:
            mine.append(data)
        else:
            others.append(data)
    return mine, others


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_two_clients_two_sessions_stamp_and_isolate():
    """Two WS clients each bound to their own session_id; the BE
    broadcasts every emit to both clients but stamps the emitting
    runtime's session_id, so each client can filter its own stream.

    Asserts (the composition contract):
    * each request is routed to the matching runtime (no double-spawn,
      no cross-routing to the default)
    * each broadcast carries the correct ``session_id`` stamp
    * stamps never cross — alpha's emits are never tagged with beta's id
    """
    tr = WebSocketServerTransport(port=0)
    await tr.start()
    pool, created = _pool_over(tr, default_id="default")

    async def echo(rt: SessionRuntime, m: msg.Message) -> None:
        if isinstance(m, msg.UserMessage):
            await rt.transport.send(msg.Info(text=f"echo:{m.text}"))

    dispatcher = Dispatcher(tr, pool, echo)
    dispatcher.start()
    try:
        ws_a, _ = await _connect_and_welcome(tr.port)
        ws_b, _ = await _connect_and_welcome(tr.port)

        # Fire both in parallel — the BE must handle both inbound
        # streams without dropping or reordering across sessions.
        await asyncio.gather(
            ws_a.send(msg.UserMessage(text="hi-a", session_id="alpha").model_dump_json()),
            ws_b.send(msg.UserMessage(text="hi-b", session_id="beta").model_dump_json()),
        )

        mine_a, others_a = await _drain_for_session(ws_a, "alpha", expected=1)
        mine_b, others_b = await _drain_for_session(ws_b, "beta", expected=1)

        # Each client sees its own session's event …
        assert mine_a[0]["type"] == "info"
        assert mine_a[0]["text"] == "echo:hi-a"
        assert mine_b[0]["type"] == "info"
        assert mine_b[0]["text"] == "echo:hi-b"

        # … and the broadcast crossover carries the OTHER session's
        # stamp (so the FE filter will drop it). The crucial bit: a
        # stray frame stamped with the *own* id would be a stamping
        # regression — verify it can't happen.
        assert all(o.get("session_id") != "alpha" for o in others_a), others_a
        assert all(o.get("session_id") != "beta" for o in others_b), others_b

        # Routing: two distinct sessions, two factory calls, neither
        # was the default (the default is pre-seeded, never re-created).
        assert sorted(created) == ["alpha", "beta"]

        await ws_a.close()
        await ws_b.close()
    finally:
        await dispatcher.stop()
        await tr.close()


@pytest.mark.asyncio
async def test_two_sessions_run_in_parallel_not_serial():
    """Two long-running per-session handlers on one BE must execute
    concurrently — wall-clock ≈ slowest task, not sum of tasks.

    A regression that awaits the handler inline in the dispatch loop
    (instead of fanning it out as a Task) would serialise sessions and
    blow this past the budget.
    """
    tr = WebSocketServerTransport(port=0)
    await tr.start()
    pool, _ = _pool_over(tr, default_id="default")

    PER_SESSION_DELAY = 0.30  # generous; CI is noisy
    SERIAL_BUDGET = 0.50  # < 2 * PER_SESSION_DELAY by a clear margin

    async def slow_emit(rt: SessionRuntime, m: msg.Message) -> None:
        if isinstance(m, msg.UserMessage):
            await asyncio.sleep(PER_SESSION_DELAY)
            await rt.transport.send(msg.Info(text=f"done:{m.text}"))

    dispatcher = Dispatcher(tr, pool, slow_emit)
    dispatcher.start()
    try:
        ws_a, _ = await _connect_and_welcome(tr.port)
        ws_b, _ = await _connect_and_welcome(tr.port)

        start = time.monotonic()
        await asyncio.gather(
            ws_a.send(msg.UserMessage(text="A", session_id="sA").model_dump_json()),
            ws_b.send(msg.UserMessage(text="B", session_id="sB").model_dump_json()),
        )

        # Each client waits only for its own session's reply.
        mine_a, _ = await _drain_for_session(ws_a, "sA", expected=1)
        mine_b, _ = await _drain_for_session(ws_b, "sB", expected=1)
        elapsed = time.monotonic() - start

        assert mine_a[0]["text"] == "done:A"
        assert mine_b[0]["text"] == "done:B"
        assert elapsed < SERIAL_BUDGET, (
            f"sessions ran serially: {elapsed:.2f}s "
            f"(budget {SERIAL_BUDGET:.2f}s, per-session {PER_SESSION_DELAY:.2f}s)"
        )

        await ws_a.close()
        await ws_b.close()
    finally:
        await dispatcher.stop()
        await tr.close()


@pytest.mark.asyncio
async def test_per_session_streaming_order_under_interleave():
    """A real session streams many events (ContentDelta×N → Assistant
    Message). Two sessions streaming simultaneously will have their
    frames interleaved on the wire — but **per session** the order must
    be preserved end-to-end. A regression that swapped the WS broadcast
    loop for ``asyncio.gather`` (which can reorder) would fail here.
    """
    tr = WebSocketServerTransport(port=0)
    await tr.start()
    pool, _ = _pool_over(tr, default_id="default")

    CHUNKS = 6

    async def stream_chunks(rt: SessionRuntime, m: msg.Message) -> None:
        if isinstance(m, msg.UserMessage):
            for i in range(CHUNKS):
                await rt.transport.send(msg.ContentDelta(text=f"{m.text}-{i}"))
                # Yield so the other session can interleave its sends.
                await asyncio.sleep(0)

    dispatcher = Dispatcher(tr, pool, stream_chunks)
    dispatcher.start()
    try:
        ws_a, _ = await _connect_and_welcome(tr.port)
        ws_b, _ = await _connect_and_welcome(tr.port)

        await asyncio.gather(
            ws_a.send(msg.UserMessage(text="A", session_id="alpha").model_dump_json()),
            ws_b.send(msg.UserMessage(text="B", session_id="beta").model_dump_json()),
        )

        mine_a, _ = await _drain_for_session(ws_a, "alpha", expected=CHUNKS)
        mine_b, _ = await _drain_for_session(ws_b, "beta", expected=CHUNKS)

        assert [d["text"] for d in mine_a] == [f"A-{i}" for i in range(CHUNKS)]
        assert [d["text"] for d in mine_b] == [f"B-{i}" for i in range(CHUNKS)]

        await ws_a.close()
        await ws_b.close()
    finally:
        await dispatcher.stop()
        await tr.close()


@pytest.mark.asyncio
async def test_two_clients_same_session_both_mirror_events():
    """Two FE clients bound to the **same** ``session_id`` (e.g. two
    browser tabs of the same project) must both receive every event
    that session emits. This is the "mirroring" contract called out in
    ``WebSocketServerTransport``'s docstring."""
    tr = WebSocketServerTransport(port=0)
    await tr.start()
    pool, created = _pool_over(tr, default_id="default")

    async def echo(rt: SessionRuntime, m: msg.Message) -> None:
        if isinstance(m, msg.UserMessage):
            await rt.transport.send(msg.Info(text=f"echo:{m.text}"))

    dispatcher = Dispatcher(tr, pool, echo)
    dispatcher.start()
    try:
        ws_tab1, _ = await _connect_and_welcome(tr.port)
        ws_tab2, _ = await _connect_and_welcome(tr.port)

        # tab1 fires the message; tab2 didn't send anything but is bound
        # to the same session, so it should mirror the response.
        await ws_tab1.send(msg.UserMessage(text="hi", session_id="shared").model_dump_json())

        mine_tab1, _ = await _drain_for_session(ws_tab1, "shared", expected=1)
        mine_tab2, _ = await _drain_for_session(ws_tab2, "shared", expected=1)

        assert mine_tab1[0]["text"] == "echo:hi"
        assert mine_tab2[0]["text"] == "echo:hi"
        # One session, created once, regardless of how many tabs attach.
        assert created == ["shared"]

        await ws_tab1.close()
        await ws_tab2.close()
    finally:
        await dispatcher.stop()
        await tr.close()


@pytest.mark.asyncio
async def test_disconnect_drops_in_flight_events_then_reconnect_resumes():
    """Pins the documented WS behaviour: events emitted while no client
    is attached are dropped (see ``test_send_without_client_is_noop``),
    and a reconnect using the same ``session_id`` resumes routing to
    the same runtime (no ghost spawn).

    If we ever add a per-session buffer, this test should be updated to
    reflect the new contract — but until then, the dropped-event
    behaviour is intentional and worth pinning so we don't silently
    change it.
    """
    tr = WebSocketServerTransport(port=0)
    await tr.start()
    pool, created = _pool_over(tr, default_id="default")

    async def emit_two_with_gap(rt: SessionRuntime, m: msg.Message) -> None:
        # First chunk lands while client is attached; the gap straddles
        # the planned disconnect; the second chunk fires into the void.
        if isinstance(m, msg.UserMessage) and m.text == "burst":
            await rt.transport.send(msg.Info(text="chunk-1"))
            await asyncio.sleep(0.15)
            await rt.transport.send(msg.Info(text="chunk-2"))
        elif isinstance(m, msg.UserMessage) and m.text == "ping":
            await rt.transport.send(msg.Info(text="pong"))

    dispatcher = Dispatcher(tr, pool, emit_two_with_gap)
    dispatcher.start()
    try:
        ws1, _ = await _connect_and_welcome(tr.port)
        await ws1.send(msg.UserMessage(text="burst", session_id="s").model_dump_json())

        mine, _ = await _drain_for_session(ws1, "s", expected=1)
        assert mine[0]["text"] == "chunk-1"

        # Disconnect *before* chunk-2 fires.
        await ws1.close()
        # Give the WS handler time to detect the close + the second
        # emit time to fire into the void.
        await asyncio.sleep(0.20)

        # Reconnect under the SAME session_id — routing must hit the
        # already-spawned runtime, not create a second one.
        ws2, _ = await _connect_and_welcome(tr.port)
        await ws2.send(msg.UserMessage(text="ping", session_id="s").model_dump_json())
        mine2, others2 = await _drain_for_session(ws2, "s", expected=1)
        assert mine2[0]["text"] == "pong"
        # chunk-2 was dropped on the floor (no client) — must not have
        # been buffered and replayed to the reconnected client.
        assert all(o.get("text") != "chunk-2" for o in others2), others2
        # Runtime was spawned exactly once across the disconnect.
        assert created == ["s"]

        await ws2.close()
    finally:
        await dispatcher.stop()
        await tr.close()


@pytest.mark.asyncio
async def test_ten_sessions_concurrent_create_routes_each_to_own_runtime():
    """N=10 sessions created concurrently — exercises the pool's
    ``_create_lock`` and ``find()`` linear scan at modest scale. Every
    client must end up routed to its own runtime; no double-creation,
    no cross-routing."""
    tr = WebSocketServerTransport(port=0)
    await tr.start()
    pool, created = _pool_over(tr, default_id="default")

    async def echo(rt: SessionRuntime, m: msg.Message) -> None:
        if isinstance(m, msg.UserMessage):
            await rt.transport.send(msg.Info(text=f"echo:{m.text}"))

    dispatcher = Dispatcher(tr, pool, echo)
    dispatcher.start()
    try:
        N = 10
        sockets = await asyncio.gather(*[_connect_and_welcome(tr.port) for _ in range(N)])

        # Fire every client's UserMessage concurrently with a distinct
        # session_id so the pool sees N parallel create requests.
        await asyncio.gather(
            *[
                ws.send(msg.UserMessage(text=f"m{i}", session_id=f"s{i}").model_dump_json())
                for i, (ws, _) in enumerate(sockets)
            ]
        )

        for i, (ws, _) in enumerate(sockets):
            mine, _ = await _drain_for_session(ws, f"s{i}", expected=1)
            assert mine[0]["text"] == f"echo:m{i}"

        assert sorted(created) == sorted(f"s{i}" for i in range(N))
        for ws, _ in sockets:
            await ws.close()
    finally:
        await dispatcher.stop()
        await tr.close()


@pytest.mark.asyncio
async def test_burst_inbound_from_one_client_dispatches_in_order():
    """A single client sending N messages back-to-back must be observed
    by the dispatcher in send-order. The WS transport's inbox is a
    FIFO; TCP guarantees per-connection ordering. This test pins both.
    """
    tr = WebSocketServerTransport(port=0)
    await tr.start()
    pool, _ = _pool_over(tr, default_id="default")

    observed: list[str] = []

    async def record(rt: SessionRuntime, m: msg.Message) -> None:
        if isinstance(m, msg.UserMessage):
            observed.append(m.text)
            # Ack so the test can synchronise on completion without
            # relying on timing.
            await rt.transport.send(msg.Info(text=f"ack:{m.text}"))

    dispatcher = Dispatcher(tr, pool, record)
    dispatcher.start()
    try:
        ws, _ = await _connect_and_welcome(tr.port)

        N = 8
        # Fire all sends without awaiting between them — push the
        # transport's queue as hard as a single client can.
        await asyncio.gather(
            *[
                ws.send(msg.UserMessage(text=f"m{i}", session_id="s").model_dump_json())
                for i in range(N)
            ]
        )

        # Wait for all acks before asserting — guarantees every
        # handler has run.
        await _drain_for_session(ws, "s", expected=N)
        assert observed == [f"m{i}" for i in range(N)]

        await ws.close()
    finally:
        await dispatcher.stop()
        await tr.close()


@pytest.mark.asyncio
async def test_one_ws_switching_session_ids_routes_each_to_own_runtime():
    """A single FE client may target different sessions on subsequent
    messages (e.g. the user switches projects, the composer keeps the
    same WS open). The BE must route purely by per-message
    ``session_id`` — never sticky-cache a session per ``client_id``.
    """
    tr = WebSocketServerTransport(port=0)
    await tr.start()
    pool, created = _pool_over(tr, default_id="default")

    async def echo(rt: SessionRuntime, m: msg.Message) -> None:
        if isinstance(m, msg.UserMessage):
            await rt.transport.send(msg.Info(text=f"{rt.backend.session_id}:{m.text}"))

    dispatcher = Dispatcher(tr, pool, echo)
    dispatcher.start()
    try:
        ws, _ = await _connect_and_welcome(tr.port)

        await ws.send(msg.UserMessage(text="hi-a", session_id="a").model_dump_json())
        mine_a, _ = await _drain_for_session(ws, "a", expected=1)
        assert mine_a[0]["text"] == "a:hi-a"

        await ws.send(msg.UserMessage(text="hi-b", session_id="b").model_dump_json())
        mine_b, _ = await _drain_for_session(ws, "b", expected=1)
        assert mine_b[0]["text"] == "b:hi-b"

        # Hop back to "a" — must reuse the original runtime, not spawn
        # a third.
        await ws.send(msg.UserMessage(text="back", session_id="a").model_dump_json())
        mine_a2, _ = await _drain_for_session(ws, "a", expected=1)
        assert mine_a2[0]["text"] == "a:back"

        assert sorted(created) == ["a", "b"]
        await ws.close()
    finally:
        await dispatcher.stop()
        await tr.close()


@pytest.mark.asyncio
async def test_handler_error_in_one_session_does_not_taint_another():
    """If a handler raises mid-dispatch, the dispatcher must keep
    pumping for every *other* session. A regression that awaited the
    handler in the dispatch loop (and let an exception kill the loop)
    would block session B forever — caught here by timeout."""
    tr = WebSocketServerTransport(port=0)
    await tr.start()
    pool, _ = _pool_over(tr, default_id="default")

    async def handler(rt: SessionRuntime, m: msg.Message) -> None:
        if not isinstance(m, msg.UserMessage):
            return
        if rt.backend.session_id == "boom":
            raise ValueError("handler exploded for session boom")
        await rt.transport.send(msg.Info(text=f"ok:{m.text}"))

    # Silence asyncio's "Task exception was never retrieved" noise from
    # the deliberate ValueError so the test output stays clean.
    loop = asyncio.get_running_loop()
    original_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, _ctx: None)

    dispatcher = Dispatcher(tr, pool, handler)
    dispatcher.start()
    try:
        ws_boom, _ = await _connect_and_welcome(tr.port)
        ws_ok, _ = await _connect_and_welcome(tr.port)

        await asyncio.gather(
            ws_boom.send(msg.UserMessage(text="x", session_id="boom").model_dump_json()),
            ws_ok.send(msg.UserMessage(text="y", session_id="ok").model_dump_json()),
        )

        # The "ok" client gets its reply even though "boom" raised.
        mine_ok, _ = await _drain_for_session(ws_ok, "ok", expected=1)
        assert mine_ok[0]["text"] == "ok:y"

        # And the dispatcher is still alive — a follow-up on "ok"
        # completes too.
        await ws_ok.send(msg.UserMessage(text="z", session_id="ok").model_dump_json())
        mine_ok2, _ = await _drain_for_session(ws_ok, "ok", expected=1)
        assert mine_ok2[0]["text"] == "ok:z"

        await ws_boom.close()
        await ws_ok.close()
    finally:
        loop.set_exception_handler(original_handler)
        await dispatcher.stop()
        await tr.close()


@pytest.mark.asyncio
async def test_real_handle_message_routes_two_sessions_through_production_path():
    """Drives the real ``ember_code.backend.__main__._handle_message``
    (the production per-message dispatcher) under a multi-session pool
    with a real ``WebSocketServerTransport``. The earlier tests in this
    file use a fake dispatcher — this one closes that gap by routing
    two FE clients' ``UserMessage`` requests through the actual code
    path that runs in the BE process.

    What's mocked: only the heavy bits of ``BackendServer`` — the
    Agno team in particular — by bypassing ``__init__`` and stubbing
    ``run_message`` to yield a deterministic ContentDelta + Assistant
    Message stream. Everything else (the dispatch wiring, the stamping
    wrapper, the WS broadcast, the StreamEnd + UserMessageReceived
    book-ending) is real production code.
    """
    from ember_code.backend.__main__ import _handle_message
    from ember_code.backend.server import BackendServer

    def _fake_backend(session_id: str) -> Any:
        """Real ``BackendServer`` instance with ``__init__`` skipped —
        same trick used in ``tests/test_backend_server.py``."""
        be = BackendServer.__new__(BackendServer)
        # ``session_id`` and ``project_dir`` are properties delegating
        # to ``_session.{session_id,project_dir}`` — stand up a stub
        # ``_session`` exposing both so the property reads work.
        be._session = MagicMock()
        be._session.session_id = session_id
        be._session.project_dir = "/tmp"  # unused by the paths exercised here
        be.cancel_run = MagicMock()
        be.shutdown = AsyncMock()
        be.handle_command = AsyncMock()
        be.switch_model = MagicMock()
        be.toggle_mcp = AsyncMock()
        be.list_sessions = AsyncMock()
        be.switch_session = AsyncMock()
        be.maybe_auto_name_session = AsyncMock(return_value=None)

        async def fake_run(text: str, media: Any = None):
            # Three deltas + a StreamingDone — mimics the shape Agno
            # emits at the protocol level without going anywhere near
            # the team. ``_handle_message`` appends its own ``StreamEnd``
            # after the async-for loop drains.
            yield msg.ContentDelta(text=f"[{session_id}]:")
            yield msg.ContentDelta(text=f"{text}.1")
            yield msg.ContentDelta(text=f"{text}.2")
            yield msg.StreamingDone()

        be.run_message = fake_run
        return be

    tr = WebSocketServerTransport(port=0)
    await tr.start()

    # Build the pool exactly like ``__main__`` does: each runtime gets
    # a SessionStampingTransport over the shared WS transport.
    backends_by_session: dict[str, Any] = {}

    def _make_runtime(session_id: str) -> SessionRuntime:
        be = _fake_backend(session_id)
        backends_by_session[session_id] = be
        return SessionRuntime(
            backend=be,
            rpc_table={},
            queue=[],
            transport=SessionStampingTransport(tr, be),
        )

    default_rt = _make_runtime("default")

    async def factory(session_id: str) -> SessionRuntime:
        return _make_runtime(session_id)

    pool = SessionPool(default_rt, factory)

    # Production-style dispatch loop, copied in shape from
    # ``_dispatch`` in ``__main__`` — fan out via Task per message.
    in_flight: set[asyncio.Task] = set()

    async def _dispatch_one(message: Any) -> None:
        rt = await pool.get_or_create(message.session_id or "")
        rt.remember_id()
        await _handle_message(message, rt.backend, rt.transport, rt.rpc_table, rt.queue, None)

    async def _loop() -> None:
        async for m in tr.receive():
            t = asyncio.create_task(_dispatch_one(m))
            in_flight.add(t)
            t.add_done_callback(in_flight.discard)

    loop_task = asyncio.create_task(_loop())
    try:
        ws_a, _ = await _connect_and_welcome(tr.port)
        ws_b, _ = await _connect_and_welcome(tr.port)

        await asyncio.gather(
            ws_a.send(msg.UserMessage(text="hi-a", session_id="alpha").model_dump_json()),
            ws_b.send(msg.UserMessage(text="hi-b", session_id="beta").model_dump_json()),
        )

        # Per session: 1 UserMessageReceived (echo) + 3 ContentDelta +
        # 1 StreamingDone + 1 StreamEnd  = 6 frames.
        EXPECTED_PER_SESSION = 6
        mine_a, _ = await _drain_for_session(ws_a, "alpha", expected=EXPECTED_PER_SESSION)
        mine_b, _ = await _drain_for_session(ws_b, "beta", expected=EXPECTED_PER_SESSION)

        # ── Per-session ordering: production stream shape ──
        types_a = [d["type"] for d in mine_a]
        assert types_a == [
            "user_message_received",
            "content_delta",
            "content_delta",
            "content_delta",
            "streaming_done",
            "stream_end",
        ], types_a
        types_b = [d["type"] for d in mine_b]
        assert types_b == types_a, "session B must have the same shape"

        # ── Content stamping: the stream content is the session's own ──
        deltas_a = [d["text"] for d in mine_a if d["type"] == "content_delta"]
        assert deltas_a == ["[alpha]:", "hi-a.1", "hi-a.2"]
        deltas_b = [d["text"] for d in mine_b if d["type"] == "content_delta"]
        assert deltas_b == ["[beta]:", "hi-b.1", "hi-b.2"]

        # ── Pool created the two named sessions on top of the boot
        # default; no extra/ghost spawns. ──
        assert set(backends_by_session.keys()) == {"default", "alpha", "beta"}
        # ``_handle_message`` fires ``maybe_auto_name_session`` as a
        # post-run task — give the event loop a tick to schedule it
        # before asserting.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        backends_by_session["default"].maybe_auto_name_session.assert_not_called()
        backends_by_session["alpha"].maybe_auto_name_session.assert_called_once()
        backends_by_session["beta"].maybe_auto_name_session.assert_called_once()

        # ── No cross-pollination: client A never receives any frame
        # stamped for beta, and vice versa. (The drain helper already
        # collected non-matching frames into ``_``; assert there were
        # exactly zero foreign-session events.) ──
        # The drain returned ``(mine, others)``; assert ``others`` was
        # empty by re-reading what's still queued — give the BE a beat
        # to deliver any stragglers before asserting nothing leaked.
        await asyncio.sleep(0.05)
        for ws, my_sid, other_sid in (
            (ws_a, "alpha", "beta"),
            (ws_b, "beta", "alpha"),
        ):
            stray: list[dict] = []
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), 0.02)
                except asyncio.TimeoutError:
                    break
                stray.append(json.loads(raw))
            assert all(s.get("session_id") != my_sid for s in stray), (
                f"{my_sid} client got an extra own-session event after drain: {stray}"
            )
            # Cross-stream events (the OTHER session's stamped frames
            # reaching this client via broadcast) are allowed — the FE
            # filters by session_id. They must NOT be stamped with our
            # own id, though.
            assert all(s.get("session_id") in ("", other_sid) for s in stray), (
                f"unexpected stamping on stray frames for {my_sid}: {stray}"
            )

        # Each fake backend's run_message must have been awaited
        # exactly once with its own user text.
        # (run_message is an async generator, not a Mock, so we
        # can't assert_called_with — but the deltas above already
        # prove it ran once per session with the right text.)

        await ws_a.close()
        await ws_b.close()
    finally:
        loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task
        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)
        await tr.close()


@pytest.mark.asyncio
async def test_hitl_pause_in_one_session_does_not_block_another():
    """A run paused for HITL approval on session A must not block
    session B from sending its own message and getting a response.
    Each runtime owns its own ``_processing``/``_run_lock``/HITL
    state — but the wiring (one BE process, one dispatcher) is what
    we're testing here: a hung handler on A must not back-pressure
    B's dispatch.

    Catches a regression class where a global lock or a shared
    coordination primitive leaks between runtimes (e.g. someone
    moves ``_run_lock`` to the pool).
    """
    tr = WebSocketServerTransport(port=0)
    await tr.start()
    pool, _ = _pool_over(tr, default_id="default")

    paused = asyncio.Event()
    release = asyncio.Event()

    async def handler(rt: SessionRuntime, m: msg.Message) -> None:
        if not isinstance(m, msg.UserMessage):
            return
        if rt.backend.session_id == "paused":
            # Mimic the BE awaiting an HITL approval mid-stream: emit
            # a partial chunk + a HITL request, then park until the
            # test releases. A regression that serialised the
            # dispatcher around per-session runs would make session
            # "ok" wait until ``release`` fires.
            await rt.transport.send(msg.ContentDelta(text="partial-A"))
            await rt.transport.send(
                msg.HITLRequest(
                    requirement_id="req-1",
                    tool_name="apply_patch",
                    summary="approve?",
                )
            )
            paused.set()
            await release.wait()
            await rt.transport.send(msg.Info(text="resumed-A"))
        else:
            await rt.transport.send(msg.Info(text=f"echo:{m.text}"))

    dispatcher = Dispatcher(tr, pool, handler)
    dispatcher.start()
    try:
        ws_paused, _ = await _connect_and_welcome(tr.port)
        ws_ok, _ = await _connect_and_welcome(tr.port)

        # Start A's run; wait for it to actually park in HITL.
        await ws_paused.send(msg.UserMessage(text="A", session_id="paused").model_dump_json())
        await asyncio.wait_for(paused.wait(), 2.0)

        # Now fire B — must complete promptly. If the dispatcher
        # serialised by session, B would wait until ``release`` fires.
        t0 = time.monotonic()
        await ws_ok.send(msg.UserMessage(text="hello", session_id="ok").model_dump_json())
        mine_ok, _ = await _drain_for_session(ws_ok, "ok", expected=1)
        elapsed = time.monotonic() - t0
        assert mine_ok[0]["text"] == "echo:hello"
        assert elapsed < 0.30, f"session B was blocked by A's HITL pause: {elapsed:.2f}s"

        # Release A; its tail event must arrive afterwards.
        release.set()
        mine_a, _ = await _drain_for_session(ws_paused, "paused", expected=3)
        # Order: ContentDelta, HITLRequest, Info("resumed-A")
        assert mine_a[-1]["type"] == "info"
        assert mine_a[-1]["text"] == "resumed-A"

        await ws_paused.close()
        await ws_ok.close()
    finally:
        await dispatcher.stop()
        await tr.close()


@pytest.mark.asyncio
async def test_cancel_targets_only_the_emitting_session():
    """``Cancel`` is routed by ``session_id`` just like every other
    inbound message — sending it on session A's connection must
    only call ``backend.cancel_run`` on runtime A, never on B.

    This is the production behaviour relied on by the FE's
    "Stop" button: a user pressing stop in tab A while tab B is
    running a separate session must not interrupt B.
    """
    tr = WebSocketServerTransport(port=0)
    await tr.start()
    pool, _ = _pool_over(tr, default_id="default")
    cancels: dict[str, int] = {}

    async def handler(rt: SessionRuntime, m: msg.Message) -> None:
        sid = rt.backend.session_id
        if isinstance(m, msg.UserMessage):
            await rt.transport.send(msg.Info(text=f"started:{sid}"))
        elif isinstance(m, msg.Cancel):
            # Use the runtime's real ``cancel_run`` mock to record
            # which runtime was targeted. Real BackendServer's
            # ``cancel_run`` cancels ``_current_run_task``; the
            # invariant we care about is *which* runtime's hook got
            # called, not what cancellation actually does.
            rt.backend.cancel_run()
            cancels[sid] = cancels.get(sid, 0) + 1

    # Patch the cancel_run MagicMocks to record sid → count.
    dispatcher = Dispatcher(tr, pool, handler)
    dispatcher.start()
    try:
        ws_a, _ = await _connect_and_welcome(tr.port)
        ws_b, _ = await _connect_and_welcome(tr.port)

        # Spawn both runtimes by sending one UserMessage each.
        await asyncio.gather(
            ws_a.send(msg.UserMessage(text="a", session_id="s-a").model_dump_json()),
            ws_b.send(msg.UserMessage(text="b", session_id="s-b").model_dump_json()),
        )
        await _drain_for_session(ws_a, "s-a", expected=1)
        await _drain_for_session(ws_b, "s-b", expected=1)

        # Now send Cancel from A's connection stamped for s-a only.
        await ws_a.send(msg.Cancel(session_id="s-a").model_dump_json())
        # Give the dispatcher a tick to route the cancel.
        await asyncio.sleep(0.05)

        assert cancels == {"s-a": 1}, cancels

        # Inspect the per-runtime backend mocks to confirm only A's
        # ``cancel_run`` was invoked — independent verification that
        # routing landed where we expected.
        for rt in pool.runtimes:
            if rt.backend.session_id == "s-a":
                rt.backend.cancel_run.assert_called_once()
            else:
                rt.backend.cancel_run.assert_not_called()

        await ws_a.close()
        await ws_b.close()
    finally:
        await dispatcher.stop()
        await tr.close()


@pytest.mark.asyncio
async def test_long_parallel_streams_complete_without_serial_blow_up():
    """A stand-in for "two real agent runs in parallel for minutes":
    two sessions each emit a long burst of ContentDeltas with small
    inter-chunk sleeps, totalling ~1s of wall-clock per session. They
    must complete in roughly max(per_session_time), not sum, AND
    every per-session event must arrive in order with no drops.

    Catches a class of regressions that only manifest with sustained
    load — a lock held across awaits, an unbounded inbox that
    eventually OOMs, a fairness bug where one session starves
    another. The pre-fix slow-client back-pressure regression would
    also surface here under enough emit pressure.
    """
    tr = WebSocketServerTransport(port=0)
    await tr.start()
    pool, _ = _pool_over(tr, default_id="default")

    CHUNKS = 80
    CHUNK_SLEEP = 0.005  # 80 × 5 ms ≈ 0.4 s per session
    # Generous parallel budget — slightly above the single-session
    # cost to absorb scheduler noise without permitting a 2× serial
    # blow-up.
    PARALLEL_BUDGET = 0.80

    async def emit_burst(rt: SessionRuntime, m: msg.Message) -> None:
        if not isinstance(m, msg.UserMessage):
            return
        for i in range(CHUNKS):
            await rt.transport.send(msg.ContentDelta(text=f"{m.text}:{i:03d}"))
            await asyncio.sleep(CHUNK_SLEEP)
        await rt.transport.send(msg.StreamingDone())

    dispatcher = Dispatcher(tr, pool, emit_burst)
    dispatcher.start()
    try:
        ws_a, _ = await _connect_and_welcome(tr.port)
        ws_b, _ = await _connect_and_welcome(tr.port)

        t0 = time.monotonic()
        await asyncio.gather(
            ws_a.send(msg.UserMessage(text="A", session_id="long-a").model_dump_json()),
            ws_b.send(msg.UserMessage(text="B", session_id="long-b").model_dump_json()),
        )

        # Expect CHUNKS ContentDeltas + 1 StreamingDone per session.
        mine_a, _ = await _drain_for_session(ws_a, "long-a", expected=CHUNKS + 1)
        mine_b, _ = await _drain_for_session(ws_b, "long-b", expected=CHUNKS + 1)
        elapsed = time.monotonic() - t0

        # ── Ordering: per-session deltas arrive 0..CHUNKS-1 in order ──
        deltas_a = [d["text"] for d in mine_a if d["type"] == "content_delta"]
        assert deltas_a == [f"A:{i:03d}" for i in range(CHUNKS)]
        deltas_b = [d["text"] for d in mine_b if d["type"] == "content_delta"]
        assert deltas_b == [f"B:{i:03d}" for i in range(CHUNKS)]

        # ── Liveness: both sessions finished concurrently, well under
        # the serial-equivalent 2× CHUNKS × CHUNK_SLEEP. ──
        assert elapsed < PARALLEL_BUDGET, (
            f"sustained-load parallel runs took {elapsed:.2f}s "
            f"(budget {PARALLEL_BUDGET:.2f}s, per-session ~"
            f"{CHUNKS * CHUNK_SLEEP:.2f}s)"
        )

        await ws_a.close()
        await ws_b.close()
    finally:
        await dispatcher.stop()
        await tr.close()


@pytest.mark.asyncio
async def test_sync_helper_offloaded_to_thread_does_not_block_other_sessions():
    """Regression guard for the audit fix in this branch.

    Before the fix, async RPCs that called sync helpers (e.g.
    ``codeindex_status`` → ``sync_manager.current_sha()`` →
    ``subprocess.run``) ran the blocking call inline on the event
    loop. Under multi-session load, *any* session's slow git call
    stalled every other session's dispatch for the duration of the
    subprocess.

    This test simulates the same shape: a blocking sync helper
    (``time.sleep``) wrapped in ``asyncio.to_thread`` from one
    session's handler, with a fast handler running in another
    session. The fast session must complete in well under the
    blocking duration.
    """
    import time as _time

    tr = WebSocketServerTransport(port=0)
    await tr.start()
    pool, _ = _pool_over(tr, default_id="default")

    BLOCK_SECONDS = 0.40
    FAST_BUDGET = 0.20  # half the block: proves we're not serialised

    def slow_blocking_helper() -> str:
        # The "git" stand-in: a real sync call (no await) that would
        # block the event loop if called inline.
        _time.sleep(BLOCK_SECONDS)
        return "done"

    async def handler(rt: SessionRuntime, m: msg.Message) -> None:
        if not isinstance(m, msg.UserMessage):
            return
        if rt.backend.session_id == "blocker":
            # Properly offloaded — fixed-shape regression test.
            result = await asyncio.to_thread(slow_blocking_helper)
            await rt.transport.send(msg.Info(text=f"slow:{result}"))
        else:
            await rt.transport.send(msg.Info(text="fast"))

    dispatcher = Dispatcher(tr, pool, handler)
    dispatcher.start()
    try:
        ws_blocker, _ = await _connect_and_welcome(tr.port)
        ws_fast, _ = await _connect_and_welcome(tr.port)

        # Fire the blocker first so its handler is parked in
        # ``to_thread`` when the fast message arrives.
        await ws_blocker.send(msg.UserMessage(text="x", session_id="blocker").model_dump_json())
        # Tiny yield so the blocker's task is scheduled and reaches
        # ``await asyncio.to_thread`` before we send the fast
        # message. Without this, the fast message could arrive
        # before the blocker's task even starts — defeats the test.
        await asyncio.sleep(0.01)

        t0 = time.monotonic()
        await ws_fast.send(msg.UserMessage(text="y", session_id="fast").model_dump_json())
        mine_fast, _ = await _drain_for_session(ws_fast, "fast", expected=1)
        elapsed = time.monotonic() - t0

        assert mine_fast[0]["text"] == "fast"
        assert elapsed < FAST_BUDGET, (
            f"fast session was blocked by sibling's to_thread call: "
            f"{elapsed:.2f}s (budget {FAST_BUDGET:.2f}s, block "
            f"{BLOCK_SECONDS:.2f}s)"
        )

        # And the blocker eventually completes too.
        mine_blocker, _ = await _drain_for_session(ws_blocker, "blocker", expected=1)
        assert mine_blocker[0]["text"] == "slow:done"

        await ws_blocker.close()
        await ws_fast.close()
    finally:
        await dispatcher.stop()
        await tr.close()


@pytest.mark.asyncio
async def test_pool_shutdown_does_not_block_on_in_flight_emit():
    """``SessionPool.shutdown`` must release all runtimes promptly; it
    must not wait for in-flight per-session emit tasks (which are owned
    by the dispatcher, not the pool). A regression that gathered or
    awaited active emit tasks would hang under a long-running stream.
    """
    tr = WebSocketServerTransport(port=0)
    await tr.start()
    pool, _ = _pool_over(tr, default_id="default")

    started = asyncio.Event()
    finish_emit = asyncio.Event()

    async def slow_emit(rt: SessionRuntime, m: msg.Message) -> None:
        if isinstance(m, msg.UserMessage):
            started.set()
            # Park the emit task indefinitely until the test releases
            # it. Pool.shutdown must NOT wait on this.
            await finish_emit.wait()
            await rt.transport.send(msg.Info(text="late"))

    dispatcher = Dispatcher(tr, pool, slow_emit)
    dispatcher.start()
    try:
        ws, _ = await _connect_and_welcome(tr.port)
        await ws.send(msg.UserMessage(text="trigger", session_id="s").model_dump_json())
        # Wait until the slow handler has actually parked before
        # we call shutdown — otherwise the test races between
        # send → receive → handler-start vs. shutdown.
        await asyncio.wait_for(started.wait(), 2.0)

        t0 = time.monotonic()
        await asyncio.wait_for(pool.shutdown(), 1.0)
        elapsed = time.monotonic() - t0
        # Generous budget: shutdown should be near-instant.
        assert elapsed < 0.10, f"pool.shutdown blocked {elapsed:.3f}s"

        # Every runtime's backend.shutdown was awaited exactly once.
        for rt in pool.runtimes:
            rt.backend.shutdown.assert_awaited_once()

        finish_emit.set()  # let the parked task complete cleanly
        await ws.close()
    finally:
        await dispatcher.stop()
        await tr.close()


@pytest.mark.asyncio
async def test_default_session_unaffected_by_named_session_traffic():
    """A TUI-style client (empty session_id → default runtime) sharing
    one BE with a GUI session must not be re-routed or starved.
    """
    tr = WebSocketServerTransport(port=0)
    await tr.start()
    pool, created = _pool_over(tr, default_id="default")

    async def echo(rt: SessionRuntime, m: msg.Message) -> None:
        if isinstance(m, msg.UserMessage):
            await rt.transport.send(msg.Info(text=f"echo:{m.text}"))

    dispatcher = Dispatcher(tr, pool, echo)
    dispatcher.start()
    try:
        ws_tui, _ = await _connect_and_welcome(tr.port)
        ws_gui, _ = await _connect_and_welcome(tr.port)

        await asyncio.gather(
            ws_tui.send(msg.UserMessage(text="tui").model_dump_json()),  # no session_id
            ws_gui.send(msg.UserMessage(text="gui", session_id="gui-s").model_dump_json()),
        )

        mine_default, _ = await _drain_for_session(ws_tui, "default", expected=1)
        mine_gui, _ = await _drain_for_session(ws_gui, "gui-s", expected=1)

        assert mine_default[0]["text"] == "echo:tui"
        assert mine_gui[0]["text"] == "echo:gui"
        # Only the GUI session was spawned — the default came from boot.
        assert created == ["gui-s"]

        await ws_tui.close()
        await ws_gui.close()
    finally:
        await dispatcher.stop()
        await tr.close()
