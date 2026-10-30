"""A plain `def` an author hands the SDK runs off the event loop.

`igni_extension.threads`: a sync tool, hook handler or reaction runs in
a worker thread, so it never holds the calls that arrive while it runs.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import pytest
from mcp.server.fastmcp import FastMCP

from igni_extension import Hooks, IgniExtension, Reject, ToolHints
from igni_extension.hooks import PlanSubmit
from igni_extension.testing import FakeHost

HINTS = ToolHints(read_only=True, destructive=False, idempotent=True, open_world=False)


def slow_sum(a: int, b: int) -> int:
    """Add, slowly."""
    time.sleep(1.0)
    return a + b


@pytest.fixture
async def host() -> AsyncIterator[FakeHost]:
    async with FakeHost() as fake:
        yield fake


def _extension(environ: dict[str, str] | None = None) -> IgniExtension:
    ext = IgniExtension("acme.example", environ=environ or {})
    ext.tool(hints=HINTS)(slow_sum)

    @ext.tool(hints=HINTS)
    async def fast() -> str:
        return "fast"

    return ext


async def test_a_fast_call_does_not_wait_on_a_slow_sync_one() -> None:
    ext = _extension()
    slow = asyncio.create_task(ext.mcp.call_tool("slow_sum", {"a": 1, "b": 2}))
    await asyncio.sleep(0.05)

    started = time.monotonic()
    await ext.mcp.call_tool("fast", {})
    took = time.monotonic() - started

    assert took < 0.5
    assert "3" in str(await slow)


async def test_a_hook_is_answered_while_a_sync_tool_runs(host: FakeHost) -> None:
    ext = _extension(host.environment())

    @ext.hook(Hooks.PLAN_SUBMIT)
    def grounded(plan: PlanSubmit) -> Reject:
        return Reject("Name the files.")

    await ext.connect()
    serving = asyncio.create_task(ext.connection.serve_forever())
    try:
        slow = asyncio.create_task(ext.mcp.call_tool("slow_sum", {"a": 1, "b": 2}))
        await asyncio.sleep(0.05)
        started = time.monotonic()
        answer = await host.hook(
            "PlanSubmit", {"session_id": "s", "plan": "p", "tasks": [], "attempt": 0}
        )
        took = time.monotonic() - started
        await slow
    finally:
        serving.cancel()
        await ext.aclose()

    assert answer["outcome"] == "reject" and answer["reason"] == "Name the files."
    assert took < 0.5


async def test_running_it_in_a_thread_leaves_its_schema_as_fastmcp_made_it() -> None:
    plain = FastMCP("plain")
    plain.tool(annotations=HINTS.annotations())(slow_sum)
    expected = {tool.name: tool.inputSchema for tool in await plain.list_tools()}

    listed = {tool.name: tool.inputSchema for tool in await _extension().mcp.list_tools()}

    assert listed["slow_sum"] == expected["slow_sum"]
