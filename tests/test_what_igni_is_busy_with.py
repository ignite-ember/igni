"""What igni is busy with decides whether it may be suspended (``core/activity``).

Anything working holds a busy lease while it works — calls out, MCP and
extension calls, scheduled tasks, processes, an extension that said so
(``self.busy``) — and lets it go when done; a lease with an expiry stops
counting then, so nothing stuck keeps igni awake for ever. Waiting on a
person is not busy. What is due later is a wake; the earliest is
``wake_at``. All of it reaches ``app.activity`` (and, in the cloud, ``activity.json``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from ember_code.backend.activity import Activity, ActivityState, SessionActivity
from ember_code.core.activity import ActivityRegistry, BusyKind
from ember_code.core.activity.http import BusyAsyncClient, BusyClient
from ember_code.core.scheduler.runner import SchedulerRunner
from ember_code.extensions.busy import ExtensionBusy
from ember_code.extensions.service import McpExtensionService
from tests.host_model_helpers import HostRig


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


# ── The registry ──────────────────────────────────────────────────


def test_a_lease_is_busy_until_let_go():
    registry = ActivityRegistry()
    with registry.hold(BusyKind.MODEL_CALL, "model", "POST api") as lease:
        [held] = registry.busy()
        assert (held.id, held.kind, held.holder) == (lease.id, BusyKind.MODEL_CALL, "model")
    assert registry.busy() == []


def test_a_lease_with_an_expiry_stops_counting_on_its_own():
    clock = Clock()
    registry = ActivityRegistry(clock=clock)
    lease = registry.hold(BusyKind.EXTENSION, "acme.indexer", "indexing", ttl=60)
    clock.now += timedelta(seconds=59)
    assert registry.is_busy()
    lease.renew(60)
    clock.now += timedelta(seconds=59)
    assert registry.is_busy(), "renewed"
    clock.now += timedelta(seconds=2)
    assert not registry.is_busy(), "expired: a stuck holder cannot keep igni awake"


def test_the_earliest_wake_is_wake_at():
    registry = ActivityRegistry()
    soon = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
    later = soon + timedelta(hours=3)
    registry.wake("scheduler:a", later, "a scheduled task")
    registry.wake("retry", soon, "a retry")
    assert registry.wake_at() == soon
    registry.wake("retry", None)
    assert registry.wake_at() == later


def test_watchers_hear_every_move():
    registry = ActivityRegistry()
    moves: list[int] = []
    registry.watch(lambda: moves.append(len(registry.busy())))
    lease = registry.hold(BusyKind.PROCESS, "process:1")
    lease.release()
    registry.wake("x", datetime.now(UTC))
    assert moves == [1, 0, 0]


def test_busy_work_makes_igni_running_and_not_suspendable():
    registry = ActivityRegistry()
    waiting = [SessionActivity(session_id="s", state=ActivityState.WAITING, waiting_approvals=1)]
    assert Activity.now(waiting, registry).suspendable, "waiting on a person is not busy"
    with registry.hold(BusyKind.SCHEDULED_TASK, "scheduled-task:t1", "nightly report"):
        activity = Activity.now(waiting, registry)
        assert activity.state is ActivityState.RUNNING and not activity.suspendable
        assert [b.holder for b in activity.busy] == ["scheduled-task:t1"]


# ── Calls out ─────────────────────────────────────────────────────


async def test_an_http_call_is_busy_until_its_answer_is_read():
    registry = ActivityRegistry.shared()
    seen: list[int] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(len(registry.busy()))
        return httpx.Response(200, json={"ok": True})

    async with BusyAsyncClient(
        busy_holder="igni-server", transport=httpx.MockTransport(answer)
    ) as client:
        assert (await client.get("https://igni.test/v1/me")).json() == {"ok": True}
    assert seen == [1]
    assert registry.busy() == []


async def test_a_streamed_model_answer_is_busy_until_the_stream_closes():
    registry = ActivityRegistry.shared()

    class Tokens(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in (b"data: hi\n\n", b"data: [DONE]\n\n"):
                yield chunk

        async def aclose(self) -> None:
            pass

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Tokens())

    client = BusyAsyncClient(
        busy_holder="model", busy_kind=BusyKind.MODEL_CALL, transport=httpx.MockTransport(answer)
    )
    async with client, client.stream("POST", "https://model.test/v1/chat/") as response:
        [lease] = registry.busy()
        assert lease.kind is BusyKind.MODEL_CALL and lease.expires_at is not None
        async for _ in response.aiter_bytes():
            assert registry.is_busy(), "while the tokens come"
    assert registry.busy() == []


def test_a_failed_call_lets_go():
    registry = ActivityRegistry.shared()

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    client = BusyClient(busy_holder="x", transport=httpx.MockTransport(refuse))
    with client, pytest.raises(httpx.ConnectError):
        client.get("https://nowhere.test/")
    assert registry.busy() == []


async def test_an_extension_call_is_busy_while_its_service_works():
    registry = ActivityRegistry.shared()
    during: list[list[str]] = []

    class Session:
        async def call_tool(self, method: str, args: dict[str, Any]) -> Any:
            during.append([b.holder for b in registry.busy()])
            return type("Result", (), {"isError": False, "structuredContent": {"ok": 1}})()

    class Client:
        async def get_session_for_run(self) -> Session:
            return Session()

    class Manager:
        def get_client(self, name: str) -> Client:
            return Client()

    service = McpExtensionService(manager=Manager(), server_name="acme.indexer")
    await service.call("reindex", {})
    assert during == [["acme.indexer"]]
    assert registry.busy() == []


# ── The scheduler's next task is a wake ───────────────────────────


async def test_the_scheduler_publishes_when_its_next_task_is_due():
    due = datetime(2026, 10, 27, 9, 30)  # stored as local wall-clock time

    class Store:
        async def get_due_tasks(self) -> list[Any]:
            return []

        async def next_pending_at(self) -> datetime | None:
            return due

    async def execute(_description: str) -> str:
        return ""

    runner = SchedulerRunner(Store(), execute)  # type: ignore[arg-type]
    await runner._check_and_spawn()
    assert ActivityRegistry.shared().wake_at() == due.astimezone(UTC)
    runner.stop()
    assert ActivityRegistry.shared().wake_at() is None


# ── ext.busy ──────────────────────────────────────────────────────


def test_an_extensions_lease_is_capped_renewed_and_dropped_when_it_stops():
    registry = ActivityRegistry()
    busy = ExtensionBusy(registry)
    first = busy.busy("acme.indexer", "indexing", ttl_seconds=86_400)
    assert first.expires_at is not None
    assert first.expires_at - first.since <= timedelta(seconds=ExtensionBusy.MAX_TTL_SECONDS)
    again = busy.busy("acme.indexer", "indexing", 30)
    assert again.id == first.id, "the same reason renews the same lease"
    busy.busy("acme.indexer", "syncing")
    busy.busy("acme.other", "indexing")
    busy.done("acme.indexer", "syncing")
    assert sorted((b.holder, b.reason) for b in registry.busy()) == [
        ("acme.indexer", "indexing"),
        ("acme.other", "indexing"),
    ]
    busy.release_all("acme.indexer")
    assert [b.holder for b in registry.busy()] == ["acme.other"]


async def test_an_extension_says_it_is_busy_through_the_host_model(tmp_path: Path):
    rig = await HostRig(tmp_path).start()
    try:
        extension = rig.install("acme.indexer", host=["app"])
        reply = await rig.ask(extension, "call", "self.busy", reason="indexing", ttl_seconds=120)
        lease = reply.result
        assert (lease["kind"], lease["holder"], lease["reason"]) == (
            "extension",
            "acme.indexer",
            "indexing",
        )
        activity = (await rig.ask(extension, "get", "app.activity")).result
        assert activity["suspendable"] is False
        assert [b["reason"] for b in activity["busy"]] == ["indexing"]
        await rig.ask(extension, "call", "self.done", reason="indexing")
        assert (await rig.ask(extension, "get", "app.activity")).result["suspendable"] is True
    finally:
        await rig.stop()
