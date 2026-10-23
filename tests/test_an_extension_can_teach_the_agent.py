"""Prompt fragments and agent variants, from a manifest.

EXTENSIBILITY §1 calls these the whole justification for a contract —
MCP carries tools and carries none of this. Both mechanisms existed
and neither was reachable from a manifest: ``PromptFragments`` was
registered into only by in-tree CodeIndex, and
``AgentDefinitionLoader``'s variant arguments were never passed by
anybody, so the default stayed a literal ``{"codeindex"}``.

Until this, an extension was a web panel with a backend. It could not
tell the agent what it knows, which is the difference between
installing a capability and installing a page.

The fragment here is fetched from a real MCP tool over a real
subprocess, because the interesting part is the round trip: a
supplier is synchronous and the data is not.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from ember_code.core.agents.variants import VariantRegistry
from ember_code.core.mcp.client import MCPClientManager
from ember_code.core.prompts.fragments import PromptFragments
from ember_code.extensions.contributions import ExtensionContributions
from ember_code.extensions.installer import ExtensionInstaller
from ember_code.extensions.manifest import ExtensionManifest
from ember_code.extensions.origin import ExtensionOrigins
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState
from ember_code.extensions.rpc import ExtensionRpc

pytestmark = pytest.mark.asyncio

FIXTURE = Path(__file__).parent / "fixtures" / "dev_extension"


def bundle_at(tmp_path: Path, **changes: object) -> Path:
    bundle = tmp_path / "ext"
    shutil.copytree(FIXTURE, bundle)
    raw = json.loads((bundle / "manifest.json").read_text())
    raw["dev"]["command"] = [sys.executable, "server.py"]
    raw.update(changes)
    (bundle / "manifest.json").write_text(json.dumps(raw))
    return bundle


class Wired:
    def __init__(self, tmp_path: Path) -> None:
        self.registry = ExtensionRegistry()
        self.rpc = ExtensionRpc(self.registry)
        self.mcp = MCPClientManager(project_dir=tmp_path)
        self.origins = ExtensionOrigins()
        # Local tables rather than the process-wide ones, so these
        # tests cannot leak into each other or into anything else.
        self.fragments = PromptFragments()
        self.variants = VariantRegistry()
        self.contributions = ExtensionContributions(
            registry=self.registry,
            rpc=self.rpc,
            fragments=self.fragments,
            variants=self.variants,
        )
        self.contributions.attach()
        self.installer = ExtensionInstaller(
            registry=self.registry,
            rpc=self.rpc,
            mcp=self.mcp,
            origins=self.origins,
            allow_unsigned=True,
        )

    async def install(self, bundle: Path):
        self.contributions.note_bundle("acme.scanner", bundle)
        entry = await self.installer.install(bundle)
        # The registry announces synchronously and the fold-in is a
        # task, so the work is scheduled rather than done.
        await self.contributions.apply(entry.manifest)
        return entry

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


# ── Fragments ──────────────────────────────────────────────────────


async def test_a_tool_sourced_anchor_reaches_a_prompt(wired, tmp_path):
    """The round trip this exists for."""
    entry = await wired.install(bundle_at(tmp_path))
    assert entry.state is ExtensionState.READY, entry.reason

    filled = wired.fragments.expand("Policy:\n{{ACME_POLICY_SUMMARY}}\nEnd.")
    assert "no plaintext secrets" in filled
    assert "{{" not in filled


async def test_a_file_sourced_anchor_reads_the_bundle(wired, tmp_path):
    bundle = bundle_at(
        tmp_path,
        contributes={
            "promptFragments": [{"anchor": "ACME_NOTES", "source": "file", "from": "notes.md"}]
        },
    )
    (bundle / "notes.md").write_text("read from the bundle")
    await wired.install(bundle)
    assert wired.fragments.expand("{{ACME_NOTES}}") == "read from the bundle"


async def test_a_file_outside_the_bundle_is_refused(wired, tmp_path):
    """`from` is a string out of a manifest, and `../../etc/passwd` is
    a thing a manifest can say."""
    secret = tmp_path / "secret.txt"
    secret.write_text("not yours")
    bundle = bundle_at(
        tmp_path,
        contributes={
            "promptFragments": [{"anchor": "ACME_NOTES", "source": "file", "from": "../secret.txt"}]
        },
    )
    await wired.install(bundle)
    # Unfilled, which leaves the anchor for whoever owns the next
    # pass rather than putting the file in front of a model.
    assert wired.fragments.expand("{{ACME_NOTES}}") == "{{ACME_NOTES}}"


async def test_an_anchor_whose_tool_fails_does_not_stop_the_install(wired, tmp_path):
    """One capability failing to describe itself must not take out
    the extension or the prompt."""
    bundle = bundle_at(
        tmp_path,
        contributes={
            "promptFragments": [{"anchor": "ACME_BOOM", "source": "tool", "from": "explode"}]
        },
    )
    entry = await wired.install(bundle)
    assert entry.state is ExtensionState.READY
    assert wired.fragments.expand("{{ACME_BOOM}}") == "{{ACME_BOOM}}"


async def test_an_anchor_goes_when_the_extension_stops_being_READY(wired, tmp_path):
    """The reason this follows state rather than installation.

    An extension whose service has died must stop telling the model
    about data it can no longer fetch — otherwise the model is told
    about a schema, reaches for the tool that reads it, and gets a
    refusal it cannot act on.
    """
    await wired.install(bundle_at(tmp_path))
    assert "ACME_POLICY_SUMMARY" in wired.fragments.anchors()

    wired.registry.set_state("acme.scanner", ExtensionState.FAILED, "it died")
    assert "ACME_POLICY_SUMMARY" not in wired.fragments.anchors()


async def test_an_anchor_comes_back_when_the_extension_does(wired, tmp_path):
    """Recovery, which is what the health monitor's restart produces.

    The state has to go back to READY *before* the text is fetched —
    the fetch is an `ext.<id>.*` call, and `authorise` refuses one to
    an extension that is not READY. That ordering is not incidental:
    it is the same rule stopping a half-recovered extension being
    asked to describe itself.
    """
    await wired.install(bundle_at(tmp_path))
    wired.registry.set_state("acme.scanner", ExtensionState.FAILED, "it died")
    assert "ACME_POLICY_SUMMARY" not in wired.fragments.anchors()

    wired.registry.set_state("acme.scanner", ExtensionState.READY)
    entry = wired.registry.get("acme.scanner")
    assert entry is not None
    await wired.contributions.apply(entry.manifest)
    assert "ACME_POLICY_SUMMARY" in wired.fragments.anchors()


async def test_a_fragment_is_not_fetched_from_an_extension_that_is_down(wired, tmp_path):
    """The ordering above, stated as its own rule."""
    await wired.install(bundle_at(tmp_path))
    wired.fragments.remove_owner("acme.scanner")
    wired.registry.set_state("acme.scanner", ExtensionState.FAILED, "down")

    entry = wired.registry.get("acme.scanner")
    assert entry is not None
    await wired.contributions.apply(entry.manifest)
    assert "ACME_POLICY_SUMMARY" not in wired.fragments.anchors()


async def test_two_extensions_cannot_take_one_anchor(wired, tmp_path):
    """Refused rather than last-write-wins: otherwise whichever prompt
    loaded last would decide what the model was told."""
    await wired.install(bundle_at(tmp_path))

    other = ExtensionManifest.model_validate(
        {
            "id": "other.ext",
            "version": "1.0.0",
            "contract": ">=1.0 <2",
            "trust": "unsigned",
            "dev": {"command": ["true"]},
            "contributes": {
                "promptFragments": [
                    {
                        "anchor": "ACME_POLICY_SUMMARY",
                        "source": "file",
                        "from": "x.md",
                    }
                ]
            },
        }
    )
    await wired.contributions.apply(other)
    assert wired.fragments.owner_of("ACME_POLICY_SUMMARY") == "acme.scanner"


# ── Variants ───────────────────────────────────────────────────────


async def test_a_READY_extension_makes_its_variants_live(wired, tmp_path):
    await wired.install(bundle_at(tmp_path))
    assert "acme-scan" in wired.variants.known()
    assert "acme-scan" in wired.variants.active()


async def test_a_down_extension_stays_known_and_stops_being_active(wired, tmp_path):
    """Known and live are different questions, and conflating them is
    the bug the loader's own docstring warns about: a sibling naming a
    variant that is installed but down has to be recognised and
    skipped, not loaded as an agent whose name contains a dot.
    """
    await wired.install(bundle_at(tmp_path))
    wired.registry.set_state("acme.scanner", ExtensionState.FAILED, "down")

    assert "acme-scan" in wired.variants.known()
    assert "acme-scan" not in wired.variants.active()


async def test_uninstalling_forgets_the_variant_entirely(wired, tmp_path):
    await wired.install(bundle_at(tmp_path))
    wired.registry.remove("acme.scanner")

    assert "acme-scan" not in wired.variants.known()
    assert "acme-scan" not in wired.variants.active()
    assert "ACME_POLICY_SUMMARY" not in wired.fragments.anchors()


async def test_re_declaring_drops_a_variant_that_went_away(wired, tmp_path):
    """An upgrade that removed a variant should stop being known for
    it."""
    await wired.install(bundle_at(tmp_path))
    assert "acme-scan" in wired.variants.known()

    wired.variants.declare("acme.scanner", [])
    assert "acme-scan" not in wired.variants.known()


async def test_the_registry_does_not_announce_a_state_that_did_not_move(wired, tmp_path):
    """Every healthy probe would otherwise re-run a tool call."""
    await wired.install(bundle_at(tmp_path))
    seen: list[str] = []
    wired.registry.watch(lambda _entry, ext_id: seen.append(ext_id))

    wired.registry.set_state("acme.scanner", ExtensionState.READY)
    assert seen == []

    wired.registry.set_state("acme.scanner", ExtensionState.FAILED, "now it moved")
    assert seen == ["acme.scanner"]
