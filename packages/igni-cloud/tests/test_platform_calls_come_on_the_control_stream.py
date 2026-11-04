"""The platform's calls are taken only from the gateway's control stream.

``deliver_service_events`` and ``vm_sign_in`` act for the producer with
nobody present. In the cloud the node agent adds ``X-Igni-Gateway: control``
to the gateway's own streams (``stream?control=1``) and strips it from
every client's; igni reads it off the connection, never off a message.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from igni_cloud.guest.credential import CredentialState, StateChange
from igni_cloud.layer import CloudLayer
from websockets.asyncio.client import connect

from ember_code.core.layer import Layer, Layers
from ember_code.protocol import messages as msg
from ember_code.transport.websocket import WebSocketServerTransport
from tests.host_model_helpers import HostRig


async def _first_message(headers: dict[str, str] | None) -> msg.Message:
    transport = WebSocketServerTransport(port=0)
    await transport.start()
    try:
        async with connect(
            f"ws://127.0.0.1:{transport.port}", additional_headers=headers
        ) as window:
            await window.recv()  # welcome
            request = msg.RPCRequest(id="r1", method="get_status")
            raw = json.loads(request.model_dump_json())
            raw["_via_control"] = True  # a client claiming it is nothing
            await window.send(json.dumps(raw))
            async for message in transport.receive():
                return message
    finally:
        await transport.close()
    raise AssertionError("nothing arrived")


async def test_only_the_connection_says_it_is_the_platforms():
    assert (await _first_message({"X-Igni-Gateway": "control"})).via_control
    assert not (await _first_message(None)).via_control


class ControlOnly(Layer):
    """The cloud layer's answer to this one seam, on a desktop backend."""

    def platform_calls_need_control(self) -> bool:
        return CloudLayer().platform_calls_need_control()


async def test_in_the_cloud_a_client_cannot_sign_the_vm_in_or_deliver_events(tmp_path: Path):
    Layers.install(ControlOnly())
    rig = await HostRig(tmp_path).start()
    asked: list[str] = []

    async def sign_in(token: str, organisation: str | None) -> Any:
        asked.append(token)
        return StateChange(state=CredentialState.READY)

    try:
        assert rig.orchestrator is not None
        rig.orchestrator.attach_vm_sign_in(sign_in)
        from_a_client = msg.RPCRequest(id="c", method="vm_sign_in", args={"session_token": "s"})
        await rig.orchestrator.dispatch(from_a_client)
        events = msg.RPCRequest(id="e", method="deliver_service_events", args={"events": []})
        await rig.orchestrator.dispatch(events)
        from_the_gateway = msg.RPCRequest(id="g", method="vm_sign_in", args={"session_token": "s"})
        from_the_gateway.mark_control()
        await rig.orchestrator.dispatch(from_the_gateway)
        answers = {m.id: m for m in rig.windows.of(msg.RPCResponse)}
        assert "control connection" in (answers["c"].error or "")
        assert "control connection" in (answers["e"].error or "")
        assert answers["g"].result == {"state": "ready", "message": ""}
        assert asked == ["s"]
    finally:
        await rig.stop()
