"""Prompt fragments, prompt sections and slots, and agent stand-ins, from a manifest.

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

from ember_code.core.mcp.client import MCPClientManager
from ember_code.core.prompts.contributions import PromptContributions
from ember_code.core.prompts.document import PromptDocumentName
from ember_code.core.prompts.fragments import PromptFragments
from ember_code.extensions.contributions import (
    ExtensionConditionReceiver,
    ExtensionContributions,
)
from ember_code.extensions.impact import Resolution
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
        self.prompts = PromptContributions()
        self.resolutions: list[Resolution] = []
        self.contributions = ExtensionContributions(
            registry=self.registry,
            rpc=self.rpc,
            fragments=self.fragments,
            prompts=self.prompts,
            resolutions=lambda: self.resolutions,
        )
        self.contributions.attach()
        self.installer = ExtensionInstaller(
            registry=self.registry,
            rpc=self.rpc,
            mcp=self.mcp,
            origins=self.origins,
            allow_development=True,
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


def _other(**contributes: object) -> ExtensionManifest:
    return ExtensionManifest.model_validate(
        {
            "id": "other.ext",
            "version": "1.0.0",
            "contract": ">=1.0 <2",
            "trust": "unsigned",
            "dev": {"command": ["true"]},
            "contributes": contributes,
        }
    )


async def test_two_extensions_wanting_one_anchor_get_it_only_when_somebody_decides(wired, tmp_path):
    """Neither keeps it by having started first: an undecided collision
    is off for both, and a super admin's decision gives it to one."""
    await wired.install(bundle_at(tmp_path))
    assert wired.fragments.owner_of("ACME_POLICY_SUMMARY") == "acme.scanner"

    wired.registry.install(
        _other(
            promptFragments=[{"anchor": "ACME_POLICY_SUMMARY", "source": "file", "from": "x.md"}]
        ),
        state=ExtensionState.PREPARING,
    )
    assert wired.fragments.owner_of("ACME_POLICY_SUMMARY") is None
    [off] = wired.registry.get("acme.scanner").switched_off
    assert off.claim == "anchor:ACME_POLICY_SUMMARY"
    assert "no super admin has decided" in off.reason

    wired.resolutions.append(
        Resolution(
            claim="anchor:ACME_POLICY_SUMMARY",
            extensions=["acme.scanner", "other.ext"],
            winner="acme.scanner",
            decided_by="admin@acme.test",
        )
    )
    wired.contributions.resolutions_changed()
    await wired.contributions.apply(wired.registry.get("acme.scanner").manifest)
    assert wired.fragments.owner_of("ACME_POLICY_SUMMARY") == "acme.scanner"
    assert wired.registry.get("acme.scanner").switched_off == []
    [lost] = wired.registry.get("other.ext").switched_off
    assert "decided by admin@acme.test" in lost.reason


# ── The prompt ─────────────────────────────────────────────────────


def prompt_bundle(tmp_path: Path, prompt: dict, files: dict[str, str]) -> Path:
    bundle = bundle_at(tmp_path, contributes={"prompt": prompt})
    for name, text in files.items():
        (bundle / name).parent.mkdir(parents=True, exist_ok=True)
        (bundle / name).write_text(text)
    return bundle


async def test_a_READY_extension_fills_its_slot_and_replaces_its_section(wired, tmp_path):
    bundle = prompt_bundle(
        tmp_path,
        {
            "replace": [{"target": "main.safety", "source": "file", "from": "safety.md"}],
            "slots": [
                {"slot": "main.code_intelligence", "source": "tool", "from": "policy_summary"}
            ],
        },
        {"safety.md": "## Safety\n\nOur rules."},
    )
    await wired.install(bundle)

    main = PromptDocumentName.MAIN
    assert wired.prompts.section_replacements(main) == {"safety": "## Safety\n\nOur rules."}
    assert "no plaintext secrets" in wired.prompts.slot_fills(main)["code_intelligence"]


