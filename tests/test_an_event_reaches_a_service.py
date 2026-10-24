"""An extension with no page can still be told something happened.

Delivery finished at an iframe. Everything about that was right for
an extension with a page, and an extension *without* one heard
nothing at all — while one whose page was closed heard nothing
until the user opened it. CodeIndex is the case that makes it
concrete: its panel can redraw on `workspace.head-changed`, but the
part that would actually stop polling is its service, and no event
could reach it.

MCP runs host→server, so a service cannot be pushed to. It is
called instead, on the reserved `igni_on_event`.

The end-to-end half of this file uses a real subprocess, for the
reason the install test does: a stub proves the wiring agrees with
itself. `tests/fixtures/dev_extension/server.py` implements the
sink and writes what it is told to a file, so what is asserted here
is what another process actually received.

The rest is about what a *slow* service must not be able to do to
the host. An event producer runs on a timer, so "this service takes
thirty seconds" and "this service never answers" are states the
host has to survive without stalling, without growing, and without
turning one slow sink into a hundred concurrent calls.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from ember_code.core.mcp.client import MCPClientManager
from ember_code.core.mcp.naming import bare_tool_name
from ember_code.extensions.events import EVENT_SINK_TOOL, HostTopic
from ember_code.extensions.installer import ExtensionInstaller, server_name
from ember_code.extensions.manifest import ExtensionManifest
from ember_code.extensions.origin import ExtensionOrigins
from ember_code.extensions.permissions import Tools
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState
from ember_code.extensions.rpc import ExtensionRpc
from ember_code.extensions.service_events import ServiceEvents

FIXTURE = Path(__file__).parent / "fixtures" / "dev_extension"


def bundle_at(tmp_path: Path, **manifest_changes: object) -> Path:
    bundle = tmp_path / "ext"
    shutil.copytree(FIXTURE, bundle)
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["dev"]["command"] = [sys.executable, "server.py"]
    manifest.setdefault("permissions", {})["events"] = ["workspace.*"]
    manifest.update(manifest_changes)
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    return bundle


# ── The round trip, through a real subprocess ──────────────────────


class Wired:
    def __init__(self, tmp_path: Path) -> None:
        self.registry = ExtensionRegistry()
        self.rpc = ExtensionRpc(self.registry)
        self.mcp = MCPClientManager(project_dir=tmp_path)
        self.origins = ExtensionOrigins()
        self.installer = ExtensionInstaller(
            registry=self.registry,
            rpc=self.rpc,
            mcp=self.mcp,
            origins=self.origins,
            allow_development=True,
        )
        self.service_events = ServiceEvents(rpc=self.rpc)

    async def close(self) -> None:
        await self.service_events.stop()
        await self.origins.stop_all()
        await self.mcp.disconnect_all()


@pytest.fixture
async def wired(tmp_path: Path):
    w = Wired(tmp_path)
    try:
        yield w
    finally:
        await w.close()


def told(bundle: Path) -> list[dict[str, Any]]:
    """What the fixture server wrote down, from its own process."""
    log = bundle / "events.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_a_real_service_is_told(wired, tmp_path):
    """The one that could not have been faked.

    Another process receives the topic and the payload and writes
    them to disk; this reads them back.
    """
    bundle = bundle_at(tmp_path)
    entry = await wired.installer.install(bundle)
    assert entry.state is ExtensionState.READY, entry.reason

    delivered = await wired.rpc.notify(
        "acme.scanner",
        HostTopic.WORKSPACE_HEAD_CHANGED,
        {"commit_sha": "abc", "branch": "main"},
    )
    assert delivered is True
    assert told(bundle) == [
        {
            "topic": "workspace.head-changed",
            "payload": {"commit_sha": "abc", "branch": "main"},
        }
    ]


@pytest.mark.asyncio
async def test_the_relay_drains_in_order(wired, tmp_path):
    """Queued, not raced. Two events arrive as two, in order."""
    bundle = bundle_at(tmp_path)
    await wired.installer.install(bundle)

    wired.service_events.tell("acme.scanner", HostTopic.WORKSPACE_HEAD_CHANGED, {"n": 1})
    wired.service_events.tell("acme.scanner", HostTopic.WORKSPACE_FILES_CHANGED, {"n": 2})

    for _ in range(100):
        await asyncio.sleep(0.05)
        if len(told(bundle)) == 2:
            break
    assert [one["payload"]["n"] for one in told(bundle)] == [1, 2]


@pytest.mark.asyncio
async def test_the_sink_is_never_offered_to_the_agent(wired, tmp_path):
    """A model that could call it could tell an extension that HEAD
    moved when it had not. A control channel the agent can drive is
    not a control channel."""
    bundle = bundle_at(tmp_path, permissions={"tools": [EVENT_SINK_TOOL, "list"]})
    await wired.installer.install(bundle)
    server = server_name("acme.scanner")
    disabled = {bare_tool_name(server, name) for name in wired.mcp.get_disabled_tools(server)}
    assert EVENT_SINK_TOOL in disabled
    # Declaring it is not a way to hand it over, and the tool beside
    # it in the same list still gets through — so the assertion above
    # is about the reserved name and not about everything being off.
    assert "list" not in disabled


@pytest.mark.asyncio
async def test_a_declared_tool_actually_reaches_the_agent(wired, tmp_path):
    """The bug this file found on the way.

    `get_tools` returns registered names (`mcp_ext:acme.scanner_list`)
    and a manifest declares the server's own (`list`). `_restrict_tools`
    compared them directly, so nothing matched and *every* tool was
    disabled — the declared ones with the rest. It looked like working
    enforcement because the failure is in the safe direction, and the
    unit test agreed because it passed bare names, which is not what
    the installer hands it.

    A real server is the only thing that could have shown this, which
    is the second time that has been true in this subsystem.
    """
    bundle = bundle_at(tmp_path, permissions={"tools": ["acme.scanner.list", "status"]})
    await wired.installer.install(bundle)
    server = server_name("acme.scanner")
    disabled = {bare_tool_name(server, name) for name in wired.mcp.get_disabled_tools(server)}
    assert disabled == {EVENT_SINK_TOOL, "policy_summary", "compliance", "explode"}


@pytest.mark.asyncio
async def test_igni_can_still_call_what_the_agent_cannot(wired, tmp_path):
    """Disabling is about agent registration, not reachability —
    otherwise the rule above would have switched the feature off."""
    bundle = bundle_at(tmp_path)
    await wired.installer.install(bundle)
    assert await wired.rpc.notify("acme.scanner", HostTopic.SESSION_SWITCHED, {"session_id": "s1"})


# ── What a slow or absent service must not be able to do ───────────


def manifest(extension_id: str = "acme.one") -> ExtensionManifest:
    return ExtensionManifest.model_validate(
        {
            "id": extension_id,
            "version": "1.0.0",
            "contract": ">=1.0 <2",
            "trust": "unsigned",
            "dev": {"command": ["true"]},
            "permissions": {"events": ["workspace.*"]},
        }
    )


class SlowService:
    """A sink that answers when the test lets it."""

    def __init__(self, *, offers_sink: bool = True) -> None:
        self.gate = asyncio.Event()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.started = asyncio.Event()
        self.concurrent = 0
        self.most_concurrent = 0
        self._offers = offers_sink

    def offers(self, tool: str) -> bool:
        return self._offers and tool == EVENT_SINK_TOOL

    async def call(self, method: str, args: dict[str, Any]) -> Any:
        self.calls.append((method, args))
        self.concurrent += 1
        self.most_concurrent = max(self.most_concurrent, self.concurrent)
        self.started.set()
        try:
            await self.gate.wait()
        finally:
            self.concurrent -= 1
        return "ok"


def relay(service: Any, extension_id: str = "acme.one") -> tuple[ServiceEvents, ExtensionRpc]:
    registry = ExtensionRegistry()
    registry.install(manifest(extension_id), state=ExtensionState.READY)
    rpc = ExtensionRpc(registry)
    rpc.bind(extension_id, service)
    return ServiceEvents(rpc=rpc), rpc


@pytest.mark.asyncio
async def test_one_call_is_in_flight_at_a_time():
    """A slow service turned concurrent by a fast producer is the
    same bug with more sockets."""
    service = SlowService()
    events, _ = relay(service)
    for n in range(5):
        events.tell("acme.one", HostTopic.WORKSPACE_FILES_CHANGED, {"n": n})
    await asyncio.wait_for(service.started.wait(), timeout=1)
    await asyncio.sleep(0.05)
    assert service.most_concurrent == 1
    assert len(service.calls) == 1
    service.gate.set()
    await events.stop()


@pytest.mark.asyncio
async def test_the_queue_is_bounded_and_says_so():
    """Unbounded is a leak with a politer name. An extension that is
    missing events should be able to find out that it is."""
    service = SlowService()
    events, _ = relay(service)
    for n in range(ServiceEvents.DEPTH + 10):
        events.tell("acme.one", HostTopic.WORKSPACE_FILES_CHANGED, {"n": n})
    await asyncio.wait_for(service.started.wait(), timeout=1)
    assert events.pending("acme.one") <= ServiceEvents.DEPTH
    assert events.dropped("acme.one") > 0
    service.gate.set()
    await events.stop()


@pytest.mark.asyncio
async def test_a_service_without_the_sink_is_not_called():
    """Most extensions will not implement it, and that is the
    expected case rather than a fault."""
    service = SlowService(offers_sink=False)
    events, rpc = relay(service)
    assert await rpc.notify("acme.one", HostTopic.SESSION_SWITCHED, {"session_id": "s"}) is False
    assert service.calls == []


@pytest.mark.asyncio
async def test_an_extension_that_is_not_ready_is_not_called():
    """A FAILED service is one whose process may be gone. Telling it
    things is how a restart loop starts."""
    service = SlowService()
    events, rpc = relay(service)
    rpc.registry.set_state("acme.one", ExtensionState.FAILED, "it died")
    assert await rpc.notify("acme.one", HostTopic.SESSION_SWITCHED, {"session_id": "s"}) is False
    assert service.calls == []


@pytest.mark.asyncio
async def test_stop_does_not_wait_for_a_service_that_is_not_answering():
    """Shutdown must not be held open by the service most likely to
    be mid-call: the one that is not answering."""
    service = SlowService()
    events, _ = relay(service)
    events.tell("acme.one", HostTopic.WORKSPACE_FILES_CHANGED, {})
    await asyncio.wait_for(service.started.wait(), timeout=1)
    await asyncio.wait_for(events.stop(), timeout=1)


@pytest.mark.asyncio
async def test_a_failing_delivery_does_not_stop_the_next():
    """One event that could not be delivered is not a reason the
    next one is not tried."""

    class Flaky:
        def __init__(self) -> None:
            self.calls = 0

        def offers(self, tool: str) -> bool:
            return True

        async def call(self, method: str, args: dict[str, Any]) -> Any:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("not today")
            return "ok"

    service = Flaky()
    events, _ = relay(service)
    events.tell("acme.one", HostTopic.WORKSPACE_FILES_CHANGED, {"n": 1})
    events.tell("acme.one", HostTopic.WORKSPACE_FILES_CHANGED, {"n": 2})
    for _ in range(100):
        await asyncio.sleep(0.01)
        if service.calls >= 2:
            break
    assert service.calls == 2
    await events.stop()


def test_the_sink_is_refused_even_when_a_manifest_declares_it():
    """Declaring it must not be a way to hand it to the model."""
    declared = ExtensionManifest.model_validate(
        {
            "id": "acme.one",
            "version": "1.0.0",
            "contract": ">=1.0 <2",
            "trust": "unsigned",
            "dev": {"command": ["true"]},
            "permissions": {"tools": [EVENT_SINK_TOOL, "acme.one.list"]},
        }
    )
    tools = Tools(declared)
    assert tools.allowed(EVENT_SINK_TOOL) is False
    assert tools.allowed("list") is True
    assert tools.refused([EVENT_SINK_TOOL, "list"]) == [EVENT_SINK_TOOL]


def test_telling_outside_a_loop_does_not_raise():
    """`tell` is called from a registry watcher, and a watcher can
    fire from a synchronous install. Throwing at whoever changed an
    extension's state would make an event a reason the install
    failed."""
    events, _ = relay(SlowService())
    events.tell("acme.one", HostTopic.SESSION_SWITCHED, {"session_id": "s"})
    assert events.pending("acme.one") == 0


@pytest.mark.asyncio
async def test_stopping_with_a_queue_behind_it_leaves_nothing_running():
    """A cancelled drain runs its `finally`, and that `finally`
    reschedules when work is left. So stopping a relay that still
    had events queued started a task which outlived the thing that
    stopped it — and `stop` had already let go of the handle."""
    service = SlowService()
    events, _ = relay(service)
    for n in range(5):
        events.tell("acme.one", HostTopic.WORKSPACE_FILES_CHANGED, {"n": n})
    await asyncio.wait_for(service.started.wait(), timeout=1)

    before = len(asyncio.all_tasks())
    await asyncio.wait_for(events.stop(), timeout=1)
    await asyncio.sleep(0.05)
    assert len(asyncio.all_tasks()) <= before - 1

    # And nothing queued afterwards starts one either.
    events.tell("acme.one", HostTopic.WORKSPACE_FILES_CHANGED, {"n": 99})
    await asyncio.sleep(0.05)
    assert events.pending("acme.one") == 0
