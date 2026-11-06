"""`ext.run()`: the extension as igni starts it, a process speaking MCP over stdio."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.extension import NotificationBinding
from mcp.client.stdio import stdio_client

from igni_extension.notifications import NOTIFY_METHOD, NotifyNotification, NotifyParams

SCRIPT = textwrap.dedent(
    """
    from igni_extension import IgniExtension, LocalNotification, Target, ToolHints

    ext = IgniExtension("acme.example")

    @ext.tool(hints=ToolHints(read_only=False, destructive=False, idempotent=True, open_world=False))
    async def export(slug: str) -> str:
        await ext.host.notifications.notify(
            LocalNotification(
                kind="export_ready",
                title=slug + ".pdf",
                target=Target(page="acme.example", params={"export": "e1"}),
                event_key="export:e1",
            )
        )
        return "done"

    ext.run()
    """
)


async def test_a_process_serves_tools_and_notifies_over_stdio(tmp_path: Path) -> None:
    script = tmp_path / "extension.py"
    script.write_text(SCRIPT)
    source = Path(__file__).resolve().parents[1] / "src"
    received: list[NotifyNotification] = []
    arrived = anyio.Event()

    async def on_notify(params: NotifyParams) -> None:
        received.append(NotifyNotification(params=params))
        arrived.set()

    igni_notifications = [
        NotificationBinding(method=NOTIFY_METHOD, params_type=NotifyParams, handler=on_notify)
    ]

    server = StdioServerParameters(
        command=sys.executable, args=[str(script)], env={"PYTHONPATH": str(source)}
    )
    with anyio.fail_after(30):
        async with (
            stdio_client(server) as (read, write),
            ClientSession(read, write, notification_bindings=igni_notifications) as session,
        ):
            await session.initialize()
            result = await session.call_tool("export", {"slug": "runbook"})
            await arrived.wait()

    assert not result.is_error
    [notification] = received
    assert notification.local().title == "runbook.pdf"
    assert notification.params.event_key == "export:e1"


BACKGROUND = textwrap.dedent(
    """
    import asyncio
    import sys
    from pathlib import Path

    from igni_extension import IgniExtension, ToolHints

    ext = IgniExtension("acme.example")
    started = asyncio.Event()

    @ext.on_start
    async def keep_running() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            Path(sys.argv[1]).write_text("stopped")

    @ext.tool(hints=ToolHints(read_only=True, destructive=False, idempotent=True, open_world=False))
    async def is_running() -> str:
        await asyncio.wait_for(started.wait(), 5)
        return "running"

    ext.run()
    """
)


async def test_work_started_with_the_process_runs_until_igni_closes_it(tmp_path: Path) -> None:
    script = tmp_path / "extension.py"
    script.write_text(BACKGROUND)
    marker = tmp_path / "marker"
    source = Path(__file__).resolve().parents[1] / "src"
    server = StdioServerParameters(
        command=sys.executable, args=[str(script), str(marker)], env={"PYTHONPATH": str(source)}
    )
    with anyio.fail_after(30):
        async with (
            stdio_client(server) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool("is_running", {})

        # Closing the pipe cancels it, and its own `finally` ran.
        while not marker.exists():
            await anyio.sleep(0.05)

    assert not result.is_error
    assert marker.read_text() == "stopped"
