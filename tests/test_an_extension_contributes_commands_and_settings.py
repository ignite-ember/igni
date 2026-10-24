"""``contributes.commands`` and ``contributes.settings``.

The last two contribution types the manifest declared and nothing
read. An extension could name ``/compliance`` and typing it got the
skill fallback, because the catalog had three sources and the
dispatcher three tiers and neither knew about a fourth. Settings were
modelled, validated, and stored nowhere.

Both are driven here against a real subprocess, because the part
worth checking is the round trip: a command is dispatched to a tool
and its text comes back, and a setting written by an administrator
reaches the frame as a launch parameter.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from ember_code.backend.panels.slash_commands_catalog import SlashCommandsCatalog
from ember_code.core.contributed_commands import ContributedCommands
from ember_code.core.mcp.client import MCPClientManager
from ember_code.extensions.contributions import ExtensionContributions
from ember_code.extensions.installer import ExtensionInstaller
from ember_code.extensions.manifest import ExtensionManifest
from ember_code.extensions.origin import ExtensionOrigins
from ember_code.extensions.registry import (
    ExtensionRefused,
    ExtensionRegistry,
    ExtensionState,
)
from ember_code.extensions.rpc import ExtensionRpc
from ember_code.extensions.store import ExtensionStore, StoreError

# No module-level ``asyncio`` mark: this file mixes async and sync
# tests, and the mark would be applied to the sync ones too — which
# pytest-asyncio warns about, rightly. ``asyncio_mode`` is ``auto``,
# so the async ones need nothing.

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
        self.commands = ContributedCommands()
        self.contributions = ExtensionContributions(
            registry=self.registry, rpc=self.rpc, commands=self.commands
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
        entry = await self.installer.install(bundle)
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


# ── C3 · commands ──────────────────────────────────────────────────


async def test_a_declared_command_runs_and_returns_its_text(wired, tmp_path):
    """The round trip. `/compliance` runs the tool `compliance`."""
    await wired.install(bundle_at(tmp_path))

    found = wired.commands.get("compliance")
    assert found is not None
    assert found.owner == "acme.scanner"
    assert "violations" in found.description

    assert "1 open violation" in await found.invoke("")


async def test_the_rest_of_the_line_reaches_the_tool(wired, tmp_path):
    await wired.install(bundle_at(tmp_path))
    found = wired.commands.get("compliance")
    assert found is not None
    assert "src/config.py" in await found.invoke("src/config.py")


async def test_a_command_goes_when_its_extension_stops_being_READY(wired, tmp_path):
    """Worse than never offering it: the user picks it and it fails."""
    await wired.install(bundle_at(tmp_path))
    assert wired.commands.get("compliance") is not None

    wired.registry.set_state("acme.scanner", ExtensionState.FAILED, "it died")
    assert wired.commands.get("compliance") is None


async def test_an_undeclared_command_is_refused_even_if_the_tool_exists(wired, tmp_path):
    """`contributes.commands` is the grant.

    The service exposes `explode`, and nothing in the manifest offers
    it as a command — so igni will not run it as one.
    """
    await wired.install(bundle_at(tmp_path))
    with pytest.raises(ExtensionRefused, match="does not declare"):
        await wired.rpc.invoke_command("acme.scanner", "explode", "")


async def test_a_command_on_a_down_extension_says_why(wired, tmp_path):
    await wired.install(bundle_at(tmp_path))
    wired.registry.set_state("acme.scanner", ExtensionState.FAILED, "licence expired")
    with pytest.raises(ExtensionRefused, match="licence expired"):
        await wired.rpc.invoke_command("acme.scanner", "compliance", "")


async def test_the_catalog_offers_it(wired, tmp_path):
    """So it appears in completion, which is where a user finds it."""
    await wired.install(bundle_at(tmp_path))

    catalog = SlashCommandsCatalog.__new__(SlashCommandsCatalog)
    catalog._commands = wired.commands  # type: ignore[attr-defined]
    entries = catalog._extension_entries()  # type: ignore[attr-defined]

    assert [e.name for e in entries] == ["compliance"]
    assert entries[0].source == "extension"
    # Attributable: "/compliance did not answer" is a support ticket,
    # "acme.scanner did not answer it" is something to act on.
    assert entries[0].owner == "acme.scanner"


def test_two_extensions_cannot_answer_one_command():
    """Refused rather than last-write-wins.

    Two extensions both answering `/scan` with the winner decided by
    install order is a product that behaves differently on two
    machines running the same software.
    """
    commands = ContributedCommands()

    async def run(_args: str) -> str:
        return "ok"

    assert commands.register("scan", owner="a.one", description="", invoke=run) is None
    refusal = commands.register("scan", owner="b.two", description="", invoke=run)
    assert refusal is not None and "a.one" in refusal
    found = commands.get("scan")
    assert found is not None and found.owner == "a.one"


def test_a_command_is_matched_without_its_slash():
    commands = ContributedCommands()

    async def run(_args: str) -> str:
        return "ok"

    commands.register("/scan", owner="a.one", description="", invoke=run)
    assert commands.get("scan") is not None
    assert commands.get("/scan") is not None
    assert commands.get("/SCAN") is not None


# ── C5 · settings ──────────────────────────────────────────────────


def manifest() -> ExtensionManifest:
    return ExtensionManifest.load(FIXTURE / "manifest.json")


def test_settings_start_at_their_declared_defaults(tmp_path):
    store = ExtensionStore(tmp_path)
    assert store.settings(manifest()) == {"failOn": "high", "notify": True}


def test_a_stored_value_wins_over_the_default(tmp_path):
    store = ExtensionStore(tmp_path)
    store.save_settings(manifest(), {"failOn": "critical"})
    assert store.settings(manifest()) == {"failOn": "critical", "notify": True}


def test_an_undeclared_key_is_refused(tmp_path):
    """A settings write is an administrator acting on what the host
    showed them, so a key that was not on that form did not come from
    there."""
    store = ExtensionStore(tmp_path)
    with pytest.raises(StoreError, match="does not declare"):
        store.save_settings(manifest(), {"backdoor": True})


def test_an_enum_outside_its_options_is_refused(tmp_path):
    store = ExtensionStore(tmp_path)
    with pytest.raises(StoreError, match="must be one of"):
        store.save_settings(manifest(), {"failOn": "catastrophic"})


def test_a_key_the_manifest_no_longer_declares_is_dropped(tmp_path):
    """An extension that removed a setting should stop being handed
    it — otherwise a stale option survives three upgrades."""
    store = ExtensionStore(tmp_path)
    store.save_settings(manifest(), {"failOn": "low"})
    store.settings_path("acme.scanner").write_text(json.dumps({"failOn": "low", "gone": "stale"}))
    assert "gone" not in store.settings(manifest())


def test_a_corrupt_settings_file_falls_back_to_defaults(tmp_path):
    """Defaults are a working configuration; a crash is not."""
    store = ExtensionStore(tmp_path)
    store._root.mkdir(parents=True, exist_ok=True)  # type: ignore[attr-defined]
    store.settings_path("acme.scanner").write_text("{ not json")
    assert store.settings(manifest()) == {"failOn": "high", "notify": True}


def test_removing_an_extension_takes_its_settings(tmp_path):
    """Reinstalling must not silently inherit what the last
    administrator configured."""
    store = ExtensionStore(tmp_path)
    store.add(FIXTURE)
    store.save_settings(manifest(), {"failOn": "low"})
    assert store.settings_path("acme.scanner").is_file()

    store.remove("acme.scanner")
    assert not store.settings_path("acme.scanner").is_file()


def test_an_extension_with_no_settings_has_none(tmp_path):
    bare = ExtensionManifest.model_validate(
        {
            "id": "acme.bare",
            "version": "1.0.0",
            "contract": ">=1.0 <2",
            "trust": "unsigned",
            "dev": {"command": ["true"]},
        }
    )
    assert ExtensionStore(tmp_path).settings(bare) == {}
