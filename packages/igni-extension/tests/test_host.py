"""`ext.host`: igni's object model, as an extension's process reaches it.

Against `FakeHost`, which serves the protocol igni serves on its loopback
socket. igni's own tests drive the real host with a real process; these
are about what the SDK does with it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from igni_extension import HostError, IgniExtension, LocalNotification, NotConfigured, Target
from igni_extension.host_model import ChatMessage, Run, SessionChange, Status
from igni_extension.testing import FakeHost, HostHarness


@pytest.fixture
async def host() -> AsyncIterator[FakeHost]:
    fake = FakeHost(
        granted=("chat.read", "chat.post", "sessions"),
        values={
            "chat.processing": False,
            "sessions.current": "s-1",
            "chat.status": {
                "session_id": "s-1",
                "model": "small",
                "model_configured": True,
                "permission_mode": "default",
                "input_tokens": 1,
                "output_tokens": 2,
                "context_tokens": 3,
                "max_context": 100,
            },
        },
        answers={"chat.append": "view-1"},
        refusals={"mcp.servers": ("not_granted", "acme.example is not granted 'mcp'")},
    )
    async with fake:
        yield fake


async def test_properties_read_typed_and_write_as_the_person_would(host: FakeHost) -> None:
    ext = IgniExtension("acme.example", environ=host.environment())

    assert await ext.host.chat.processing is False
    status = await ext.host.chat.status.get()
    assert isinstance(status, Status) and status.model == "small"
    await ext.host.sessions.current.set("s-2")

    assert host.values["sessions.current"] == "s-2"
    assert [(c.op, c.path) for c in host.calls] == [
        ("get", "chat.processing"),
        ("get", "chat.status"),
        ("set", "sessions.current"),
    ]
    with pytest.raises(TypeError):
        await ext.host.chat.processing.set(True)  # not writable; never sent
    await ext.aclose()


async def test_methods_send_their_parameters_and_read_their_result(host: FakeHost) -> None:
    ext = IgniExtension("acme.example", environ=host.environment())

    await ext.host.chat.post("Two issues in auth.py.")
    view = await ext.host.chat.append({"root": "c", "elements": {}})

    post, append = host.calls
    assert (post.path, post.params) == ("chat.post", {"text": "Two issues in auth.py."})
    assert append.params == {"view": {"root": "c", "elements": {}}}
    assert view == "view-1"
    await ext.aclose()


async def test_a_refusal_is_a_host_error_with_its_code(host: FakeHost) -> None:
    ext = IgniExtension("acme.example", environ=host.environment())

    with pytest.raises(HostError) as refused:
        await ext.host.mcp.servers

    assert refused.value.code == "not_granted"
    assert "not granted" in refused.value.message
    await ext.aclose()


async def test_any_window_is_addressed_by_its_id(host: FakeHost) -> None:
    ext = IgniExtension("acme.example", environ=host.environment())
    host.answers["ui.navigate"] = []

    await ext.host.windows.ui("w-2").navigate("plugins")

    [call] = host.calls
    assert (call.path, call.window, call.params) == ("ui.navigate", "w-2", {"page": "plugins"})
    await ext.aclose()


async def test_a_signal_runs_its_handler_with_the_typed_value(host: FakeHost) -> None:
    ext = IgniExtension("acme.example", environ=host.environment())
    seen: list[str] = []
    done = asyncio.Event()

    @ext.on(ext.host.sessions.current_changed)
    async def follow(change: SessionChange) -> None:
        # Reading the model while handling: the socket is not blocked.
        busy = await ext.host.chat.processing
        seen.append(f"{change.session_id} busy={busy}")
        done.set()

    await ext.connect()
    await host.wait_subscribed("session.switched")
    await host.emit("session.switched", {"session_id": "s-9", "window": None})
    await asyncio.wait_for(done.wait(), 5)

    assert seen == ["s-9 busy=False"]
    await ext.aclose()


async def test_a_handler_added_after_connecting_is_subscribed_then(host: FakeHost) -> None:
    ext = IgniExtension("acme.example", environ=host.environment())
    await ext.connect()
    got: list[Run] = []
    done = asyncio.Event()

    @ext.on(ext.host.chat.run_completed)
    async def reviewed(run: Run) -> None:
        got.append(run)
        done.set()

    await host.wait_subscribed("chat.run_completed")
    await host.emit(
        "chat.run_completed",
        {
            "session_id": "s-1",
            "run_id": "r1",
            "parent_run_id": "",
            "input_tokens": 1,
            "output_tokens": 1,
            "reasoning_tokens": 0,
            "duration": 0.2,
            "changed_files": ["src/a.py"],
        },
    )
    await asyncio.wait_for(done.wait(), 5)
    assert got[0].changed_files == ["src/a.py"]
    await ext.aclose()


async def test_the_hello_says_what_the_extension_holds(host: FakeHost) -> None:
    ext = IgniExtension("acme.example", environ=host.environment())
    await ext.connect()
    assert ext.connection.granted == ["chat.read", "chat.post", "sessions"]
    await ext.aclose()


async def test_outside_igni_the_host_is_not_configured() -> None:
    ext = IgniExtension("acme.example", environ={})
    with pytest.raises(NotConfigured):
        await ext.host.chat.processing


async def test_notify_travels_on_the_socket_when_there_is_one(host: FakeHost) -> None:
    ext = IgniExtension("acme.example", environ=host.environment())

    await ext.host.notifications.notify(
        LocalNotification(
            kind="export_ready",
            title="Runbook.pdf",
            target=Target(page="acme.example"),
            event_key="e1",
        )
    )

    [call] = host.calls
    assert call.path == "notifications.notify"
    assert call.params["notification"]["event_key"] == "e1"
    await ext.aclose()


async def test_without_the_socket_notify_still_reaches_igni_over_mcp() -> None:
    ext = IgniExtension("acme.example", environ={})
    async with HostHarness(ext) as harness:
        await ext.host.notifications.notify(
            LocalNotification(
                kind="k", title="t", target=Target(page="acme.example"), event_key="e2"
            )
        )
        [received] = await harness.received(1)
    assert received.event_key == "e2"


async def test_a_signal_payload_the_sdk_cannot_read_is_dropped_not_raised(host: FakeHost) -> None:
    ext = IgniExtension("acme.example", environ=host.environment())
    got: list[ChatMessage] = []

    @ext.on(ext.host.chat.message_added)
    async def added(message: ChatMessage) -> None:
        got.append(message)

    await ext.connect()
    await host.wait_subscribed("chat.message_added")
    await host.emit("chat.message_added", {"nonsense": True})
    await host.emit(
        "chat.message_added",
        {"session_id": "s-1", "text": "hi", "queued": False, "from_extension": "acme.other"},
    )
    for _ in range(50):
        if got:
            break
        await asyncio.sleep(0.01)
    assert [m.from_extension for m in got] == ["acme.other"]
    await ext.aclose()
