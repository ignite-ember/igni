"""`ext.host.notifications.notify`: §10.2 over a real MCP session."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from igni_extension import IgniExtension, LocalNotification, Target
from igni_extension.testing import HostHarness

EXPORT_READY = LocalNotification(
    kind="export_ready",
    title="Runbook.pdf",
    body="Your export is ready",
    target=Target(page="acme.example", params={"export": "e1"}),
    event_key="export:e1",
)


def an_extension() -> IgniExtension:
    ext = IgniExtension("acme.example", environ={})

    @ext.tool
    async def export(slug: str) -> str:
        await ext.host.notifications.notify(EXPORT_READY)
        return f"exported {slug}"

    return ext


async def test_a_notification_crosses_the_wire_as_the_host_reads_it() -> None:
    async with HostHarness(an_extension()) as host:
        result = await host.session.call_tool("export", {"slug": "runbook"})
        [received] = await host.received(1)

    assert not result.isError
    assert received == EXPORT_READY
    assert host.raw == [
        {
            "jsonrpc": "2.0",
            "method": "notifications/igni/notify",
            "params": {
                "kind": "export_ready",
                "title": "Runbook.pdf",
                "body": "Your export is ready",
                "target": {"page": "acme.example", "params": {"export": "e1"}},
                "event_key": "export:e1",
            },
        }
    ]


async def test_a_background_task_can_notify_outside_any_tool_call() -> None:
    ext = IgniExtension("acme.example", environ={})

    async with HostHarness(ext) as host:
        task = asyncio.create_task(ext.host.notifications.notify(EXPORT_READY))
        await task
        assert await host.received(1) == [EXPORT_READY]


async def test_what_is_sent_before_the_host_connects_is_delivered_when_it_does() -> None:
    ext = IgniExtension("acme.example", environ={})
    await ext.host.notifications.notify(EXPORT_READY)

    async with HostHarness(ext) as host:
        assert await host.received(1) == [EXPORT_READY]


def test_a_body_is_one_line() -> None:
    notification = LocalNotification(
        kind="k", title="t", body=" a\n b ", target=Target(page="p"), event_key="e"
    )

    assert notification.body == "a b"


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": ""},
        {"kind": "k" * 65},
        {"title": ""},
        {"title": "t" * 201},
        {"body": "b" * 1001},
        {"event_key": ""},
        {"event_key": "e" * 201},
        {"target": None},
        {"target": {"page": ""}},
        {"target": {"page": "p", "params": {"big": "x" * 2100}}},
        {"recipients": ["someone"]},
        {"actor": "someone"},
    ],
)
def test_what_the_host_would_refuse_is_refused_where_it_is_written(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        LocalNotification.model_validate(EXPORT_READY.model_dump() | overrides)
