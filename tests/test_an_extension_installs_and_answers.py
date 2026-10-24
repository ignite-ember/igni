"""An extension is installed, starts, and answers a call.

Everything else in the extension system was reachable only from a
test before this: the manifest was modelled, ``ext.<id>.*`` was
routed and authorised, the frame was built and the floor was
published, and no code path anywhere called
``ExtensionRegistry.install``.

So this is the end-to-end one, and it uses a real subprocess on
purpose. ``tests/fixtures/dev_extension`` is an actual MCP server;
the installer starts it over stdio exactly as a development
extension is started, and the call goes through the same
``ExtensionRpc.dispatch`` the dispatcher uses. A stub here would
have proved the wiring agreed with itself.

The command is rewritten to the running interpreter, because the
fixture says ``python`` and the one on PATH is not necessarily the
one with the SDK.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from ember_code.core.mcp.client import MCPClientManager
from ember_code.extensions.installer import (
    ExtensionInstaller,
    InstallError,
    server_name,
)
from ember_code.extensions.origin import ExtensionOrigins
from ember_code.extensions.registry import (
    ExtensionRefused,
    ExtensionRegistry,
    ExtensionState,
)
from ember_code.extensions.rpc import ExtensionRpc

pytestmark = pytest.mark.asyncio

FIXTURE = Path(__file__).parent / "fixtures" / "dev_extension"


def bundle_at(tmp_path: Path, **manifest_changes: object) -> Path:
    """A copy of the fixture, runnable by this interpreter."""
    bundle = tmp_path / "ext"
    shutil.copytree(FIXTURE, bundle)
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["dev"]["command"] = [sys.executable, "server.py"]
    manifest.update(manifest_changes)
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    return bundle


class Wired:
    """A registry, a router and an installer over a real MCP manager."""

    def __init__(self, tmp_path: Path, *, allow_development: bool = True) -> None:
        self.registry = ExtensionRegistry()
        self.rpc = ExtensionRpc(self.registry)
        self.mcp = MCPClientManager(project_dir=tmp_path)
        self.origins = ExtensionOrigins()
        self.installer = ExtensionInstaller(
            registry=self.registry,
            rpc=self.rpc,
            mcp=self.mcp,
            origins=self.origins,
            allow_development=allow_development,
        )

    async def close(self) -> None:
        await self.origins.stop_all()
        await self.mcp.disconnect_all()


@pytest.fixture
async def wired(tmp_path: Path):
    w = Wired(tmp_path)
    try:
        yield w
    finally:
        await w.close()


# ── The round trip ─────────────────────────────────────────────────


async def test_an_installed_extension_answers_its_own_namespace(wired, tmp_path):
    entry = await wired.installer.install(bundle_at(tmp_path))

    assert entry.state is ExtensionState.READY, entry.reason
    assert wired.registry.ids() == ["acme.scanner"]

    violations = await wired.rpc.dispatch(
        caller="acme.scanner", method="ext.acme.scanner.list", args={}
    )
    assert violations == [
        {
            "id": "v1",
            "rule": "no-plaintext-secrets",
            "severity": "critical",
            "path": "src/config.py",
            "line": 12,
        }
    ]


async def test_json_text_arrives_as_a_value_not_a_string(wired, tmp_path):
    """An MCP tool answers with content built for a model to read.

    A UI wants data, so a server written for one sends JSON and the
    service layer parses it. Without that every extension writes the
    same three lines.
    """
    await wired.installer.install(bundle_at(tmp_path))
    status = await wired.rpc.dispatch(
        caller="acme.scanner", method="ext.acme.scanner.status", args={}
    )
    assert status["state"] == "idle"


async def test_a_failing_tool_is_a_sentence_not_a_crash(wired, tmp_path):
    await wired.installer.install(bundle_at(tmp_path))
    with pytest.raises(ExtensionRefused, match="fell over"):
        await wired.rpc.dispatch(caller="acme.scanner", method="ext.acme.scanner.explode", args={})


async def test_the_permission_check_still_applies_to_a_live_extension(wired, tmp_path):
    """Authorisation is not a property of the stub it was tested with."""
    await wired.installer.install(bundle_at(tmp_path))
    with pytest.raises(ExtensionRefused, match="reaches its own namespace"):
        await wired.rpc.dispatch(caller="someone.else", method="ext.acme.scanner.list", args={})


# ── Failure, recorded rather than raised ───────────────────────────


async def test_a_service_that_will_not_start_is_FAILED_with_a_reason(wired, tmp_path):
    """An extension that failed to start is a state, not an exception.

    An administrator needs to see it, with the reason, which means it
    has to be in the registry rather than in a log nobody reads.
    """
    bundle = bundle_at(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["dev"]["command"] = [sys.executable, "no-such-file.py"]
    (bundle / "manifest.json").write_text(json.dumps(manifest))

    entry = await wired.installer.install(bundle)
    assert entry.state is ExtensionState.FAILED
    assert entry.reason

    with pytest.raises(ExtensionRefused, match="not READY"):
        await wired.rpc.dispatch(caller="acme.scanner", method="ext.acme.scanner.list", args={})


async def test_a_sidecar_says_what_is_not_implemented(wired, tmp_path):
    """Rather than appearing to work.

    Container supervision does not exist, and an install that quietly
    succeeded would be discovered at the first call.
    """
    bundle = bundle_at(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text())
    del manifest["dev"]
    manifest["sidecar"] = {
        "image": "acme__scanner:1.0.0",
        "mcp": {"transport": "stdio"},
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest))

    entry = await wired.installer.install(bundle)
    assert entry.state is ExtensionState.FAILED
    assert "not implemented yet" in entry.reason
    assert "acme__scanner:1.0.0" in entry.reason


# ── The UI ─────────────────────────────────────────────────────────


async def test_a_page_contributing_extension_gets_its_own_origin(wired, tmp_path):
    """One loopback port each.

    A port is part of an origin and a path prefix is not, so two
    extensions sharing one origin could read each other's storage and
    script each other's frames.
    """
    await wired.installer.install(bundle_at(tmp_path))
    url = wired.origins.url_for("acme.scanner")
    assert url is not None
    assert url.startswith("http://127.0.0.1:")


async def test_pages_with_no_ui_directory_is_FAILED_not_READY(wired, tmp_path):
    """A sidebar entry that opens on nothing is worse than no entry."""
    bundle = bundle_at(tmp_path)
    shutil.rmtree(bundle / "ui")

    entry = await wired.installer.install(bundle)
    assert entry.state is ExtensionState.FAILED
    assert "no 'ui' directory" in entry.reason

    # And the service is unbound, so the half-installed thing cannot
    # be called either.
    with pytest.raises(ExtensionRefused, match="not READY"):
        await wired.rpc.dispatch(caller="acme.scanner", method="ext.acme.scanner.list", args={})


async def test_a_headless_backend_says_so_rather_than_half_installing(tmp_path):
    w = Wired(tmp_path)
    w.installer = ExtensionInstaller(
        registry=w.registry,
        rpc=w.rpc,
        mcp=w.mcp,
        origins=None,
        allow_development=True,
    )
    try:
        entry = await w.installer.install(bundle_at(tmp_path))
        assert entry.state is ExtensionState.FAILED
        assert "serves no extension UI" in entry.reason
    finally:
        await w.close()


async def test_a_headless_extension_needs_no_origin(wired, tmp_path):
    """Contributing no pages is normal, not a degraded install."""
    bundle = bundle_at(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text())
    del manifest["contributes"]
    (bundle / "manifest.json").write_text(json.dumps(manifest))

    entry = await wired.installer.install(bundle)
    assert entry.state is ExtensionState.READY, entry.reason
    assert wired.origins.url_for("acme.scanner") is None


async def test_uninstall_releases_the_port(wired, tmp_path):
    await wired.installer.install(bundle_at(tmp_path))
    assert "acme.scanner" in wired.origins
    await wired.installer.uninstall("acme.scanner")
    assert "acme.scanner" not in wired.origins


# ── Refusals, before anything is registered ────────────────────────


async def test_a_bundle_with_no_manifest_is_refused(wired, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(InstallError, match="no manifest.json"):
        await wired.installer.install(empty)
    assert wired.registry.ids() == []


async def test_an_invalid_manifest_names_the_file(wired, tmp_path):
    bundle = bundle_at(tmp_path)
    (bundle / "manifest.json").write_text('{"id": "nope"}')
    with pytest.raises(InstallError, match="manifest.json"):
        await wired.installer.install(bundle)
    assert wired.registry.ids() == []


async def test_a_development_install_is_refused_unless_the_flag_is_set(tmp_path):
    """The easiest way to ship must not be the one with no provenance."""
    w = Wired(tmp_path, allow_development=False)
    try:
        with pytest.raises(InstallError, match="IGNI_DEV_EXTENSIONS"):
            await w.installer.install(bundle_at(tmp_path))
        assert w.registry.ids() == []
    finally:
        await w.close()


async def test_installing_twice_is_refused(wired, tmp_path):
    """An upgrade keeps the old grants until an administrator accepts
    the new ones, and that is a separate decision from this one."""
    await wired.installer.install(bundle_at(tmp_path))
    with pytest.raises(InstallError, match="already installed"):
        await wired.installer.install(bundle_at(tmp_path / "second"))


# ── Uninstall ──────────────────────────────────────────────────────


async def test_uninstall_stops_answering_and_forgets(wired, tmp_path):
    await wired.installer.install(bundle_at(tmp_path))
    assert await wired.installer.uninstall("acme.scanner") is True
    assert wired.registry.ids() == []
    with pytest.raises(ExtensionRefused, match="no extension"):
        await wired.rpc.dispatch(caller="acme.scanner", method="ext.acme.scanner.list", args={})


async def test_uninstalling_something_absent_says_so(wired):
    assert await wired.installer.uninstall("never.installed") is False


async def test_the_server_name_cannot_shadow_a_users_own(wired, tmp_path):
    """An extension called `github` must not take over the user's
    `github` MCP server."""
    await wired.installer.install(bundle_at(tmp_path))
    assert server_name("acme.scanner") in wired.mcp.configs
    assert "acme.scanner" not in wired.mcp.configs
