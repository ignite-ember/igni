"""An MCP server from ``.mcp.json`` is connected, its tools filtered, and asked for more.

Everything here runs a real stdio MCP server — an MCPServer script in the
test's own directory, started by :class:`MCPClientManager` through the
same :class:`StdioMCPBinding` a session uses. What is checked is what a
person sees through the MCP panel and the agent: which tools arrive,
that switching one off hides it from the agent and survives a restart,
that switching it back on brings it back (with any confirmation pause it
had), and what the server says about its resources, prompts and tools'
annotations. The gates in front of the connection — managed policy, the
air gap, first-use approval — are checked against a configuration that
would otherwise connect.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ember_code.core.mcp.approval import MCPApprovalManager
from ember_code.core.mcp.client import MCPClientManager
from ember_code.core.mcp.config import MCPPolicy, MCPServerConfig, MCPTransport
from ember_code.core.network_policy import NetworkPolicy

SERVER = r"""
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("notes")

@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def read_note(title: str) -> str:
    "Read one note by its title."
    return f"the note called {title}"

@server.tool()
def delete_note(title: str) -> str:
    "Delete one note."
    return f"deleted {title}"

@server.resource("notes://index", mime_type="text/plain", description="Every note's title.")
def index() -> str:
    return "groceries"

@server.prompt(description="Summarise one note.")
def summarise(title: str) -> str:
    return f"Summarise {title}."

server.run()
"""

#: A server built on the SDK's low-level ``Server`` with tools and nothing else,
#: so asking it for resources or prompts is "Method not found".
TOOLS_ONLY_SERVER = r"""
import anyio
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import ListToolsResult, Tool

async def list_tools(ctx, params):
    return ListToolsResult(tools=[Tool(name="ping", description="Answers.", input_schema={"type": "object"})])

server = Server("bare", on_list_tools=list_tools)

async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

