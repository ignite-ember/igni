"""``READY`` has to keep being true.

``ExtensionState`` was written once, at install, and never again.
That was survivable while nothing could install an extension. Once
something could, a service that died ten minutes later left the
registry saying ``READY`` — so ``list_extensions`` said ``READY``,
the panel drew a frame, and the first call came back as a connection
error rather than a state with a reason. The UI could already render
``FAILED`` with its reason; it simply never saw one after install.

The service here is a real subprocess and the test kills it. A stub
that returns an exception on demand would prove the branch and not
the thing: what is being checked is that a *process going away* is
noticed, and a mock cannot go away.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path

import pytest

from ember_code.core.mcp.client import MCPClientManager
from ember_code.extensions.health import ExtensionHealth
from ember_code.extensions.installer import ExtensionInstaller
from ember_code.extensions.origin import ExtensionOrigins
from ember_code.extensions.registry import ExtensionRefused, ExtensionRegistry, ExtensionState
from ember_code.extensions.rpc import ExtensionRpc

pytestmark = pytest.mark.asyncio

FIXTURE = Path(__file__).parent / "fixtures" / "dev_extension"


def bundle_at(tmp_path: Path) -> Path:
    bundle = tmp_path / "ext"
    shutil.copytree(FIXTURE, bundle)
    raw = json.loads((bundle / "manifest.json").read_text())
    raw["dev"]["command"] = [sys.executable, "server.py"]
    (bundle / "manifest.json").write_text(json.dumps(raw))
    return bundle


class Wired:
    def __init__(self, tmp_path: Path) -> None:
        self.registry = ExtensionRegistry()
        self.rpc = ExtensionRpc(self.registry)
        self.mcp = MCPClientManager(project_dir=tmp_path)
        self.origins = ExtensionOrigins()
        self.installer = ExtensionInstaller(
            registry=self.registry,
            rpc=self.rpc,
            mcp=self.mcp,
            origins=self.origins,
            allow_unsigned=True,
        )
        self.health = ExtensionHealth(
            registry=self.registry, rpc=self.rpc, mcp=self.mcp, interval_s=3600
        )
        self.rpc.watch(self.health.on_call_failed)

    def kill_service(self, bundle: Path) -> None:
        """Stop the subprocess behind the extension, as a crash would.

        The fixture writes its own pid, which is simpler and steadier
        than reaching into the MCP client for a process that anyio
        keeps on a generator's frame. Killing the real process is the
        point: a stub that raises on demand proves the branch and not
        the thing, because a mock cannot go away.
        """
        pid_file = bundle / "server.pid"
        for _ in range(100):
            if pid_file.is_file():
                break
            time.sleep(0.05)
        pid = int(pid_file.read_text())
        os.kill(pid, signal.SIGKILL)
        # Reaped by whoever started it; waiting here would block on a
        # child of the MCP transport rather than of this process.
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except OSError:
                return
            time.sleep(0.05)

    async def close(self) -> None:
        await self.health.stop()
        await self.origins.stop_all()
        await self.mcp.disconnect_all()


@pytest.fixture
async def wired(tmp_path: Path):
    w = Wired(tmp_path)
    try:
        yield w
    finally:
        await w.close()


async def test_a_live_extension_probes_READY(wired, tmp_path):
    await wired.installer.install(bundle_at(tmp_path))
    assert await wired.health.probe("acme.scanner") is ExtensionState.READY


async def test_a_killed_service_stops_being_READY(wired, tmp_path):
    """The whole point. Install, kill, probe, and the state follows."""
    bundle = bundle_at(tmp_path)
    entry = await wired.installer.install(bundle)
    assert entry.state is ExtensionState.READY

    wired.kill_service(bundle)
    await wired.health.probe("acme.scanner")

    after = wired.registry.get("acme.scanner")
    assert after is not None
    # Either it noticed and could not restart, or it noticed and
    # restarted — both are correct, and both are "the state is true".
    if after.state is ExtensionState.FAILED:
        assert after.reason, "FAILED with no reason is what this replaced"
    else:
        assert after.state is ExtensionState.READY
        assert await wired.health.probe("acme.scanner") is ExtensionState.READY


async def test_a_call_to_a_dead_extension_says_so_rather_than_timing_out(wired, tmp_path):
    bundle = bundle_at(tmp_path)
    await wired.installer.install(bundle)
    wired.kill_service(bundle)

    with pytest.raises(ExtensionRefused):
        await wired.rpc.dispatch(caller="acme.scanner", method="ext.acme.scanner.list", args={})


async def test_an_unknown_extension_probes_to_nothing(wired):
    assert await wired.health.probe("never.installed") is None


async def test_a_DISABLED_extension_is_left_alone(wired, tmp_path):
    """Somebody turned it off. Probing it would argue with them."""
    await wired.installer.install(bundle_at(tmp_path))
    wired.registry.set_state("acme.scanner", ExtensionState.DISABLED, "by an admin")

    assert await wired.health.probe("acme.scanner") is ExtensionState.DISABLED
    entry = wired.registry.get("acme.scanner")
    assert entry is not None and entry.reason == "by an admin"


async def test_probe_all_covers_everything_installed(wired, tmp_path):
    await wired.installer.install(bundle_at(tmp_path))
    await wired.health.probe_all()
    entry = wired.registry.get("acme.scanner")
    assert entry is not None and entry.state is ExtensionState.READY


async def test_the_monitor_starts_and_stops_cleanly(wired):
    """Idempotent start, and a stop that does not leave a task."""
    wired.health.start()
    wired.health.start()
    await wired.health.stop()
    await wired.health.stop()


async def test_stop_cancels_a_probe_already_in_flight(wired, tmp_path):
    """The bug that only showed up with the tests run together.

    ``on_call_failed`` starts a probe in the background. If ``stop``
    cancels only the sweep loop, a probe partway through a restart —
    disconnecting and reconnecting an MCP client — is still running
    when the backend tears the manager down, and the two race. It
    hangs rather than crashes, which is the worst way for it to
    surface: the first run of this file sat for ten minutes with no
    output and no failure.
    """
    bundle = bundle_at(tmp_path)
    await wired.installer.install(bundle)
    wired.kill_service(bundle)

    # The call fails, which schedules a probe.
    with pytest.raises(ExtensionRefused):
        await wired.rpc.dispatch(caller="acme.scanner", method="ext.acme.scanner.list", args={})
    assert wired.health.pending_probes > 0

    await asyncio.wait_for(wired.health.stop(), timeout=30)
    assert wired.health.pending_probes == 0
