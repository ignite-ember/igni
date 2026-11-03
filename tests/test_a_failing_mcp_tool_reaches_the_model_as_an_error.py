"""A tool that raises reaches the model as its message, marked as an error.

What the model got from an MCP tool that raised was a Python repr —
``Error from MCP tool 'boom': [TextContent(type='text', text='...',
annotations=None, meta=None)]`` — and the call counted as a success, so
nothing (the transcript's ``is_error``, Anthropic's ``is_error`` on the
tool result) said it had failed. Agno marks a call failed only when its
entrypoint raises; the MCP result's text parts now become that error.

A real stdio FastMCP server, called the way the agent calls it: through
Agno's ``FunctionCall``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from agno.tools.function import FunctionCall

from ember_code.core.mcp.approval import MCPApprovalManager
from ember_code.core.mcp.client import MCPClientManager
from ember_code.core.mcp.config import MCPPolicy

pytestmark = pytest.mark.asyncio

SERVER = r"""
from mcp.server.fastmcp import FastMCP

server = FastMCP("tickets")

@server.tool()
def boom(x: str) -> str:
    "Always fails."
    raise RuntimeError("database connection lost (simulated)")

@server.tool()
def fine(x: str) -> str:
    "Answers."
    return f"ok {x}"

server.run()
"""


@pytest.fixture
async def manager(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "server.py").write_text(SERVER)
    config = project / ".mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "tickets": {"command": sys.executable, "args": [str(project / "server.py")]}
                }
            }
        )
    )
    MCPApprovalManager().approve("tickets", str(config))
    mcp = MCPClientManager(project_dir=project, policy=MCPPolicy())
    yield mcp
    await mcp.disconnect_all()


async def _call(manager: MCPClientManager, tool: str) -> FunctionCall:
    result = await manager.connect("tickets")
    assert result.ok, result.reason
    function = result.client.functions[tool]
    call = FunctionCall(function=function, arguments={"x": "T-1"})
    await call.aexecute()
    return call


async def test_a_raising_tool_is_a_failed_call_with_its_message(manager):
    call = await _call(manager, "mcp_tickets_boom")

    assert call.error, "the call counted as a success"
    assert "database connection lost (simulated)" in call.error
    assert "TextContent" not in call.error


async def test_a_tool_that_answers_is_unchanged(manager):
    call = await _call(manager, "mcp_tickets_fine")

    assert call.error is None
    assert "ok T-1" in str(call.result)
