"""An extension tells the person at this machine something, and it stays here.

igni server's ``docs/service-identity.md`` §10.2. "Your export is ready"
is sent by the extension's own process as the MCP notification
``notifications/igni/notify`` on the stdio connection the host opened.
The host keeps it only if it came from that extension and points at one
of that extension's pages, keeps it once per ``event_key``, keeps it
across restarts, and never tells igni server about it.

The last test is end to end over a real subprocess: the MCP SDK drops
any notification method it has no type for, so the only proof that the
host hears this one is a real server sending it on a real connection.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification

from ember_code.core.mcp.client import MCPClientManager
from ember_code.core.mcp.notification_tap import McpNotificationRouter
from ember_code.extensions.installer import ExtensionInstaller, server_name
from ember_code.extensions.manifest import ExtensionManifest
from ember_code.extensions.origin import ExtensionOrigins
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState
from ember_code.extensions.rpc import ExtensionRpc
from ember_code.notifications.center import NotificationCenter
from ember_code.notifications.extension_notices import (
    ExtensionNoticeReceiver,
    NoticeVerdict,
)
from ember_code.notifications.local_store import LocalNotificationStore
from ember_code.notifications.schemas import MarkReadRequest, NotificationCounts
from tests.notification_helpers import FakeNotificationServer, settle, signed_in

FIXTURE = Path(__file__).parent / "fixtures" / "dev_extension"


def manifest(extension_id: str, *pages: str) -> ExtensionManifest:
    return ExtensionManifest.model_validate(
        {
            "id": extension_id,
            "version": "1.0.0",
            "contract": ">=1.0 <2",
            "name": extension_id,
            "trust": "unsigned",
            "dev": {"command": ["python", "server.py"]},
            "contributes": {
                "pages": [{"kind": page, "label": page, "sidebar": True} for page in pages]
            },
        }
    )


def notice(**changes: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "kind": "export_ready",
        "title": "Runbook.pdf",
        "body": "Your export is ready",
        "target": {"page": "acme.scan", "params": {"export": "e1"}},
        "event_key": "export:e1",
    }
    params.update(changes)
    return params


class Floor:
    """A registry with two extensions, a local store, and the center over them."""

    def __init__(self, tmp_path: Path, server: FakeNotificationServer | None = None) -> None:
        self.registry = ExtensionRegistry()
        self.registry.install(manifest("acme.scanner", "acme.scan"), state=ExtensionState.READY)
        self.registry.install(manifest("acme.other", "acme.other.page"), state=ExtensionState.READY)
        self.db = tmp_path / "data" / "notifications.db"
        self.store = LocalNotificationStore(self.db)
        self.server = server or FakeNotificationServer()
        self.announced: list[NotificationCounts] = []
        self.center = NotificationCenter(
            server=signed_in(),
            local=self.store,
            announce=self.announced.append,
            transport=self.server.transport,
        )
        self.receiver = ExtensionNoticeReceiver(registry=self.registry, sink=self.center)
        self.router = McpNotificationRouter()
        self.router.handle(ExtensionNoticeReceiver.METHOD, self.receiver)


# ── Accepted, kept, counted ─────────────────────────────────────────


def test_a_notice_about_its_own_page_is_kept(tmp_path):
    floor = Floor(tmp_path)

    outcome = floor.receiver.receive("acme.scanner", notice())

    assert outcome.verdict is NoticeVerdict.STORED
    [kept] = floor.store.listing()
    assert kept.source == "acme.scanner"
    assert kept.title == "Runbook.pdf"
    assert kept.target is not None and kept.target.page == "acme.scan"
    assert kept.target.params == {"export": "e1"}
    assert not kept.read
    assert floor.center.counts() == NotificationCounts(unread=1, by_source={"acme.scanner": 1})
    # The client heard the badge move.
    assert floor.announced[-1].by_source == {"acme.scanner": 1}


def test_it_survives_a_restart(tmp_path):
    floor = Floor(tmp_path)
    floor.receiver.receive("acme.scanner", notice())

    reopened = LocalNotificationStore(floor.db)

    assert [n.title for n in reopened.listing()] == ["Runbook.pdf"]
    assert reopened.counts().unread == 1


def test_its_read_state_is_kept_here_too(tmp_path):
    floor = Floor(tmp_path)
    kept = floor.receiver.receive("acme.scanner", notice()).notification
    assert kept is not None

    floor.store.mark_read(ids=[kept.id])

    assert LocalNotificationStore(floor.db).listing()[0].read
    assert LocalNotificationStore(floor.db).counts().unread == 0


# ── Refused ─────────────────────────────────────────────────────────


def test_a_page_that_is_not_its_own_is_refused(tmp_path):
    floor = Floor(tmp_path)

    outcome = floor.receiver.receive(
        "acme.scanner", notice(target={"page": "acme.other.page", "params": {}})
    )

    assert outcome.verdict is NoticeVerdict.REFUSED
    assert "acme.other.page" in outcome.reason
    assert floor.store.listing() == []


def test_a_page_nobody_declares_is_refused(tmp_path):
    floor = Floor(tmp_path)

    outcome = floor.receiver.receive("acme.scanner", notice(target={"page": "igni.settings"}))

    assert outcome.verdict is NoticeVerdict.REFUSED


def test_a_malformed_notice_is_refused(tmp_path):
    floor = Floor(tmp_path)

    for bad in (notice(event_key=""), notice(title=""), {"title": "no target"}, "text"):
        assert floor.receiver.receive("acme.scanner", bad).verdict is NoticeVerdict.REFUSED
    assert floor.store.listing() == []


def test_an_extension_that_is_not_installed_is_refused(tmp_path):
    floor = Floor(tmp_path)

    assert floor.receiver.receive("acme.gone", notice()).verdict is NoticeVerdict.REFUSED


def test_another_extension_cannot_post_as_this_one(tmp_path):
    """The sender is the connection it arrived on. Naming somebody else in
    the params changes nothing: ``acme.other`` saying it is the scanner,
    about the scanner's page, is ``acme.other`` pointing at a page that
    is not its own."""
    floor = Floor(tmp_path)

    floor.router.deliver(
        server_name("acme.other"),
        ExtensionNoticeReceiver.METHOD,
        notice(source="acme.scanner", extension_id="acme.scanner"),
    )

    assert floor.store.listing() == []


def test_a_connection_that_is_not_an_extension_is_ignored(tmp_path):
    floor = Floor(tmp_path)

    floor.router.deliver("filesystem", ExtensionNoticeReceiver.METHOD, notice())

    assert floor.store.listing() == []


def test_what_it_sends_about_its_own_page_is_kept_under_its_own_name(tmp_path):
    floor = Floor(tmp_path)

    floor.router.deliver(
        server_name("acme.other"),
        ExtensionNoticeReceiver.METHOD,
        notice(source="acme.scanner", target={"page": "acme.other.page"}),
    )

    [kept] = floor.store.listing()
    assert kept.source == "acme.other"


# ── Once per event ──────────────────────────────────────────────────


def test_a_repeated_event_key_is_dropped(tmp_path):
    floor = Floor(tmp_path)

    first = floor.receiver.receive("acme.scanner", notice())
    second = floor.receiver.receive("acme.scanner", notice(title="Runbook.pdf (again)"))

    assert first.verdict is NoticeVerdict.STORED
    assert second.verdict is NoticeVerdict.DUPLICATE
    assert [n.title for n in floor.store.listing()] == ["Runbook.pdf"]


def test_a_read_notice_is_not_brought_back_by_a_retry(tmp_path):
    floor = Floor(tmp_path)
    kept = floor.receiver.receive("acme.scanner", notice()).notification
    assert kept is not None
    floor.store.mark_read(ids=[kept.id])

    floor.receiver.receive("acme.scanner", notice())

    assert floor.store.counts().unread == 0


def test_the_same_key_from_two_extensions_is_two_events(tmp_path):
    floor = Floor(tmp_path)

    floor.receiver.receive("acme.scanner", notice())
    floor.receiver.receive("acme.other", notice(target={"page": "acme.other.page"}))

    assert floor.store.counts().by_source == {"acme.scanner": 1, "acme.other": 1}


def test_an_extension_that_notifies_in_a_loop_is_bounded(tmp_path, monkeypatch):
    floor = Floor(tmp_path)
    monkeypatch.setattr(LocalNotificationStore, "KEEP", 3)

    for n in range(5):
        floor.receiver.receive("acme.scanner", notice(event_key=f"export:{n}"))

    assert len(floor.store.listing()) == 3


# ── Never sent to igni server ───────────────────────────────────────


async def test_a_local_notice_never_reaches_igni_server(tmp_path):
    floor = Floor(tmp_path)

    kept = floor.receiver.receive("acme.scanner", notice()).notification
    assert kept is not None
    assert floor.server.requests == []

    # Marking it read is local too: its id is not sent anywhere.
    await floor.center.mark_read(MarkReadRequest(ids=[kept.id]))

    assert floor.server.read_bodies == []
    assert not any(path == "/v1/notifications/read" for _, path in floor.server.requests)
    assert floor.store.counts().unread == 0


async def test_marking_everything_read_tells_the_server_nothing_local(tmp_path):
    floor = Floor(tmp_path)
    floor.receiver.receive("acme.scanner", notice())

    await floor.center.mark_read(MarkReadRequest())

    assert floor.server.read_bodies == [{}]
    assert floor.store.counts().unread == 0


# ── The tap on the connection ───────────────────────────────────────


def _session_message(method: str, params: dict[str, Any]) -> SessionMessage:
    return SessionMessage(
        message=JSONRPCMessage(JSONRPCNotification(jsonrpc="2.0", method=method, params=params))
    )


async def test_the_tap_takes_its_method_and_passes_everything_else():
    heard: list[tuple[str, dict[str, Any]]] = []
    router = McpNotificationRouter()
    router.handle(
        "notifications/igni/notify", lambda server, params: heard.append((server, params))
    )
    send, receive = anyio.create_memory_object_stream[Any](10)
    tapped = router.tap("ext:acme.scanner", receive)

    progress = _session_message("notifications/progress", {"progressToken": 1, "progress": 1})
    await send.send(_session_message("notifications/igni/notify", {"title": "x"}))
    await send.send(progress)
    await send.aclose()

    assert await tapped.receive() is progress
    assert heard == [("ext:acme.scanner", {"title": "x"})]
    with pytest.raises(anyio.EndOfStream):
        await tapped.receive()


async def test_a_handler_that_raises_does_not_end_the_connection():
    router = McpNotificationRouter()

    def broken(server: str, params: dict[str, Any]) -> None:
        raise RuntimeError("boom")

    router.handle("notifications/igni/notify", broken)
    send, receive = anyio.create_memory_object_stream[Any](10)
    tapped = router.tap("ext:acme.scanner", receive)
    after = _session_message("notifications/progress", {"progressToken": 1, "progress": 1})
    await send.send(_session_message("notifications/igni/notify", {}))
    await send.send(after)

    assert await tapped.receive() is after


# ── End to end, over a real extension process ───────────────────────


def bundle_at(tmp_path: Path) -> Path:
    bundle = tmp_path / "ext"
    shutil.copytree(FIXTURE, bundle)
    raw = json.loads((bundle / "manifest.json").read_text())
    raw["dev"]["command"] = [sys.executable, "server.py"]
    (bundle / "manifest.json").write_text(json.dumps(raw))
    return bundle


async def test_a_real_extension_process_notifies_the_host(tmp_path):
    registry = ExtensionRegistry()
    rpc = ExtensionRpc(registry)
    mcp = MCPClientManager(project_dir=tmp_path)
    origins = ExtensionOrigins()
    server = FakeNotificationServer()
    store = LocalNotificationStore(tmp_path / "notifications.db")
    announced: list[NotificationCounts] = []
    center = NotificationCenter(
        server=signed_in(), local=store, announce=announced.append, transport=server.transport
    )
    mcp.notifications.handle(
        ExtensionNoticeReceiver.METHOD,
        ExtensionNoticeReceiver(registry=registry, sink=center),
    )
    installer = ExtensionInstaller(
        registry=registry, rpc=rpc, mcp=mcp, origins=origins, allow_development=True
    )
    try:
        entry = await installer.install(bundle_at(tmp_path))
        assert entry.state is ExtensionState.READY, entry.reason

        await rpc.dispatch(
            caller="acme.scanner",
            method="ext.acme.scanner.notify_me",
            args={"params": notice()},
        )
        # Refused: not its page. Then a repeat of the first, dropped.
        await rpc.dispatch(
            caller="acme.scanner",
            method="ext.acme.scanner.notify_me",
            args={"params": notice(event_key="x", target={"page": "igni.settings"})},
        )
        await rpc.dispatch(
            caller="acme.scanner",
            method="ext.acme.scanner.notify_me",
            args={"params": notice()},
        )
        for _ in range(100):
            if store.listing():
                break
            await asyncio.sleep(0.02)
        await settle()
    finally:
        await origins.stop_all()
        await mcp.disconnect_all()

    [kept] = store.listing()
    assert kept.source == "acme.scanner"
    assert kept.title == "Runbook.pdf"
    assert center.counts().by_source == {"acme.scanner": 1}
    assert announced and announced[-1].by_source == {"acme.scanner": 1}
    assert server.requests == []
