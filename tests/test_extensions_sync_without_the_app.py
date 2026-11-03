"""Extensions arrive without the igni app: ``igni ext sync`` and ``igni -m``.

The distribution sync ran only in the app's backend, after a window
attached. A developer working from a terminal — and every ``igni -m``
run — never received an extension their organisation had switched on.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from ember_code.backend.headless_extensions import HeadlessExtensions
from ember_code.core.auth.credentials import Credentials, CredentialsStore
from ember_code.extensions.distribution import OneShotSync
from ember_code.extensions.store import ExtensionStore
from tests.extension_distribution_helpers import (
    API_URL,
    SESSION,
    FakeIgni,
    Listed,
    Publisher,
    bundle_archive,
    manifest,
)

# ``ember_code.cli`` re-exports the ``ext`` group under the module's name.
ext_cli = importlib.import_module("ember_code.cli.ext")


class World:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        self.igni = FakeIgni()
        self.publisher = Publisher("RSA")
        self.credentials = tmp_path / "credentials.json"
        self.store = ExtensionStore(tmp_path / "store")
        self.settings = SimpleNamespace(
            api_url=API_URL,
            portal_url="",
            air_gapped=False,
            auth=SimpleNamespace(credentials_file=str(self.credentials)),
            storage=SimpleNamespace(data_dir=str(tmp_path / "data")),
        )
        monkeypatch.setattr(OneShotSync, "transport", self.igni.transport())
        monkeypatch.setattr(ext_cli, "_settings", lambda: self.settings)

    def sign_in(self, *, ttl: int = 3600) -> None:
        CredentialsStore(str(self.credentials)).save(
            Credentials.new(SESSION, "ada@acme.test", ttl=ttl)
        )

    def publish(self) -> None:
        raw = manifest()
        self.igni.publish(
            Listed(bundle_archive(raw, {"wheels/placeholder.txt": b"x"}), self.publisher)
        )

    def sync(self, *args: str):
        argv = ["sync", "--root", str(self.store.root), *args]
        return CliRunner().invoke(ext_cli.ext, argv)


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    return World(tmp_path, monkeypatch)


# ── igni ext sync ──────────────────────────────────────────────────


def test_sync_installs_what_igni_lists_for_the_persons_group(world):
    world.sign_in()
    world.publish()
    result = world.sync()
    assert result.exit_code == 0, result.output
    assert "installed" in result.output and "acme.notes 1.0.0" in result.output
    assert "https://notes.example.com" in result.output
    assert world.store.distribution_of("acme.notes") is not None
    # What to restart, said exactly.
    assert "quit the igni app" in result.output


def test_sync_again_says_nothing_changed(world):
    world.sign_in()
    world.publish()
    world.sync()
    result = world.sync()
    assert result.exit_code == 0, result.output
    assert "unchanged" in result.output and "acme.notes" in result.output


def test_a_listing_with_nothing_for_the_group_says_who_switches_it_on(world):
    world.sign_in()
    result = world.sync()
    assert result.exit_code == 0, result.output
    assert "nothing" in result.output
    assert "organisation admin" in result.output
    assert "super admin" in result.output


def test_a_development_install_is_kept_and_said(world, tmp_path):
    world.sign_in()
    world.publish()
    bundle = tmp_path / "mine"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(
        '{"id": "acme.notes", "version": "0.0.1", "contract": ">=1.0 <2",'
        ' "trust": "unsigned", "dev": {"command": ["true"]}}'
    )
    world.store.add(bundle)
    result = world.sync()
    assert "kept" in result.output and "development install" in result.output
    assert "igni ext remove acme.notes" in result.output


def test_sync_when_nobody_is_signed_in_says_how_to_sign_in(world):
    result = world.sync()
    assert result.exit_code != 0
    assert "igni login" in result.output


def test_sync_with_an_expired_sign_in_says_it_expired(world):
    world.sign_in(ttl=-10)
    result = world.sync()
    assert result.exit_code != 0
    assert "expired" in result.output


def test_sync_with_no_server_names_the_setting(world):
    world.settings.api_url = ""
    world.sign_in()
    result = world.sync()
    assert result.exit_code != 0
    assert "api_url" in result.output


def test_a_refused_listing_is_reported_not_a_traceback(world):
    world.sign_in()
    world.igni.listing_status = 503
    result = world.sync()
    assert result.exit_code != 0
    assert "503" in result.output
    assert "Traceback" not in result.output


# ── igni -m / -p ───────────────────────────────────────────────────


class Mcp:
    configs: dict = {}

    async def connect(self, name):
        return SimpleNamespace(ok=False, reason="not started in this test")

    async def disconnect_one(self, name):
        return False

    async def disconnect_all(self):
        return None

    def get_client(self, name):
        return None


async def test_a_headless_run_syncs_before_it_starts_extensions(world, monkeypatch):
    world.sign_in()
    world.publish()
    monkeypatch.setattr(ExtensionStore, "__init__", _rooted(world.store.root))
    project = world.tmp / "project"
    project.mkdir()
    session = SimpleNamespace(
        settings=world.settings,
        project_dir=project,
        session_id="s-1",
        mcp_manager=Mcp(),
        reload_group_agents=lambda: True,
    )
    extensions = HeadlessExtensions(session)
    await extensions.start()
    try:
        assert world.store.distribution_of("acme.notes") is not None
        assert extensions.registry.get("acme.notes") is not None
    finally:
        await extensions.stop()


async def test_a_headless_run_that_cannot_sync_still_runs(world, monkeypatch):
    world.sign_in()
    world.igni.listing_status = 500
    monkeypatch.setattr(ExtensionStore, "__init__", _rooted(world.store.root))
    session = SimpleNamespace(
        settings=world.settings,
        project_dir=world.tmp,
        session_id="s-1",
        mcp_manager=Mcp(),
        reload_group_agents=lambda: True,
    )
    extensions = HeadlessExtensions(session)
    await extensions.start()  # does not raise
    await extensions.stop()


def _rooted(root: Path):
    original = ExtensionStore.__init__

    def init(self, root_override=None):
        original(self, root_override or root)

    return init
