"""Artifacts, installed as an extension and driven through igni.

The second real capability, and the one that tests the floor
hardest: 12,700 lines, 59 RPCs, a graph store it has to be given,
and an agent of its own. CodeIndex proved an extension could answer
a question. This proves one can *be* a subsystem.

What it checks, and what it does not. The extension installs
through igni's real installer, its requirements are resolved and
reach the service, the plugin it carries lands where the agent
loader looks, and its tools answer over MCP. What it cannot check
here is the document store working, because that needs a Neo4j —
so the install is the unconfigured one, and the thing proved is
that an extension with no infrastructure *says so* rather than
failing. A capability that crashed when its store was absent would
be one you could never install before provisioning, which is the
wrong way round.

Skipped when the sibling repository is not checked out, because
this suite has to pass in a clone of this repository alone.
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
from ember_code.core.mcp.naming import bare_tool_name
from ember_code.core.prompts.fragments import PromptFragments
from ember_code.extensions.capabilities import Resolved
from ember_code.extensions.contributions import ExtensionContributions
from ember_code.extensions.installer import ExtensionInstaller
from ember_code.extensions.manifest import Capability
from ember_code.extensions.origin import ExtensionOrigins
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState
from ember_code.extensions.rpc import ExtensionRpc

EXTENSION = Path(__file__).resolve().parents[2] / "igni__artifacts" / "extension"

pytestmark = pytest.mark.skipif(
    not (EXTENSION / "manifest.json").is_file(),
    reason="igni__artifacts is not checked out beside this repository",
)


class StubResolver:
    """A deployment that has a graph store and an embedder.

    Returns connections, never processes — which is the whole
    contract: on a laptop igni would have provisioned one first and
    in a deployment it would have bound to the infrastructure's,
    and the extension cannot tell.
    """

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def resolve(self, requirement, *, extension_id: str) -> Resolved:
        self.asked.append(requirement.capability.value)
        if requirement.capability is Capability.EMBEDDER:
            return Resolved(
                capability=requirement.capability,
                uri="http://127.0.0.1:9100",
                how="bound to the deployment's",
            )
        return Resolved(
            capability=requirement.capability,
            uri="bolt://127.0.0.1:7687",
            secret="graph-password",
            how="provisioned",
        )


def bundle_at(tmp_path: Path) -> Path:
    """The real bundle, started by this interpreter."""
    bundle = tmp_path / "artifacts"
    shutil.copytree(EXTENSION, bundle)
    raw = json.loads((bundle / "manifest.json").read_text())
    raw["dev"]["command"] = [sys.executable, "-m", "igni_artifacts"]
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
        self.plugins_dir = tmp_path / "plugins"
        self.resolver = StubResolver()
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
            resolver=self.resolver,
            plugins_dir=self.plugins_dir,
            secret_resolver=lambda name: "test-token",
            allow_unsigned=True,
        )

    async def install(self, bundle: Path):
        self.contributions.note_bundle("igni.artifacts", bundle)
        entry = await self.installer.install(bundle)
        await self.contributions.apply(entry.manifest)
        return entry

    async def call(self, tool: str, **args: object):
        return await self.rpc.dispatch(
            caller="igni.artifacts",
            method=f"ext.igni.artifacts.{tool}",
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


pytestmark = [*(pytestmark if isinstance(pytestmark, list) else [pytestmark])]


@pytest.mark.asyncio
async def test_it_installs_and_comes_up_ready(wired, tmp_path):
    entry = await wired.install(bundle_at(tmp_path))
    assert entry.state is ExtensionState.READY, entry.reason


@pytest.mark.asyncio
async def test_its_requirements_are_resolved(wired, tmp_path):
    """Declared on the manifest, not under a packaging — which is
    what a `dev`-packaged extension needing a graph store exposed."""
    await wired.install(bundle_at(tmp_path))
    assert sorted(wired.resolver.asked) == ["embedder", "graph-db"]


@pytest.mark.asyncio
async def test_the_connection_reaches_the_service(wired, tmp_path):
    """Resolution used to end at a log line. The service was started
    with no way to reach what had just been resolved for it."""
    await wired.install(bundle_at(tmp_path))
    config = wired.mcp.configs["ext:igni.artifacts"]
    assert config.env["IGNI_CAPABILITY_GRAPH_DB_URI"] == "bolt://127.0.0.1:7687"
    assert config.env["IGNI_CAPABILITY_GRAPH_DB_SECRET"] == "graph-password"
    assert config.env["IGNI_CAPABILITY_EMBEDDER_URI"] == "http://127.0.0.1:9100"


@pytest.mark.asyncio
async def test_the_workspace_grant_reaches_it_too(wired, tmp_path):
    await wired.install(bundle_at(tmp_path))
    config = wired.mcp.configs["ext:igni.artifacts"]
    assert config.env["IGNI_WORKSPACE"] == str(tmp_path)


@pytest.mark.asyncio
async def test_the_archivist_lands_where_the_agent_loader_looks(wired, tmp_path):
    """The only agent allowed to touch the document store. Extracted
    without it, Artifacts would be a capability whose agent stayed
    behind pointing at tools that had gone."""
    await wired.install(bundle_at(tmp_path))
    archivist = wired.plugins_dir / "igni.artifacts" / "agents" / "archivist.md"
    assert archivist.is_file()
    assert "artifacts" in archivist.read_text().lower()


@pytest.mark.asyncio
async def test_it_answers_its_own_namespace(wired, tmp_path):
    """Through `ExtensionRpc.dispatch`, which is the path a frame
    takes — authorised against `permissions.rpc` on the installed
    manifest rather than trusted."""
    await wired.install(bundle_at(tmp_path))
    answer = await wired.call("status")
    # The grants are both present, so it opened a store — and the
    # graph behind the resolved URI is not listening, which is the
    # ordinary state at install time. Either shape is a *sentence*,
    # never an exception and never an empty reason.
    assert isinstance(answer, dict)
    if answer.get("configured") is False:
        assert answer["reason"], "configured:false with no reason says nothing"
    else:
        assert "documents" in answer or "enabled" in answer


@pytest.mark.asyncio
async def test_the_chip_renders_without_a_store(wired, tmp_path):
    """igni draws nothing when an indicator source errors, so a
    broken capability would look like an absent one."""
    await wired.install(bundle_at(tmp_path))
    chip = await wired.call("indicator")
    assert chip["tone"] in {"neutral", "success", "warning", "danger"}


@pytest.mark.asyncio
async def test_the_graph_schema_fragment_arrives(wired, tmp_path):
    """`{{ARTIFACT_GRAPH_SCHEMA}}` is filled by the extension, so
    the text the model reads comes from whoever owns the graph."""
    await wired.install(bundle_at(tmp_path))
    assert "ARTIFACT_GRAPH_SCHEMA" in wired.fragments.anchors()
    filled = wired.fragments.expand("schema:\n{{ARTIFACT_GRAPH_SCHEMA}}")
    assert "Artifact" in filled
    assert "{{ARTIFACT_GRAPH_SCHEMA}}" not in filled


@pytest.mark.asyncio
async def test_its_command_is_offered(wired, tmp_path):
    await wired.install(bundle_at(tmp_path))
    # Stored without the slash; the dispatcher adds it back.
    assert "artifacts" in {c.name for c in wired.commands.all()}


@pytest.mark.asyncio
async def test_uninstalling_takes_the_archivist_with_it(wired, tmp_path):
    await wired.install(bundle_at(tmp_path))
    assert await wired.installer.uninstall("igni.artifacts") is True
    assert not (wired.plugins_dir / "igni.artifacts").exists()


@pytest.mark.asyncio
async def test_the_whole_rpc_surface_is_served(wired, tmp_path):
    """Fifty-nine document RPCs plus the extension's own four.

    Registered whenever the grants are present, not when the graph
    happens to be listening: MCP fixes the tool list at connect, so
    a store that was down at startup would otherwise leave this
    serving four tools until somebody restarted igni.
    """
    await wired.install(bundle_at(tmp_path))
    served = {
        bare_tool_name("ext:igni.artifacts", name)
        for name in wired.mcp.get_tools("ext:igni.artifacts")
    }
    # 62, not 63: `status` is on the controller *and* on the
    # extension's own surface, and `RESERVED` is what makes this
    # module's win rather than whichever registered last.
    assert len(served) == 62, sorted(served)
    # A few by name, so a count that drifted for the wrong reason
    # is still caught.
    assert {"status", "indicator", "graph_schema", "igni_on_event"} <= served
    assert {"entries", "versions", "lineage", "comments", "tree"} <= served


@pytest.mark.asyncio
async def test_a_document_rpc_answers_through_igni(wired, tmp_path):
    """Not the chip and not the schema — an actual store query,
    dispatched through `ExtensionRpc` the way a frame would."""
    await wired.install(bundle_at(tmp_path))
    assert await wired.call("entries") == []
