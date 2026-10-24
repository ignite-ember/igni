"""The second shape, installed and called.

``examples/reference-extension`` is single-view, read-mostly,
one-pane and prose-free — each of those avoids a known gap, so it
cannot be the standing check its README claims to be.
``examples/release-notes`` is the opposite of all four: a two-pane
browser over editable documents.

Building it against the published floor found a real bug — ``Tabs``
generated its own id prefix and ``TabPanel`` required one, with no
way for a caller to learn what had been generated, so the two were
published as a pair that could not be used as a pair. That is pinned
in ``packages/ui-react/src/tier0.test.tsx``.

This file drives the half that runs: the manifest installs, the
backend answers, and the revision check refuses a write over somebody
else's — which is the part of this example worth copying.
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

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "release-notes"


def bundle_at(tmp_path: Path) -> Path:
    bundle = tmp_path / "notes"
    shutil.copytree(EXAMPLE, bundle)
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
            allow_development=True,
        )

    async def call(self, method: str, **args: object):
        return await self.rpc.dispatch(
            caller="acme.notes", method=f"ext.acme.notes.{method}", args=args
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


async def test_the_example_installs_and_serves_its_ui(wired, tmp_path):
    bundle = bundle_at(tmp_path)
    wired.contributions.note_bundle("acme.notes", bundle)

    entry = await wired.installer.install(bundle)
    assert entry.state is ExtensionState.READY, entry.reason
    assert wired.origins.url_for("acme.notes")


async def test_its_backend_answers_the_calls_its_ui_makes(wired, tmp_path):
    """The five methods `src/notes.ts` names, driven for real."""
    await wired.installer.install(bundle_at(tmp_path))

    releases = await wired.call("releases")
    assert [r["version"] for r in releases] == ["2.4.0", "2.3.1"]

    note = await wired.call("note", version="2.4.0")
    assert note["revision"]
    assert note["body"].startswith("# 2.4.0")

    comments = await wired.call("comments", version="2.4.0")
    assert len(comments) == 1


async def test_a_stale_revision_is_refused_rather_than_overwriting(wired, tmp_path):
    """The part of this example worth copying.

    A document is the one shape where two people editing at once is
    normal rather than exceptional, and "last write wins" there loses
    somebody's afternoon with no error.
    """
    await wired.installer.install(bundle_at(tmp_path))

    note = await wired.call("note", version="2.4.0")
    first = await wired.call("save", version="2.4.0", body="mine", revision=note["revision"])
    assert first["body"] == "mine"
    assert first["revision"] != note["revision"]

    # Somebody else still holding the old revision.
    refused = await wired.call("save", version="2.4.0", body="theirs", revision=note["revision"])
    assert refused == {"conflict": first["revision"]}

    # And the refusal did not write.
    assert (await wired.call("note", version="2.4.0"))["body"] == "mine"


async def test_its_file_sourced_prompt_fragment_reaches_a_prompt(wired, tmp_path):
    """A manifest teaching the agent this repository's house style."""
    bundle = bundle_at(tmp_path)
    wired.contributions.note_bundle("acme.notes", bundle)
    entry = await wired.installer.install(bundle)
    await wired.contributions.apply(entry.manifest)

    filled = wired.fragments.expand("Style:\n{{ACME_NOTES_STYLE}}")
    # Matched on a phrase that does not straddle a line break: the
    # fragment is a file and arrives with its wrapping intact.
    assert "Name the behaviour, not the commit." in filled
    assert "{{" not in filled


async def test_it_declares_a_variant_and_makes_it_live(wired, tmp_path):
    bundle = bundle_at(tmp_path)
    wired.contributions.note_bundle("acme.notes", bundle)
    entry = await wired.installer.install(bundle)
    await wired.contributions.apply(entry.manifest)

    assert "acme-notes" in wired.variants.active()


async def test_every_method_its_client_names_is_granted(wired, tmp_path):
    """The manifest and `src/notes.ts` have to agree.

    Narrowing the grant without noticing would fail in somebody's
    browser instead of here.
    """
    manifest = ExtensionManifest.load(EXAMPLE / "manifest.json")
    for method in ("releases", "note", "comments", "save", "publish"):
        assert manifest.grants_rpc(f"ext.acme.notes.{method}"), method
