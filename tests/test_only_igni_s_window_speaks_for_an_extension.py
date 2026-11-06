"""Only igni's window speaks for an extension, and its answers go to that window alone.

``RPCRequest.caller`` names the extension a call is made for. igni's
window sets it for the page it hosts (``IgniClient.asExtension``) — but
it is a field of the message, and the backend trusted it: any client of
the socket could name any installed extension and get its host-model
grants, call its ``ext.*`` methods as the person, and be handed the
person's token for its service. Every answer was broadcast, too, so a
page's service token reached every client attached.

Now:

* every message carries the connection it arrived on, stamped by the
  transport (``Message.from_client``), never read from the message;
* a ``caller`` is honoured only where the layer says a connection may
  speak for an extension (never in the cloud — see igni-cloud's tests),
  for an extension that is installed, and only for what its page may
  call: its own namespace, the host model, its service token, and base
  methods its manifest grants — never igni's pool-level calls;
* an answer that carries a secret or one extension's data is addressed
  to the connection that asked (``Message.to_client``), and the transport
  sends it to that connection only;
* a window's connection, and the window framing a page, are the
  server's to know, not the request's to say.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

from ember_code.protocol import messages as msg
from ember_code.protocol.rpc import RpcMethod
from ember_code.transport.in_process import InProcessTransport
from ember_code.transport.websocket import CompositeTransport, WebSocketServerTransport
from tests.host_model_helpers import HostRig

# ── The transport ─────────────────────────────────────────────────


async def _welcomed(port: int) -> tuple[Any, str]:
    socket = await connect(f"ws://127.0.0.1:{port}")
    welcome = json.loads(await asyncio.wait_for(socket.recv(), 5))
    return socket, welcome["client_id"]


async def _nothing_arrives(socket: Any, within: float = 0.2) -> bool:
    try:
        await asyncio.wait_for(socket.recv(), within)
    except TimeoutError:
        return True
    return False


async def test_a_message_carries_the_connection_it_came_on():
    transport = WebSocketServerTransport(port=0)
    await transport.start()
    try:
        socket, client_id = await _welcomed(transport.port)
        raw = json.loads(msg.RPCRequest(id="r", method="get_status").model_dump_json())
        raw["_from_client"] = "ws-somebody-else"  # a client claiming a connection is nothing
        await socket.send(json.dumps(raw))
        async for message in transport.receive():
            assert message.from_client == client_id
            break
        await socket.close()
    finally:
        await transport.close()


async def test_a_reply_addressed_to_a_window_reaches_only_that_window():
    transport = WebSocketServerTransport(port=0)
    await transport.start()
    try:
        asker, asker_id = await _welcomed(transport.port)
        other, _ = await _welcomed(transport.port)

        await transport.send(msg.RPCResponse(id="t", result="secret").address_to(asker_id))

        assert json.loads(await asyncio.wait_for(asker.recv(), 5))["result"] == "secret"
        assert await _nothing_arrives(other)
        await transport.send(msg.Info(text="for everyone"))
        assert json.loads(await asyncio.wait_for(other.recv(), 5))["text"] == "for everyone"
        await asker.close()
        await other.close()
    finally:
        await transport.close()


async def test_a_reply_to_a_window_that_left_is_dropped():
    transport = WebSocketServerTransport(port=0)
    await transport.start()
    try:
        other, _ = await _welcomed(transport.port)

        await transport.send(msg.RPCResponse(id="t", result="secret").address_to("ws-gone"))

        assert await _nothing_arrives(other)
        await other.close()
    finally:
        await transport.close()


def test_an_address_survives_a_copy():
    # The session stamp and the event-sequence stamp both copy the message.
    addressed = msg.RPCResponse(id="t", result="x").address_to("ws-1")

    assert addressed.model_copy(update={"session_id": "s"}).to_client == "ws-1"
    assert msg.RPCResponse(id="t").address_to("").to_client == ""


async def test_the_terminal_never_sees_a_reply_addressed_to_a_window():
    socket_side = WebSocketServerTransport(port=0)
    terminal_view, terminal = InProcessTransport.create_pair()
    composite = CompositeTransport([socket_side, terminal])
    await composite.start()
    try:
        window, window_id = await _welcomed(socket_side.port)

        await composite.send(msg.RPCResponse(id="t", result="secret").address_to(window_id))

        assert json.loads(await asyncio.wait_for(window.recv(), 5))["result"] == "secret"
        try:
            await asyncio.wait_for(anext(terminal_view.receive()), 0.2)
            reached_the_terminal = True
        except TimeoutError:
            reached_the_terminal = False
        assert not reached_the_terminal
        assert composite.owns_client(window_id) and not composite.owns_client("ws-gone")
        await window.close()
    finally:
        await composite.close()


# ── The orchestrator ──────────────────────────────────────────────


def _from(client_id: str, method: str, *, caller: str | None = None, **args: Any) -> Any:
    request = msg.RPCRequest(id=f"{method}-{client_id}", method=method, args=args, caller=caller)
    request.mark_from(client_id)
    return request


def _answer(rig: HostRig, request_id: str) -> Any:
    return next(m for m in rig.windows.sent if getattr(m, "id", "") == request_id)


async def test_a_host_model_answer_reaches_only_the_window_that_asked(tmp_path: Path):
    rig = await HostRig(tmp_path).start()
    try:
        assert rig.orchestrator is not None
        page = rig.install("acme.reviewer", host=["sessions"])

        await rig.orchestrator.dispatch(
            _from("ws-a", RpcMethod.HOST_MODEL, caller=page, op="get", path="sessions.list")
        )

        assert _answer(rig, "host_model-ws-a").to_client == "ws-a"
    finally:
        await rig.stop()


async def test_a_page_s_service_token_reaches_only_the_window_that_asked(tmp_path: Path):
    rig = await HostRig(tmp_path).start()
    try:
        assert rig.orchestrator is not None
        page = rig.install("acme.reviewer")

        await rig.orchestrator.dispatch(
            _from("ws-a", RpcMethod.EXTENSION_SERVICE_TOKEN, caller=page)
        )

        assert _answer(rig, "extension_service_token-ws-a").to_client == "ws-a"
    finally:
        await rig.stop()


async def test_an_extension_call_s_answer_reaches_only_the_window_that_asked(tmp_path: Path):
    rig = await HostRig(tmp_path).start()
    try:
        assert rig.orchestrator is not None
        page = rig.install("acme.reviewer", rpc=["ext.acme.reviewer.*"])

        await rig.orchestrator.dispatch(_from("ws-a", "ext.acme.reviewer.search", caller=page))

        assert _answer(rig, "ext.acme.reviewer.search-ws-a").to_client == "ws-a"
    finally:
        await rig.stop()


async def test_igni_s_own_calls_are_still_broadcast(tmp_path: Path):
    rig = await HostRig(tmp_path).start()
    try:
        assert rig.orchestrator is not None

        await rig.orchestrator.dispatch(_from("ws-a", RpcMethod.GET_SESSION_ID))

        assert _answer(rig, "get_session_id-ws-a").to_client == ""
    finally:
        await rig.stop()


async def test_a_caller_that_is_not_installed_is_refused(tmp_path: Path):
    rig = await HostRig(tmp_path).start()
    try:
        assert rig.orchestrator is not None

        await rig.orchestrator.dispatch(
            _from("ws-a", RpcMethod.EXTENSION_SERVICE_TOKEN, caller="acme.nobody")
        )

        answer = _answer(rig, "extension_service_token-ws-a")
        assert "no extension 'acme.nobody' is installed" in (answer.error or "")
        assert answer.to_client == "ws-a"
    finally:
        await rig.stop()


async def test_a_page_may_call_a_base_method_only_if_its_manifest_grants_it(tmp_path: Path):
    rig = await HostRig(tmp_path).start()
    try:
        assert rig.orchestrator is not None
        granted = rig.install("acme.granted", rpc=["get_session_id"])
        not_granted = rig.install("acme.plain")

        await rig.orchestrator.dispatch(_from("ws-a", RpcMethod.GET_SESSION_ID, caller=granted))
        await rig.orchestrator.dispatch(_from("ws-b", RpcMethod.GET_SESSION_ID, caller=not_granted))

        allowed = _answer(rig, "get_session_id-ws-a")
        refused = _answer(rig, "get_session_id-ws-b")
        assert allowed.error is None and allowed.to_client == "ws-a"
        assert "'acme.plain' is not permitted to call 'get_session_id'" in (refused.error or "")
    finally:
        await rig.stop()


async def test_a_page_never_reaches_a_pool_level_method(tmp_path: Path):
    rig = await HostRig(tmp_path).start()
    try:
        assert rig.orchestrator is not None
        # Granted in the manifest, and still refused: sessions and windows are igni's.
        page = rig.install("acme.reviewer", rpc=["attach_session", "window_model"])

        await rig.orchestrator.dispatch(_from("ws-a", RpcMethod.ATTACH_SESSION, caller=page))
        await rig.orchestrator.dispatch(
            _from("ws-b", RpcMethod.WINDOW_MODEL, caller=page, op="hydrate", window="w")
        )

        for request_id, method in (
            ("attach_session-ws-a", "attach_session"),
            ("window_model-ws-b", "window_model"),
        ):
            assert f"an extension's page does not reach '{method}'" in (
                _answer(rig, request_id).error or ""
            )
    finally:
        await rig.stop()


async def test_a_window_s_connection_is_the_one_it_spoke_on(tmp_path: Path):
    rig = await HostRig(tmp_path).start()
    try:
        assert rig.orchestrator is not None and rig.model is not None

        await rig.orchestrator.dispatch(
            _from("ws-a", RpcMethod.WINDOW_MODEL, op="hydrate", window="w-1", connection_id="ws-x")
        )

        assert rig.model.windows.by_connection("ws-a") == "w-1"
        assert rig.model.windows.by_connection("ws-x") is None
    finally:
        await rig.stop()


async def test_a_page_s_home_window_is_the_window_framing_it(tmp_path: Path, monkeypatch):
    rig = await HostRig(tmp_path).start()
    try:
        assert rig.orchestrator is not None and rig.model is not None
        page = rig.install("acme.reviewer", host=["ui"])
        await rig.orchestrator.dispatch(
            _from("ws-a", RpcMethod.WINDOW_MODEL, op="hydrate", window="w-1")
        )
        seen: list[dict[str, Any]] = []
        real = rig.model.handle_page

        async def recording(caller: str, args: dict[str, Any]) -> Any:
            seen.append(dict(args))
            return await real(caller, args)

        monkeypatch.setattr(rig.model, "handle_page", recording)

        await rig.orchestrator.dispatch(
            _from(
                "ws-a",
                RpcMethod.HOST_MODEL,
                caller=page,
                op="get",
                path="ui.route",
                home_window="w-other",
            )
        )

        assert seen[0]["home_window"] == "w-1"
    finally:
        await rig.stop()
