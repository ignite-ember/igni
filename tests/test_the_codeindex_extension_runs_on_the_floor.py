"""The first real capability, installed as an extension.

Everything before this was examples: `reference-extension` and
`release-notes` are fictional vendors written to exercise the floor.
This installs the actual CodeIndex extension from
``igni__code-index/extension`` and drives it, which is a different
kind of check — an example can be written around a gap, and a real
capability cannot.

What it proves, and what it does not. CodeIndex's service-backed
half works here: the status chip, the graph-schema fragment, the
page, the menu entry, the command and the agent variant all arrive
from its manifest. Its *local index* half — the Cypher tool — is
still inside igni at ``core/code_index/`` and is deliberately not
declared, because a tool that answers plausibly and wrongly is worse
than one that is not offered.

Skipped when the sibling repository is not checked out, because this
suite has to pass in a clone of this repository alone.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from ember_code.core.agents.variants import VariantRegistry
from ember_code.core.contributed_commands import ContributedCommands
from ember_code.core.mcp.client import MCPClientManager
from ember_code.core.prompts.fragments import PromptFragments
from ember_code.extensions.contributions import ExtensionContributions
from ember_code.extensions.installer import ExtensionInstaller
from ember_code.extensions.manifest import ExtensionManifest
from ember_code.extensions.origin import ExtensionOrigins
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState
from ember_code.extensions.rpc import ExtensionRpc

EXTENSION = Path(__file__).resolve().parents[2] / "igni__code-index" / "extension"

pytestmark = pytest.mark.skipif(
    not (EXTENSION / "manifest.json").is_file(),
    reason="igni__code-index is not checked out beside this repository",
)


def bundle_at(tmp_path: Path) -> Path:
    """The real bundle, started by this interpreter.

    The manifest says `igni-codeindex-extension`, its installed
    console script. A test has not installed it, so the command
    becomes this interpreter running the package with its source on
    `PYTHONPATH` — which is what `dev.env` is for.
    """
    bundle = tmp_path / "codeindex"
    shutil.copytree(EXTENSION, bundle)
    raw = json.loads((bundle / "manifest.json").read_text())
    raw["dev"]["command"] = [sys.executable, "-m", "igni_codeindex"]
    raw["dev"]["env"] = {"PYTHONPATH": str(bundle / "src")}
    (bundle / "manifest.json").write_text(json.dumps(raw))
    return bundle


class Wired:
    def __init__(self, tmp_path: Path) -> None:
        self.registry = ExtensionRegistry()
        self.rpc = ExtensionRpc(self.registry)
        self.mcp = MCPClientManager(project_dir=tmp_path)
        self.origins = ExtensionOrigins()
        self.fragments = PromptFragments()
        self.variants = VariantRegistry()
        self.commands = ContributedCommands()
        self.contributions = ExtensionContributions(
            registry=self.registry,
            rpc=self.rpc,
            fragments=self.fragments,
            variants=self.variants,
            commands=self.commands,
        )
        self.contributions.attach()
        self.installer = ExtensionInstaller(
            registry=self.registry,
            rpc=self.rpc,
            mcp=self.mcp,
            origins=self.origins,
            project_dir=tmp_path,
            secret_resolver=lambda name: "test-token" if name == "codeindex-token" else None,
            allow_unsigned=True,
        )

    async def install(self, bundle: Path):
        self.contributions.note_bundle("igni.codeindex", bundle)
        entry = await self.installer.install(bundle)
        await self.contributions.apply(entry.manifest)
        return entry

    async def call(self, tool: str, **args: object):
        return await self.rpc.dispatch(
            caller="igni.codeindex",
            method=f"ext.igni.codeindex.{tool}",
            args=args,
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


@pytest.mark.asyncio
async def test_it_installs_and_starts(wired, tmp_path):
    entry = await wired.install(bundle_at(tmp_path))
    assert entry.state is ExtensionState.READY, entry.reason


@pytest.mark.asyncio
async def test_its_indicator_answers_what_the_host_will_draw(wired, tmp_path):
    """The contribution added for exactly this extension.

    Unconfigured here — no service URL — and that is the interesting
    case: an unconfigured CodeIndex is not a broken one, so the chip
    is neutral rather than red. Colouring it red teaches people to
    ignore red.
    """
    await wired.install(bundle_at(tmp_path))
    state = await wired.call("indicator", commit_sha="HEAD")

    assert state["tone"] == "neutral"
    assert state["label"] == "not set up"
    # The four the design system publishes, and no others — the host
    # refuses anything else and draws nothing.
    assert state["tone"] in {"neutral", "success", "warning", "danger"}


@pytest.mark.asyncio
async def test_its_graph_schema_fills_the_anchor_it_was_built_for(wired, tmp_path):
    """`{{CODEINDEX_GRAPH_SCHEMA}}` is the placeholder the whole
    fragment system was generalised from."""
    await wired.install(bundle_at(tmp_path))

    filled = wired.fragments.expand("Schema:\n{{CODEINDEX_GRAPH_SCHEMA}}")
    assert "property graph" in filled
    assert "{{" not in filled


@pytest.mark.asyncio
async def test_it_contributes_everything_its_manifest_declares(wired, tmp_path):
    """Nothing lost: each thing CodeIndex puts into the host today
    arrives from the manifest."""
    entry = await wired.install(bundle_at(tmp_path))
    contributes = entry.manifest.contributes

    assert [p.kind for p in contributes.pages] == ["igni.codeindex"]
    assert [m.command for m in contributes.menu] == ["/codeindex"]
    assert wired.commands.get("codeindex") is not None
    assert "codeindex" in wired.variants.active()
    assert "CODEINDEX_GRAPH_SCHEMA" in wired.fragments.anchors()
    assert [i.id for i in contributes.indicators] == ["igni.codeindex.freshness"]


@pytest.mark.asyncio
async def test_its_status_tool_says_when_it_is_unconfigured(wired, tmp_path):
    """Rather than failing. A deployment that has not pointed this at
    a service is a normal state, not an error."""
    await wired.install(bundle_at(tmp_path))
    answer = await wired.call("status", commit_sha="HEAD")
    assert answer == {"configured": False}


def test_its_manifest_matches_the_published_schema():
    """The one check that does not need the extension to run.

    It is the author-facing contract: a manifest igni's installer
    accepts and an author's editor rejects is a worse failure than
    either alone.
    """
    manifest = ExtensionManifest.load(EXTENSION / "manifest.json")
    assert manifest.id == "igni.codeindex"
    assert manifest.grants_rpc("ext.igni.codeindex.indicator")
    # The local-index tool is deliberately absent until the index
    # moves here; declaring it would promise an answer nothing gives.
    assert not any(c.command == "/codeindex-cypher" for c in manifest.contributes.commands)
