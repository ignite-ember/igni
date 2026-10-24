"""`ext.run()`: the extension as igni starts it, a process speaking MCP over stdio."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import anyio
from mcp import StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.session import RequestResponder

from igni_extension.notifications import NotifyNotification
from igni_extension.testing import HostClientSession

SCRIPT = textwrap.dedent(
    """
    from igni_extension import IgniExtension, LocalNotification, Target

    ext = IgniExtension("acme.example")

    @ext.tool
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

    async def on_message(
        message: RequestResponder[types.ServerRequest, types.ClientResult]
        | types.ServerNotification
        | Exception,
    ) -> None:
        root = getattr(message, "root", None)
        if isinstance(root, NotifyNotification):
            received.append(root)
            arrived.set()

    server = StdioServerParameters(
        command=sys.executable, args=[str(script)], env={"PYTHONPATH": str(source)}
    )
    with anyio.fail_after(30):
        async with (
            stdio_client(server) as (read, write),
            HostClientSession(read, write, message_handler=on_message) as session,
        ):
            await session.initialize()
            result = await session.call_tool("export", {"slug": "runbook"})
            await arrived.wait()

    assert not result.isError
    [notification] = received
    assert notification.local().title == "runbook.pdf"
    assert notification.params.event_key == "export:e1"