async def test_a_condition_holds_only_once_the_extension_says_so(wired, tmp_path):
    """No polling: the extension's process says, on its own connection."""
    bundle = prompt_bundle(
        tmp_path,
        {"replace": [{"target": "main", "source": "file", "from": "main.md", "when": "covered"}]},
        {"main.md": "The whole prompt, ours."},
    )
    await wired.install(bundle)
    assert wired.prompts.replacement(PromptDocumentName.MAIN) is None

    receiver = ExtensionConditionReceiver(registry=wired.registry, prompts=wired.prompts)
    receiver("ext:acme.scanner", {"conditions": {"covered": True}})
    assert wired.prompts.replacement(PromptDocumentName.MAIN) == "The whole prompt, ours."

    receiver("ext:acme.scanner", {"conditions": {"covered": False}})
    assert wired.prompts.replacement(PromptDocumentName.MAIN) is None


async def test_a_condition_is_only_ever_its_own_extensions(wired, tmp_path):
    """The connection names the sender: nothing else can say it."""
    bundle = prompt_bundle(
        tmp_path,
        {"replace": [{"target": "main", "source": "file", "from": "main.md", "when": "covered"}]},
        {"main.md": "ours"},
    )
    await wired.install(bundle)
    receiver = ExtensionConditionReceiver(registry=wired.registry, prompts=wired.prompts)

    receiver("ext:other.ext", {"conditions": {"covered": True}})
    receiver("some-mcp-server", {"conditions": {"covered": True}})
    assert wired.prompts.replacement(PromptDocumentName.MAIN) is None


async def test_a_slot_fill_longer_than_a_slot_is_refused(wired, tmp_path):
    bundle = prompt_bundle(
        tmp_path,
        {"slots": [{"slot": "main.code_intelligence", "source": "file", "from": "long.md"}]},
        {"long.md": "x" * 8_001},
    )
    await wired.install(bundle)
    assert wired.prompts.slot_fills(PromptDocumentName.MAIN) == {}


async def test_an_agent_stand_in_is_read_from_the_installed_plugin(tmp_path):
    """So it counts among the extension's own agents for its tools."""
    registry = ExtensionRegistry()
    prompts = PromptContributions()
    plugins = tmp_path / "plugins"
    installed = plugins / "acme.scanner" / "stand-ins" / "explorer.md"
    installed.parent.mkdir(parents=True)
    installed.write_text("---\nname: explorer\ndescription: x\n---\nbody")
    contributions = ExtensionContributions(
        registry=registry, rpc=None, prompts=prompts, plugins_dir=plugins
    )
    manifest = ExtensionManifest.model_validate(
        {
            "id": "acme.scanner",
            "version": "1.0.0",
            "contract": ">=1.0 <2",
            "trust": "unsigned",
            "dev": {"command": ["true"]},
            "contributes": {
                "plugin": "plugin",
                "prompt": {
                    "agents": [{"agent": "explorer", "from": "plugin/stand-ins/explorer.md"}]
                },
            },
        }
    )
    await contributions.apply(manifest)
    assert prompts.agent_replacements()["explorer"].path == installed


async def test_a_down_extension_takes_its_prompt_with_it(wired, tmp_path):
    bundle = prompt_bundle(
        tmp_path,
        {"slots": [{"slot": "main.code_intelligence", "source": "file", "from": "ci.md"}]},
        {"ci.md": "the index"},
    )
    await wired.install(bundle)
    assert wired.prompts.slot_fills(PromptDocumentName.MAIN)

    wired.registry.set_state("acme.scanner", ExtensionState.FAILED, "down")
    assert wired.prompts.slot_fills(PromptDocumentName.MAIN) == {}


async def test_uninstalling_forgets_everything_it_contributed(wired, tmp_path):
    bundle = prompt_bundle(
        tmp_path,
        {"slots": [{"slot": "main.code_intelligence", "source": "file", "from": "ci.md"}]},
        {"ci.md": "the index"},
    )
    await wired.install(bundle)
    wired.registry.remove("acme.scanner")

    assert wired.prompts.slot_fills(PromptDocumentName.MAIN) == {}
    assert "ACME_POLICY_SUMMARY" not in wired.fragments.anchors()


async def test_the_registry_does_not_announce_a_state_that_did_not_move(wired, tmp_path):
    """Every healthy probe would otherwise re-run a tool call."""
    await wired.install(bundle_at(tmp_path))
    seen: list[str] = []
    wired.registry.watch(lambda _entry, ext_id: seen.append(ext_id))

    wired.registry.set_state("acme.scanner", ExtensionState.READY)
    assert seen == []

    wired.registry.set_state("acme.scanner", ExtensionState.FAILED, "now it moved")
    assert seen == ["acme.scanner"]
