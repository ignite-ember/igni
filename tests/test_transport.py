"""Tests for the transport layer — in-process and Unix socket."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from ember_code.protocol.messages import ContentDelta, Info, UserMessage
from ember_code.protocol.registry import MessageRegistry
from ember_code.transport.in_process import InProcessTransport
from ember_code.transport.unix_socket import (
    UnixSocketClientTransport,
    UnixSocketServerTransport,
)


class TestInProcessTransport:
    @pytest.mark.asyncio
    async def test_send_receive(self):
        fe, be = InProcessTransport.create_pair()
        msg = ContentDelta(text="hello")
        await fe.send(msg)
        received = []
        # Close after a short delay to stop the receive loop
        asyncio.get_event_loop().call_later(0.1, lambda: asyncio.ensure_future(fe.close()))
        async for m in be.receive():
            received.append(m)
            await be.close()
        assert len(received) == 1
        assert isinstance(received[0], ContentDelta)
        assert received[0].text == "hello"

    @pytest.mark.asyncio
    async def test_bidirectional(self):
        fe, be = InProcessTransport.create_pair()
        await fe.send(UserMessage(text="from fe"))
        await be.send(ContentDelta(text="from be"))

        # Read one from each side
        be_received = await be._recv_q.get()
        fe_received = await fe._recv_q.get()

        assert isinstance(be_received, UserMessage)
        assert be_received.text == "from fe"
        assert isinstance(fe_received, ContentDelta)
        assert fe_received.text == "from be"

    @pytest.mark.asyncio
    async def test_close_sends_sentinel(self):
        fe, be = InProcessTransport.create_pair()
        await fe.close()
        assert fe.is_closed
        # BE should receive None (sentinel)
        sentinel = await be._recv_q.get()
        assert sentinel is None

    @pytest.mark.asyncio
    async def test_send_after_close_is_noop(self):
        fe, be = InProcessTransport.create_pair()
        await fe.close()
        await fe.send(Info(text="should be dropped"))
        # Queue has only the sentinel (None) from close(), not the dropped message
        sentinel = await be._recv_q.get()
        assert sentinel is None
        assert be._recv_q.empty()


class TestUnixSocketTransport:
    @pytest.mark.asyncio
    async def test_send_receive_over_socket(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "test.sock"

            server = UnixSocketServerTransport(sock_path)
            await server.start()

            client = UnixSocketClientTransport(sock_path)
            await client.connect()
            await server.wait_for_connection()

            # Client → Server
            await client.send(UserMessage(text="hello from FE"))
            received = []
            async for msg in server.receive():
                received.append(msg)
                break  # Just get one
            assert len(received) == 1
            assert isinstance(received[0], UserMessage)
            assert received[0].text == "hello from FE"

            # Server → Client
            await server.send(ContentDelta(text="hello from BE"))
            async for msg in client.receive():
                received.append(msg)
                break
            assert len(received) == 2
            assert isinstance(received[1], ContentDelta)
            assert received[1].text == "hello from BE"

            await client.close()
            await server.close()

    @pytest.mark.asyncio
    async def test_multiple_messages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "test.sock"

            server = UnixSocketServerTransport(sock_path)
            await server.start()

            client = UnixSocketClientTransport(sock_path)
            await client.connect()
            await server.wait_for_connection()

            # Send 5 messages
            for i in range(5):
                await client.send(UserMessage(text=f"msg {i}"))

            received = []
            count = 0
            async for msg in server.receive():
                received.append(msg)
                count += 1
                if count >= 5:
                    break

            assert len(received) == 5
            for i, msg in enumerate(received):
                assert msg.text == f"msg {i}"

            await client.close()
            await server.close()

    @pytest.mark.asyncio
    async def test_deserialize_unknown_type(self):
        result = MessageRegistry.default().deserialize('{"type": "unknown_type", "payload": {}}')
        assert result is None

    @pytest.mark.asyncio
    async def test_deserialize_invalid_json(self):
        result = MessageRegistry.default().deserialize("not json at all")
        assert result is None

    @pytest.mark.asyncio
    async def test_deserialize_valid_message(self):
        result = MessageRegistry.default().deserialize(
            '{"type": "content_delta", "text": "hello", "is_thinking": false}'
        )
        assert isinstance(result, ContentDelta)
        assert result.text == "hello"

    @pytest.mark.asyncio
    async def test_socket_cleanup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "test.sock"
            server = UnixSocketServerTransport(sock_path)
            await server.start()
            assert sock_path.exists()
            await server.close()
            assert not sock_path.exists()