anyio.run(main)
"""

EMPTY_SERVER = r"""
from mcp.server.mcpserver import MCPServer
MCPServer("empty").run()
"""

READ = "mcp_notes_read_note"
DELETE = "mcp_notes_delete_note"


def _project(tmp_path: Path, script: str = SERVER, name: str = "notes") -> Path:
    """A project whose ``.mcp.json`` declares one stdio server, approved once before."""
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    (project / "server.py").write_text(script)
    config = project / ".mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    name: {"command": sys.executable, "args": [str(project / "server.py")]}
                }
            }
        )
    )
    MCPApprovalManager().approve(name, str(config))
    return project


@pytest.fixture
async def manager(tmp_path):
    mcp = MCPClientManager(project_dir=_project(tmp_path), policy=MCPPolicy())
    yield mcp
    await mcp.disconnect_all()


async def test_its_tools_arrive_namespaced_with_their_descriptions(manager):
    result = await manager.connect("notes")

    assert result.ok, result.reason
    assert manager.list_connected() == ["notes"]
    assert manager.get_client("notes") is result.client
    assert set(manager.all_clients()) == {"notes"}
    assert set(manager.get_tools("notes")) == {READ, DELETE}
    assert manager.get_tool_descriptions("notes")[READ] == "Read one note by its title."
    assert all(row.enabled for row in manager.list_tool_info("notes"))
    # A second connect is the same live connection, not a second process.
    assert (await manager.connect("notes")).client is result.client


async def test_a_tool_switched_off_is_hidden_from_the_agent_but_still_listed(manager):
    client = (await manager.connect("notes")).client

    manager.set_tool_enabled("notes", DELETE, False)

    assert set(client.functions) == {READ}
    assert set(manager.get_tools("notes")) == {READ, DELETE}
    assert manager.get_disabled_tools("notes") == [DELETE]
    rows = {row.name: row for row in manager.list_tool_info("notes")}
    assert rows[DELETE].enabled is False
    assert rows[DELETE].description == "Delete one note."

    manager.set_tool_enabled("notes", DELETE, True)

    assert set(client.functions) == {READ, DELETE}
    assert manager.get_disabled_tools("notes") == []


async def test_a_switched_off_tool_stays_off_after_a_restart(tmp_path):
    project = _project(tmp_path)
    first = MCPClientManager(project_dir=project, policy=MCPPolicy())
    try:
        await first.connect("notes")
        first.set_tool_enabled("notes", DELETE, False)
    finally:
        await first.disconnect_all()

    second = MCPClientManager(project_dir=project, policy=MCPPolicy())
    try:
        client = (await second.connect("notes")).client
        assert set(client.functions) == {READ}
        assert second.get_disabled_tools("notes") == [DELETE]
    finally:
        await second.disconnect_all()


async def test_a_tool_switched_back_on_keeps_its_confirmation_pause(manager):
    client = (await manager.connect("notes")).client
    manager.set_tool_enabled("notes", DELETE, False)

    manager.require_confirmation("notes", {DELETE})
    manager.set_tool_enabled("notes", DELETE, True)

    assert client.functions[DELETE].requires_confirmation is True
    assert not client.functions[READ].requires_confirmation


async def test_the_server_is_asked_for_its_resources_prompts_and_annotations(manager):
    await manager.connect("notes")

    (resource,) = await manager.get_resources("notes")
    (prompt,) = await manager.get_prompts("notes")
    annotations = await manager.tool_annotations("notes")

    assert (resource.uri, resource.mime_type) == ("notes://index", "text/plain")
    assert resource.description == "Every note's title."
    assert (prompt.name, prompt.arguments) == ("summarise", ["title"])
    assert annotations["read_note"].read_only_hint is True
    assert annotations["delete_note"] is None


async def test_a_server_not_connected_has_nothing_to_say(manager):
    assert await manager.get_resources("notes") == []
    assert await manager.get_prompts("notes") == []
    assert await manager.tool_annotations("notes") == {}
    assert manager.get_tools("notes") == []
    assert manager.get_tool_descriptions("notes") == {}


async def test_a_server_without_resources_or_prompts_has_none(tmp_path):
    mcp = MCPClientManager(
        project_dir=_project(tmp_path, TOOLS_ONLY_SERVER, name="bare"), policy=MCPPolicy()
    )
    try:
        assert (await mcp.connect("bare")).ok
        assert mcp.get_tools("bare") == ["mcp_bare_ping"]

        assert await mcp.get_resources("bare") == []
        assert await mcp.get_prompts("bare") == []
    finally:
        await mcp.disconnect_all()


async def test_disconnecting_one_server_forgets_it(manager):
    await manager.connect("notes")

    assert await manager.disconnect_one("notes") is True
    assert await manager.disconnect_one("notes") is False
    assert manager.list_connected() == []
    assert manager.get_client("notes") is None


async def test_a_server_with_no_tools_is_closed_and_reported(tmp_path):
    mcp = MCPClientManager(
        project_dir=_project(tmp_path, EMPTY_SERVER, name="empty"), policy=MCPPolicy()
    )

    result = await mcp.connect("empty")

    assert not result.ok
    assert "returned no tools" in result.reason
    assert mcp.list_connected() == []


async def test_a_command_that_cannot_start_is_a_failure_not_a_crash(tmp_path):
    project = _project(tmp_path)
    mcp = MCPClientManager(project_dir=project, policy=MCPPolicy())
    mcp.configs["notes"] = mcp.configs["notes"].model_copy(
        update={"command": str(tmp_path / "no-such-binary")}
    )

    result = await mcp.connect("notes")

    assert not result.ok
    assert result.reason


# ── The gates in front of a connection ────────────────────────────────


async def test_managed_policy_denies_by_name(tmp_path):
    mcp = MCPClientManager(project_dir=_project(tmp_path), policy=MCPPolicy(denied=["notes"]))

    result = await mcp.connect("notes")

    assert result.reason == "Server 'notes' is blocked by admin policy"
    assert mcp.is_policy_denied("notes")


async def test_managed_policy_allows_only_what_it_lists(tmp_path):
    mcp = MCPClientManager(project_dir=_project(tmp_path), policy=MCPPolicy(allowed=["other"]))

    result = await mcp.connect("notes")

    assert result.reason == "Server 'notes' is not in the allowed list"


async def test_a_required_server_is_listed_until_it_connects(manager):
    manager._policy = MCPPolicy(required=["notes"])

    assert manager.list_required() == ["notes"]
    await manager.connect("notes")
    assert manager.list_required() == []


async def test_a_server_nobody_approved_does_not_start_without_a_terminal(tmp_path):
    project = tmp_path / "unapproved"
    project.mkdir()
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"notes": {"command": sys.executable, "args": ["-c", "1"]}}})
    )
    mcp = MCPClientManager(project_dir=project, policy=MCPPolicy())

    result = await mcp.connect("notes")

    assert result.reason == "User denied MCP server connection"


async def test_an_air_gap_refuses_a_url_server_outside_the_network(tmp_path):
    mcp = MCPClientManager(
        project_dir=tmp_path,
        policy=MCPPolicy(),
        network_policy=NetworkPolicy(air_gapped=True, internal_hosts=["mcp.corp.example"]),
    )
    mcp.configs["remote"] = MCPServerConfig(
        name="remote", type=MCPTransport.sse, url="https://mcp.example.com/sse"
    )

    result = await mcp.connect("remote")

    assert not result.ok
    assert "mcp.example.com" in result.reason
    assert mcp.list_connected() == []


async def test_an_sse_server_without_a_url_says_so(tmp_path):
    mcp = MCPClientManager(project_dir=tmp_path, policy=MCPPolicy())
    mcp.configs["remote"] = MCPServerConfig(
        name="remote", type=MCPTransport.sse, source="extension"
    )

    result = await mcp.connect("remote")

    assert result.reason == "SSE transport requires a 'url' field"


async def test_an_sse_server_that_cannot_be_reached_is_a_failure(tmp_path):
    mcp = MCPClientManager(project_dir=tmp_path, policy=MCPPolicy())
    mcp.configs["remote"] = MCPServerConfig(
        name="remote", type=MCPTransport.sse, url="http://127.0.0.1:9/sse", source="extension"
    )

    result = await mcp.connect("remote")

    assert not result.ok
    assert mcp.list_connected() == []
