"""The client is told enough to render an installed extension.

Before this, an installed extension answered calls and had nowhere to
put them: nothing told the frontend which extensions existed, what
state each was in, or which loopback port its UI was on. The frame
component had been built and tested for months against a stub window
and was never mounted, because there was no way to know what to
mount.

``list_extensions`` is the base side of that. It is deliberately not
the manifest: a manifest holds the packaging, the egress allowlist
and the sidecar image, and none of those belong in a browser's memory
or in anything that logs a WebSocket frame.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from ember_code.backend.rpc_handlers.base import RpcHandlerContext
from ember_code.backend.rpc_handlers.extensions import ExtensionsRpcHandler
from ember_code.core.mcp.client import MCPClientManager
from ember_code.extensions.installer import ExtensionInstaller
from ember_code.extensions.origin import ExtensionOrigins
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState
from ember_code.extensions.rpc import ExtensionRpc

pytestmark = pytest.mark.asyncio

FIXTURE = Path(__file__).parent / "fixtures" / "dev_extension"


def bundle_at(tmp_path: Path) -> Path:
    bundle = tmp_path / "ext"
    shutil.copytree(FIXTURE, bundle)
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["dev"]["command"] = [sys.executable, "server.py"]
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    return bundle


def handler(extensions: ExtensionRpc | None, origins: ExtensionOrigins | None):
    ctx = RpcHandlerContext(
        backend=None,
        transport=None,
        login=None,  # type: ignore[arg-type]
        push=None,  # type: ignore[arg-type]
        extensions=extensions,
        origins=origins,
    )
    return ExtensionsRpcHandler(ctx)


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
            allow_development=True,
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


async def test_an_installed_extension_is_listed_with_what_it_takes_to_draw(wired, tmp_path):
    await wired.installer.install(bundle_at(tmp_path))
    result = await handler(wired.rpc, wired.origins).list_extensions({})

    (summary,) = result.extensions
    assert summary.id == "acme.scanner"
    assert summary.name == "Compliance Scanner (dev)"
    assert summary.state == ExtensionState.READY.value
    assert summary.reason == ""
    assert summary.origin.startswith("http://127.0.0.1:")
    assert summary.allow == ["ext.acme.scanner.*"]
    assert [p.kind for p in summary.pages] == ["acme.scan"]
    assert summary.pages[0].sidebar is True
    assert summary.pages[0].entry == "index.html"


async def test_the_manifest_itself_does_not_go_to_the_browser(wired, tmp_path):
    """Only what is needed to draw.

    The packaging, the egress allowlist and the sidecar image are
    none of a frontend's business, and sending them would put each of
    them in anything that logs a frame.
    """
    await wired.installer.install(bundle_at(tmp_path))
    result = await handler(wired.rpc, wired.origins).list_extensions({})

    fields = set(result.extensions[0].model_dump())
    assert fields == {
        "id",
        "name",
        "version",
        "state",
        "reason",
        "origin",
        "allow",
        "pages",
        "menu",
        # Status chips in the host's own chrome. The host renders
        # them; the extension decides what they say.
        "indicators",
        # The settings *shape*, so the host can render a form, and
        # the configured *values*, which become the frame's launch
        # parameters. Neither is sensitive by contract: a secret is
        # declared in `permissions.secrets` and resolved by the
        # deployment, never typed into a setting — `Setting.type` has
        # no password among its four.
        "settings",
        "values",
    }


async def test_a_broken_extension_is_listed_with_its_reason(wired, tmp_path):
    """Not filtered out.

    An administrator asking "why is Compliance missing" is better
    served by a row that says why than by an absence they have to
    interpret.
    """
    bundle = bundle_at(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["dev"]["command"] = [sys.executable, "no-such-file.py"]
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    await wired.installer.install(bundle)

    (summary,) = (await handler(wired.rpc, wired.origins).list_extensions({})).extensions
    assert summary.state == ExtensionState.FAILED.value
    assert summary.reason


async def test_a_backend_without_extension_support_answers_empty(tmp_path):
    """Truthful rather than an error: nothing is installed, and
    nothing can be."""
    result = await handler(None, None).list_extensions({})
    assert result.extensions == []


async def test_an_extension_with_no_ui_has_no_origin(wired, tmp_path):
    bundle = bundle_at(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text())
    del manifest["contributes"]
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    await wired.installer.install(bundle)

    (summary,) = (await handler(wired.rpc, wired.origins).list_extensions({})).extensions
    assert summary.state == ExtensionState.READY.value
    assert summary.origin == ""
    assert summary.pages == []


async def test_the_listing_is_stable(wired, tmp_path):
    """Sorted by id, so the sidebar does not reorder itself between
    calls."""
    await wired.installer.install(bundle_at(tmp_path))
    first = await handler(wired.rpc, wired.origins).list_extensions({})
    second = await handler(wired.rpc, wired.origins).list_extensions({})
    assert [e.id for e in first.extensions] == [e.id for e in second.extensions]
