"""Tests for mcp/transport.py — stdio transport for MCP servers."""

import asyncio

from ember_code.core.mcp.transport import StdioTransport


class TestStdioTransport:
    def test_stores_command_and_args(self):
        t = StdioTransport("node", args=["server.js"], env={"KEY": "val"})
        assert t.command == "node"
        assert t.args == ["server.js"]
        assert t.env == {"KEY": "val"}

    def test_defaults(self):
        t = StdioTransport("python")
        assert t.args == []
        assert t.env == {}
        assert t._process is None

    def test_stdin_none_before_start(self):
        t = StdioTransport("echo")
        assert t.stdin is None

    def test_stdout_none_before_start(self):
        t = StdioTransport("echo")
        assert t.stdout is None

    def test_stop_without_start(self):
        t = StdioTransport("echo")
        asyncio.run(t.stop())  # should not raise
