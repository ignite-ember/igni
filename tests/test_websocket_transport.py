"""Tests for the WebSocket BE transport used by GUI clients.

Multi-client mirroring semantics: N clients attach to one BE session;
every ``send()`` broadcasts to all of them; each client receives a
``Welcome`` with its client_id at attach.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ember_code.protocol import messages as msg
from ember_code.transport.in_process import InProcessTransport
from ember_code.transport.websocket import (
    CompositeTransport,
    WebSocketServerTransport,
)


async def _connect(port: int):
    from websockets.asyncio.client import connect

    return await connect(f"ws://127.0.0.1:{port}")


async def _connect_and_welcome(port: int):
    """Connect and consume the Welcome; returns (ws, client_id)."""
    ws = await _connect(port)
    raw = await asyncio.wait_for(ws.recv(), 5)
    data = json.loads(raw)
    assert data["type"] == "welcome", f"first frame must be welcome, got {data['type']}"
    assert data["client_id"]
    return ws, data["client_id"]


@pytest.mark.asyncio
async def test_port_auto_assign():
    """port=0 binds an ephemeral port and exposes it via ``.port``."""
    tr = WebSocketServerTransport(port=0)
    await tr.start()
    try:
        assert tr.port > 0
    finally:
        await tr.close()


@pytest.mark.asyncio
async def test_welcome_assigns_unique_client_ids():
    tr = WebSocketServerTransport(port=0)
    await tr.start()
    try:
        ws1, id1 = await _connect_and_welcome(tr.port)
        ws2, id2 = await _connect_and_welcome(tr.port)
        assert id1 != id2
        assert tr.client_count == 2
        await ws1.close()
        await ws2.close()
    finally:
        await tr.close()


@pytest.mark.asyncio
async def test_round_trip_protocol_messages():
    """One JSON protocol message per text frame, both directions."""
    tr = WebSocketServerTransport(port=0)
    await tr.start()
    try:
        ws, _ = await _connect_and_welcome(tr.port)
        await ws.send(msg.UserMessage(text="hello", id="r1").model_dump_json())

        received = None

        async def _recv_one():
            nonlocal received
            async for m in tr.receive():
                received = m
                break

        await asyncio.wait_for(_recv_one(), 5)
        assert isinstance(received, msg.UserMessage)
        assert received.text == "hello"
        assert received.id == "r1"

        await tr.send(msg.Info(text="pong"))
        raw = await asyncio.wait_for(ws.recv(), 5)
        data = json.loads(raw)
        assert data["type"] == "info"
        assert data["text"] == "pong"
        await ws.close()
    finally:
        await tr.close()


@pytest.mark.asyncio
async def test_send_broadcasts_to_all_clients():
    """Mirroring core: every attached view receives every event."""
    tr = WebSocketServerTransport(port=0)
    await tr.start()
    try:
        ws1, _ = await _connect_and_welcome(tr.port)
        ws2, _ = await _connect_and_welcome(tr.port)

        await tr.send(msg.Info(text="to everyone"))

        for ws in (ws1, ws2):
            raw = await asyncio.wait_for(ws.recv(), 5)
            data = json.loads(raw)
            assert data["type"] == "info"
            assert data["text"] == "to everyone"

        await ws1.close()
        await ws2.close()
    finally:
        await tr.close()


@pytest.mark.asyncio
async def test_inbound_merges_from_all_clients():
    """Messages from every client land in the same receive stream."""
    tr = WebSocketServerTransport(port=0)
    await tr.start()
    try:
        ws1, id1 = await _connect_and_welcome(tr.port)
        ws2, id2 = await _connect_and_welcome(tr.port)

        await ws1.send(msg.Typing(text="abc", client_id=id1).model_dump_json())
        await ws2.send(msg.Typing(text="xyz", client_id=id2).model_dump_json())

        got: dict[str, str] = {}

        async def _drain():
            async for m in tr.receive():
                got[m.client_id] = m.text
                if len(got) == 2:
                    break

        await asyncio.wait_for(_drain(), 5)
        assert got == {id1: "abc", id2: "xyz"}
        await ws1.close()
        await ws2.close()
    finally:
        await tr.close()


@pytest.mark.asyncio
async def test_survives_client_reconnect():
    """Client disconnect must NOT end ``receive()`` — webviews reload."""
    tr = WebSocketServerTransport(port=0)
    await tr.start()
    try:
        got: list[str] = []

        async def _drain():
            async for m in tr.receive():
                got.append(m.text)
                if len(got) >= 2:
                    break

        drain_task = asyncio.create_task(_drain())

        ws1, _ = await _connect_and_welcome(tr.port)
        await ws1.send(msg.UserMessage(text="first").model_dump_json())
        await ws1.close()

        ws2, _ = await _connect_and_welcome(tr.port)
        await ws2.send(msg.UserMessage(text="second").model_dump_json())

        await asyncio.wait_for(drain_task, 5)
        assert got == ["first", "second"]
        await ws2.close()
    finally:
        await tr.close()


@pytest.mark.asyncio
async def test_send_without_client_is_noop():
    """Events emitted with no views attached are dropped, not raised."""
    tr = WebSocketServerTransport(port=0)
    await tr.start()
    try:
        await tr.send(msg.Info(text="nobody listening"))  # must not raise
    finally:
        await tr.close()


@pytest.mark.asyncio
async def test_close_unblocks_receive():
    tr = WebSocketServerTransport(port=0)
    await tr.start()

    async def _drain():
        async for _ in tr.receive():
            pass

    task = asyncio.create_task(_drain())
    await asyncio.sleep(0.05)
    await tr.close()
    await asyncio.wait_for(task, 5)
    assert tr.is_closed


# ── CompositeTransport (unix TUI + ws tabs on one BE) ───────────────


@pytest.mark.asyncio
async def test_composite_broadcasts_and_merges():
    """send() reaches children; receive() merges children's inbound."""
    ws_child = WebSocketServerTransport(port=0)
    comp = CompositeTransport([ws_child])
    await comp.start()
    try:
        ws, _ = await _connect_and_welcome(ws_child.port)

        # Inbound through the composite.
        await ws.send(msg.UserMessage(text="via child").model_dump_json())

        got = None

        async def _recv_one():
            nonlocal got
            async for m in comp.receive():
                got = m
                break

        await asyncio.wait_for(_recv_one(), 5)
        assert got is not None and got.text == "via child"

        # Outbound through the composite.
        await comp.send(msg.Info(text="fanned out"))
        raw = await asyncio.wait_for(ws.recv(), 5)
        assert json.loads(raw)["text"] == "fanned out"
        await ws.close()
    finally:
        await comp.close()


@pytest.mark.asyncio
async def test_composite_close_closes_children():
    ws_child = WebSocketServerTransport(port=0)
    comp = CompositeTransport([ws_child])
    await comp.start()
    await comp.close()
    assert comp.is_closed
    assert ws_child.is_closed


@pytest.mark.asyncio
async def test_composite_with_in_process_child():
    """Composite must work with any Transport, not just WS — the unix
    transport is used by the TUI; InProcessTransport stands in for it
    here (same interface, no real socket needed)."""
    fe_side, be_side = InProcessTransport.create_pair()
    comp = CompositeTransport([be_side])

    async def _recv_one():
        async for m in comp.receive():
            return m

    recv_task = asyncio.create_task(_recv_one())
    await asyncio.sleep(0.05)
    await fe_side.send(msg.UserMessage(text="from tui"))
    m = await asyncio.wait_for(recv_task, 5)
    assert m.text == "from tui"

    await comp.send(msg.Info(text="to tui"))

    async def _fe_recv_one():
        async for m in fe_side.receive():
            return m

    out = await asyncio.wait_for(_fe_recv_one(), 5)
    assert out.text == "to tui"
    await comp.close()
