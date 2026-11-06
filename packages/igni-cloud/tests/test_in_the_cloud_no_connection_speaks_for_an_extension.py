"""In the cloud, no connection speaks for an extension.

Every client of a VM's igni is the producer, through Fleet's gateway —
igni's web UI, or a program holding their token — and none of them hosts
an extension's page. So a message naming an extension (``caller``) is
refused before any handler hears it: no host-model grants, no ``ext.*``
call, and above all no token for the extension's service minted in the
producer's name. Not from the gateway's control stream either.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from igni_cloud.layer import CloudLayer

from ember_code.core.layer import Layer, Layers
from ember_code.protocol import messages as msg
from tests.host_model_helpers import HostRig


class NoPagesHere(Layer):
    """The cloud layer's answer to this one seam, on a desktop backend."""

    def connections_speak_for_extensions(self) -> bool:
        return CloudLayer().connections_speak_for_extensions()


def test_the_cloud_layer_lets_no_connection_speak_for_an_extension():
    assert CloudLayer().connections_speak_for_extensions() is False


async def test_a_page_s_calls_are_refused_before_any_handler(tmp_path: Path, monkeypatch):
    Layers.install(NoPagesHere())
    rig = await HostRig(tmp_path).start()
    reached: list[str] = []
    try:
        assert rig.orchestrator is not None and rig.model is not None
        page = rig.install("acme.reviewer", host=["sessions"], rpc=["ext.acme.reviewer.*"])

        async def handle_page(caller: str, args: dict[str, Any]) -> Any:
            reached.append("host_model")
            return {}

        monkeypatch.setattr(rig.model, "handle_page", handle_page)

        requests = [
            msg.RPCRequest(id="h", method="host_model", caller=page, args={"op": "get"}),
            msg.RPCRequest(id="t", method="extension_service_token", caller=page),
            msg.RPCRequest(id="x", method="ext.acme.reviewer.search", caller=page),
        ]
        from_the_gateway = msg.RPCRequest(id="g", method="extension_service_token", caller=page)
        from_the_gateway.mark_control()
        for request in [*requests, from_the_gateway]:
            request.mark_from("ws-producer")
            await rig.orchestrator.dispatch(request)

        answers = {m.id: m for m in rig.windows.of(msg.RPCResponse)}
        for request_id in ("h", "t", "x", "g"):
            assert "no connection to this igni speaks for an extension" in (
                answers[request_id].error or ""
            ), request_id
            assert answers[request_id].to_client == "ws-producer"
        assert reached == []
    finally:
        await rig.stop()
