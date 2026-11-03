"""An extension whose process exits at start-up says why, and keeps saying it.

What a developer saw was ``its service did not start: MCP server connected
but returned no tools. Ensure the IDE has MCP support enabled.`` — about an
IDE nobody was using, with the process's own last words (``config file
missing``) written to a temp file nobody was told about. And the first
health probe then replaced even that with ``its service is not connected.``

The service here is a real subprocess that prints to stderr and exits.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ember_code.core.mcp.client import MCPClientManager
from ember_code.core.mcp.stdio_binding import StdioMCPBinding
from ember_code.extensions.health import ExtensionHealth
from ember_code.extensions.installer import ExtensionInstaller
from ember_code.extensions.origin import ExtensionOrigins
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState
from ember_code.extensions.rpc import ExtensionRpc

pytestmark = pytest.mark.asyncio

SERVER = """\
import sys
print("loading settings from /etc/acme/crashy.toml", file=sys.stderr)
print("config file missing (simulated)", file=sys.stderr)
sys.exit(3)
"""


def crashy_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "crashy"
    bundle.mkdir()
    (bundle / "server.py").write_text(SERVER)
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "id": "acme.crashy",
                "version": "0.1.0",
                "contract": ">=1.0 <2",
                "name": "Crashy",
                "trust": "unsigned",
                "dev": {"command": [sys.executable, "server.py"]},
            }
        )
    )
    return bundle


@pytest.fixture
async def wired(tmp_path: Path):
    registry = ExtensionRegistry()
    rpc = ExtensionRpc(registry)
    mcp = MCPClientManager(
        project_dir=tmp_path, stdio_binding=StdioMCPBinding(tmp_path / "mcp_stderr.log")
    )
    origins = ExtensionOrigins()
    installer = ExtensionInstaller(
        registry=registry, rpc=rpc, mcp=mcp, origins=origins, allow_development=True
    )
    health = ExtensionHealth(registry=registry, rpc=rpc, mcp=mcp, interval_s=3600)
    try:
        yield registry, installer, health
    finally:
        await health.stop()
        await origins.stop_all()
        await mcp.disconnect_all()


async def test_the_reason_carries_the_process_s_last_words(wired, tmp_path):
    registry, installer, _ = wired
    await installer.install(crashy_bundle(tmp_path))

    entry = registry.get("acme.crashy")
    assert entry is not None
    assert entry.state is ExtensionState.FAILED
    assert "config file missing (simulated)" in entry.reason
    assert "IDE" not in entry.reason


async def test_a_probe_and_a_failed_restart_keep_the_first_cause(wired, tmp_path):
    registry, installer, health = wired
    await installer.install(crashy_bundle(tmp_path))

    first = registry.get("acme.crashy")
    assert first is not None
    first_reason = first.reason

    # The first probe restarts it; the second is inside the restart
    # back-off, so it only finds the service "not connected" — which
    # is true and is not why.
    for _ in range(2):
        assert await health.probe("acme.crashy") is ExtensionState.FAILED
        entry = registry.get("acme.crashy")
        assert entry is not None
        assert "config file missing (simulated)" in entry.reason
        assert "IDE" not in entry.reason
        assert entry.reason.startswith(first_reason)


async def test_a_server_that_is_not_an_extension_keeps_the_generic_wording(tmp_path):
    """The IDE hint is for the IDE's MCP server, which is where it came from."""
    from ember_code.core.mcp.client import _no_tools_reason

    assert "IDE" not in _no_tools_reason("ext:acme.crashy", stderr_tail="")
    assert "no tools" in _no_tools_reason("ide", stderr_tail="")
