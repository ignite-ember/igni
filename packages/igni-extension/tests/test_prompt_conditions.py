"""`ext.prompt.hold`: a manifest's `when`, said by the extension, over a real MCP session."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_extension import IgniExtension
from igni_extension.notifications import ConditionsParams
from igni_extension.testing import HostHarness


async def test_a_condition_crosses_the_wire_as_the_host_reads_it() -> None:
    ext = IgniExtension("acme.example", environ={})

    async with HostHarness(ext) as host:
        await ext.prompt.hold("index-covers-head", True)
        assert await host.conditions_said(1) == [{"index-covers-head": True}]

    assert host.raw == [
        {
            "jsonrpc": "2.0",
            "method": "notifications/igni/conditions",
            "params": {"conditions": {"index-covers-head": True}},
        }
    ]


async def test_saying_the_same_again_sends_nothing() -> None:
    ext = IgniExtension("acme.example", environ={})

    async with HostHarness(ext) as host:
        await ext.prompt.hold("ready", True)
        await ext.prompt.hold("ready", True)
        await ext.prompt.set({"ready": True, "fresh": False})
        assert await host.conditions_said(2) == [{"ready": True}, {"fresh": False}]
    assert ext.prompt.holding == {"ready": True, "fresh": False}


async def test_what_is_said_before_the_host_connects_arrives_when_it_does() -> None:
    """An extension says what holds as it starts, before igni's handshake."""
    ext = IgniExtension("acme.example", environ={})
    await ext.prompt.hold("ready")

    async with HostHarness(ext) as host:
        assert await host.conditions_said(1) == [{"ready": True}]


async def test_refresh_asks_igni_to_fetch_the_text_again() -> None:
    ext = IgniExtension("acme.example", environ={})

    async with HostHarness(ext) as host:
        await ext.prompt.refresh()
        await host.conditions_said(1)

    assert host.raw[-1]["params"] == {"conditions": {}, "refresh": True}


@pytest.mark.parametrize("name", ["Ready", "index covers head", "", "1st"])
def test_a_name_no_manifest_could_use_is_refused_where_it_is_written(name: str) -> None:
    with pytest.raises(ValidationError):
        ConditionsParams(conditions={name: True})
