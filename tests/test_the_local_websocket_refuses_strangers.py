"""The backend's local WebSocket lets in igni's own clients and nobody else.

Browsers do not apply CORS to WebSockets, so without a handshake check
any web page could drive the agent on ``ws://127.0.0.1:<port>``. The
desktop shell hands the backend a per-launch token (``IGNI_WS_TOKEN``);
its webview offers it as the subprotocol ``igni-token.<token>`` next to
``igni.v1``. Foreign Origins are refused whether or not there is a token.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from ember_code.backend.lockfile import Lockfile
from ember_code.backend.schemas_lockfile import LockfilePayload
from ember_code.protocol import messages as msg
from ember_code.transport.websocket import WebSocketServerTransport
from ember_code.transport.ws_auth import (
    DEV_NO_TOKEN_ENV,
    TOKEN_ENV,
    origin_allowed,
    take_token_from_environment,
)

TOKEN = "s3cr3t-Per_Launch-token-0123456789abcdefghij"


def _protocols(token: str | None) -> list[str]:
    return ["igni.v1"] if token is None else ["igni.v1", f"igni-token.{token}"]


async def _open(port: int, *, token: str | None = None, origin: str | None = None, headers=None):
    return await connect(
        f"ws://127.0.0.1:{port}",
        subprotocols=_protocols(token),
        origin=origin,
        additional_headers=headers,
    )


async def _refused(port: int, **kw) -> int:
    with pytest.raises(InvalidStatus) as exc:
        ws = await _open(port, **kw)
        await ws.close()
    return exc.value.response.status_code


async def _welcomed(ws) -> dict:
    data = json.loads(await asyncio.wait_for(ws.recv(), 5))
    assert data["type"] == "welcome"
    return data


@pytest.fixture
async def guarded():
    tr = WebSocketServerTransport(port=0, token=TOKEN)
    await tr.start()
    try:
        yield tr
    finally:
        await tr.close()


@pytest.fixture
async def open_transport():
    tr = WebSocketServerTransport(port=0)
    await tr.start()
    try:
        yield tr
    finally:
        await tr.close()


# ── With the launcher's token ───────────────────────────────────


async def test_without_the_token_it_is_refused(guarded):
    assert await _refused(guarded.port) == 403
    assert await _refused(guarded.port, origin="tauri://localhost") == 403
    assert guarded.client_count == 0


async def test_with_a_wrong_token_it_is_refused(guarded):
    assert await _refused(guarded.port, token="not-it", origin="tauri://localhost") == 403
    assert await _refused(guarded.port, token=TOKEN[:-1]) == 403


async def test_from_a_foreign_origin_it_is_refused_even_with_the_token(guarded):
    for origin in ("https://evil.example", "http://127.0.0.1.evil.example", "null"):
        assert await _refused(guarded.port, token=TOKEN, origin=origin) == 403, origin


@pytest.mark.parametrize(
    "origin",
    ["tauri://localhost", "http://tauri.localhost", "https://tauri.localhost", None],
)
async def test_with_the_token_and_igni_s_origin_it_is_accepted(guarded, origin):
    ws = await _open(guarded.port, token=TOKEN, origin=origin)
    try:
        await _welcomed(ws)
        # The server answers igni.v1 and never echoes the token back.
        assert ws.subprotocol == "igni.v1"
    finally:
        await ws.close()


async def test_the_control_header_does_not_stand_in_for_the_token(guarded):
    # On a desktop anybody can send this header.
    assert await _refused(guarded.port, headers={"X-Igni-Gateway": "control"}) == 403


async def test_a_refusal_is_logged_without_the_token(guarded, caplog):
    caplog.set_level(logging.WARNING, logger="ember_code.transport.ws_auth")
    await _refused(guarded.port, token=TOKEN + "x")
    assert "wrong token" in caplog.text
    assert TOKEN not in caplog.text


# ── Without a token (hand-started, IDE plugin, cloud) ───────────


async def test_without_a_token_a_foreign_origin_is_still_refused(open_transport):
    assert await _refused(open_transport.port, origin="https://evil.example") == 403
    assert await _refused(open_transport.port, origin="null") == 403


async def test_without_a_token_a_non_browser_client_connects(open_transport):
    ws = await connect(f"ws://127.0.0.1:{open_transport.port}")
    try:
        await _welcomed(ws)
        assert ws.subprotocol is None
    finally:
        await ws.close()


async def test_the_gateway_control_stream_still_works(open_transport):
    """In the cloud the gateway authenticates clients; its own stream is
    marked by ``X-Igni-Gateway: control`` (added by the node agent)."""
    ws = await connect(
        f"ws://127.0.0.1:{open_transport.port}",
        additional_headers={"X-Igni-Gateway": "control"},
    )
    try:
        await _welcomed(ws)
        await ws.send(msg.UserMessage(text="hi", id="c1").model_dump_json())

        async def first():
            async for m in open_transport.receive():
                return m

        got = await asyncio.wait_for(first(), 5)
        assert got.via_control
    finally:
        await ws.close()


async def test_the_control_stream_with_a_token_offers_it_too(guarded):
    ws = await _open(guarded.port, token=TOKEN, headers={"X-Igni-Gateway": "control"})
    try:
        await _welcomed(ws)
    finally:
        await ws.close()


# ── Origins, the environment, the lockfile ──────────────────────


@pytest.mark.parametrize(
    ("origin", "allowed"),
    [
        ("tauri://localhost", True),
        ("http://tauri.localhost", True),
        ("https://tauri.localhost", True),
        ("http://localhost:5179", True),
        ("http://127.0.0.1:61234", True),
        ("http://[::1]:8080", True),
        ("vscode-webview://1a2b3c", True),
        ("https://evil.example", False),
        ("http://localhost.evil.example", False),
        ("tauri://evil", False),
        ("file://", False),
        ("null", False),
        ("", False),
        ("http://localhost:notaport", False),
    ],
)
def test_which_origins_are_igni_s_own(origin, allowed):
    assert origin_allowed(origin) is allowed


def test_the_token_is_taken_out_of_the_environment():
    env = {TOKEN_ENV: TOKEN}
    assert take_token_from_environment(env) == TOKEN
    # Shell tools, hooks and MCP servers inherit the environment.
    assert TOKEN_ENV not in env


def test_the_dev_switch_drops_the_token_loudly(caplog):
    env = {TOKEN_ENV: TOKEN, DEV_NO_TOKEN_ENV: "1"}
    with caplog.at_level(logging.WARNING, logger="ember_code.transport.ws_auth"):
        assert take_token_from_environment(env) is None
    assert DEV_NO_TOKEN_ENV in caplog.text
    assert TOKEN_ENV not in env


def test_no_token_in_the_environment_means_none():
    assert take_token_from_environment({}) is None
    assert take_token_from_environment({TOKEN_ENV: "  "}) is None


def test_the_lockfile_carries_the_token_owner_only(tmp_path):
    lock = Lockfile(tmp_path)
    assert lock.write(LockfilePayload.now(pid=1, port=2, wire_version="v", ws_token=TOKEN)).ok
    data = json.loads(lock.path.read_text())
    assert data["ws_token"] == TOKEN
    if os.name == "posix":
        assert stat.S_IMODE(lock.path.stat().st_mode) == 0o600
    assert lock.read().ws_token == TOKEN


def test_without_a_token_the_lockfile_keeps_its_old_keys(tmp_path):
    lock = Lockfile(tmp_path)
    assert lock.write(LockfilePayload.now(pid=1, port=2, wire_version="v")).ok
    assert set(json.loads(lock.path.read_text())) == {"pid", "port", "wire_version", "created_at"}
