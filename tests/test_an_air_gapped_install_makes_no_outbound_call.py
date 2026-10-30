"""``air_gapped: true`` switches off every outbound flow the client has.

ember-server's ``docs/NETWORK_DIAGRAM.md`` §3 lists the flows an
unconfigured install makes out of the customer's network: the PyPI
update check, the desktop updater's call to github.com, the session-start
clone of Anthropic's plugin marketplace, and the agent's web tools. One
setting turns all of them off. This file proves each is off by watching
the seam the request would go through — the HTTP client, the ``git``
subprocess, the toolkit constructor — and asserting nothing reached it.
Asserting on return values alone would pass even if the call had been
attempted and failed, which on an air-gapped network it would.

The desktop updater's and runtime's halves live in Rust and are tested
there (``clients/tauri/src-tauri/src/update_policy.rs``, ``runtime.rs``).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from ember_code.backend.cmd_plugin.gateway import PluginBackendGateway
from ember_code.core import air_gap
from ember_code.core.config.group_policy import GroupPolicyEntry, GroupPolicyPack
from ember_code.core.config.schemas import PluginsConfig, StorageConfig
from ember_code.core.config.settings import Settings, SettingsLoader, load_settings
from ember_code.core.config.tool_permissions import ToolPermissions
from ember_code.core.decisions.schemas import DecisionsUnavailable
from ember_code.core.plugins import marketplaces as _marketplaces
from ember_code.core.plugins.installer import PluginError, PluginInstaller
from ember_code.core.plugins.models import MarketplaceEntry, MarketplaceRegistry
from ember_code.core.plugins.source_policy import PluginSourcePolicy
from ember_code.core.plugins.state import load_state, save_state
from ember_code.core.session.core import Session
from ember_code.core.session.startup.marketplace import MarketplaceWarmupPhase
from ember_code.core.tools.registry import ToolRegistry
from ember_code.core.tools.tool_spec import (
    ToolResolutionRequest,
    WebFetchSpec,
    WebSearchSpec,
)
from ember_code.core.utils.update_checker import UpdateChecker
from ember_code.decisions_server import model as decisions_model

OFFICIAL = "https://github.com/anthropics/claude-plugins-official"


class GitRecorder:
    """Stands in for ``subprocess.run`` inside the git client.

    Records every git command line. ``passthrough`` runs it for real —
    for the tests where a *local* clone is the point.
    """

    def __init__(self, *, passthrough: bool = False) -> None:
        self.calls: list[list[str]] = []
        self._passthrough = passthrough
        self._real = subprocess.run

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if self._passthrough:
            return self._real(args, **kwargs)
        raise AssertionError(f"git was started: {args}")

    def clone_urls(self) -> list[str]:
        return [c[-2] for c in self.calls if c[:2] == ["git", "clone"]]


def _session_with(settings: Settings) -> Session:
    sess = Session.__new__(Session)
    sess.settings = settings
    return sess


def _air_gapped(tmp_path: Path, **plugins) -> Settings:
    return Settings(
        air_gapped=True,
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        plugins=PluginsConfig(**plugins),
    )


def _register(data_dir: Path, *entries: MarketplaceEntry) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "marketplaces.json").write_text(
        MarketplaceRegistry(marketplaces=list(entries)).model_dump_json()
    )


def _local_marketplace(root: Path, name: str = "acme-internal") -> Path:
    """A marketplace repository on disk, the way a customer mirrors one."""
    repo = root / "marketplace"
    (repo / ".claude-plugin").mkdir(parents=True)
    (repo / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": name, "plugins": []})
    )
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "catalog"], check=True)
    return repo


# ── The switch ──────────────────────────────────────────────────────


class TestTheSwitch:
    def test_it_is_off_by_default(self):
        assert Settings().air_gapped is False

    def test_the_config_key_turns_it_on(self, tmp_path):
        (Path.home() / ".igni").mkdir(parents=True, exist_ok=True)
        (Path.home() / ".igni" / "config.yaml").write_text("air_gapped: true\n")
        assert load_settings(project_dir=tmp_path).air_gapped is True

    @pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
    def test_the_environment_variable_turns_it_on(self, monkeypatch, value):
        monkeypatch.setenv(Settings.AIR_GAPPED_ENV, value)
        assert Settings().air_gapped is True

    def test_the_environment_variable_cannot_turn_it_off(self, monkeypatch):
        monkeypatch.setenv(Settings.AIR_GAPPED_ENV, "0")
        assert Settings(air_gapped=True).air_gapped is True
        assert Settings().air_gapped is False

    def test_a_managed_settings_file_turns_it_on(self, tmp_path, monkeypatch):
        managed = tmp_path / "managed-settings.yaml"
        managed.write_text("air_gapped: true\n")
        monkeypatch.setattr(SettingsLoader, "platform_managed_settings_path", lambda: managed)
        # A user cannot switch a sysadmin's air gap back off.
        (Path.home() / ".igni").mkdir(parents=True, exist_ok=True)
        (Path.home() / ".igni" / "config.yaml").write_text("air_gapped: false\n")
        assert load_settings(project_dir=tmp_path).air_gapped is True

    def test_a_group_policy_turns_it_on(self, tmp_path):
        pack = GroupPolicyPack(
            group_id="g",
            group_name="Regulated",
            fetched_at=datetime.now(UTC),
            entries=[
                GroupPolicyEntry(
                    kind="settings",
                    entry_name="air-gap",
                    content=json.dumps({"air_gapped": True}),
                    content_type="json",
                )
            ],
        )
        settings = load_settings(project_dir=tmp_path, group_policy_fetcher=lambda: pack)
        assert settings.air_gapped is True


# ── D9: the update check to pypi.org ────────────────────────────────


class TestTheUpdateCheck:
    def test_no_http_client_is_created(self, tmp_path):
        with patch("ember_code.core.utils.update_checker.httpx.AsyncClient") as client:
            result = asyncio.run(
                UpdateChecker(settings=_air_gapped(tmp_path), cache_path=tmp_path / "c").check()
            )
        client.assert_not_called()
        assert result.available is False

    def test_a_ttl_cannot_turn_it_back_on(self, tmp_path):
        settings = _air_gapped(tmp_path)
        settings.update_check_ttl = 86400
        with patch("ember_code.core.utils.update_checker.httpx.AsyncClient") as client:
            asyncio.run(UpdateChecker(settings=settings, cache_path=tmp_path / "c").check())
        client.assert_not_called()


# ── D11: the plugin marketplace clone ───────────────────────────────


class TestTheMarketplaceClone:
    async def test_session_start_does_not_clone_the_default(self, tmp_path):
        git = GitRecorder()
        phase = MarketplaceWarmupPhase(_session_with(_air_gapped(tmp_path)))
        with patch("ember_code.core.plugins.git.subprocess.run", git):
            await phase._refresh_all()
        assert git.calls == []

    async def test_a_public_marketplace_registered_earlier_is_not_refreshed(self, tmp_path):
        """Turning the switch on after the default was registered must not
        leave a six-hourly clone of github.com behind."""
        settings = _air_gapped(tmp_path)
        _register(Path(settings.storage.data_dir), MarketplaceEntry(name="official", url=OFFICIAL))
        git = GitRecorder()
        with patch("ember_code.core.plugins.git.subprocess.run", git):
            await MarketplaceWarmupPhase(_session_with(settings))._refresh_all()
        assert git.calls == []

    async def test_the_clone_has_its_own_switch(self, tmp_path):
        settings = Settings(
            storage=StorageConfig(data_dir=str(tmp_path / "data")),
            plugins=PluginsConfig(default_marketplace=False),
        )
        git = GitRecorder()
        with patch("ember_code.core.plugins.git.subprocess.run", git):
            await MarketplaceWarmupPhase(_session_with(settings))._refresh_all()
        assert git.calls == []

    async def test_the_customers_own_marketplace_is_registered(self, tmp_path):
        repo = _local_marketplace(tmp_path)
        settings = _air_gapped(tmp_path, marketplaces=[str(repo)])
        git = GitRecorder(passthrough=True)
        with patch("ember_code.core.plugins.git.subprocess.run", git):
            await MarketplaceWarmupPhase(_session_with(settings))._refresh_all()

        assert git.clone_urls() == [str(repo)]
        registry = _marketplaces.load_registry(settings.storage.data_dir)
        assert [m.name for m in registry.marketplaces] == ["acme-internal"]

    def test_a_user_cannot_add_a_public_marketplace(self, tmp_path):
        gateway = PluginBackendGateway(
            data_dir=str(tmp_path),
            source_policy=PluginSourcePolicy.from_settings(_air_gapped(tmp_path)),
        )
        git = GitRecorder()
        with patch("ember_code.core.plugins.git.subprocess.run", git):
            added = gateway.add_marketplace(OFFICIAL)
            installed = gateway.install("git@github.com:acme/plugin.git", ref=None, subdir=None)
        assert git.calls == []
        assert not added.ok and "air-gapped" in added.error
        assert not installed.ok and "air-gapped" in installed.error


class TestTheSourcePolicy:
    @pytest.mark.parametrize(
        "url",
        [
            "/srv/plugins/marketplace",
            "./marketplace",
            "file:///srv/plugins/marketplace.git",
            r"C:\plugins\marketplace",
            "C:/plugins/marketplace",
        ],
    )
    def test_local_sources_are_allowed(self, url):
        assert PluginSourcePolicy(air_gapped=True).allows(url)

    @pytest.mark.parametrize(
        "url",
        [
            OFFICIAL,
            "git@github.com:anthropics/claude-plugins-official.git",
            "github.com:anthropics/claude-plugins-official",
            "ssh://git@gitlab.com/acme/m.git",
        ],
    )
    def test_remote_sources_are_refused(self, url):
        assert not PluginSourcePolicy(air_gapped=True).allows(url)

    def test_a_host_the_customer_named_is_inside_the_network(self):
        policy = PluginSourcePolicy(
            air_gapped=True,
            customer_marketplaces=["https://git.acme.internal/tools/marketplace.git"],
        )
        assert policy.allows("https://git.acme.internal/tools/marketplace")
        assert policy.allows("git@git.acme.internal:tools/some-plugin.git")
        assert not policy.allows("https://github.com/acme/some-plugin")

    def test_air_gap_removes_the_default_from_registration(self):
        assert PluginSourcePolicy(air_gapped=True).marketplaces_to_register() == []
        assert PluginSourcePolicy().marketplaces_to_register() == [OFFICIAL]

    def test_without_the_switch_everything_is_allowed(self):
        assert PluginSourcePolicy().allows(OFFICIAL)


# ── D12, D13: WebFetch and WebSearch ────────────────────────────────


class TestTheWebTools:
    def test_they_are_denied(self, tmp_path):
        settings = _air_gapped(tmp_path)
        assert settings.permissions.web_fetch == "deny"
        assert settings.permissions.web_search == "deny"

    def test_a_config_allow_does_not_reopen_them(self, tmp_path):
        (Path.home() / ".igni").mkdir(parents=True, exist_ok=True)
        (Path.home() / ".igni" / "config.yaml").write_text(
            "air_gapped: true\npermissions:\n  web_fetch: allow\n  web_search: allow\n"
        )
        settings = load_settings(project_dir=tmp_path)
        assert settings.permissions.web_fetch == "deny"
        assert settings.permissions.web_search == "deny"

    def test_no_web_toolkit_is_built(self, tmp_path):
        settings = _air_gapped(tmp_path)
        permissions = ToolPermissions(
            project_dir=tmp_path, settings_permissions=settings.permissions
        )
        registry = ToolRegistry(base_dir=str(tmp_path), permissions=permissions)
        with (
            patch.object(WebFetchSpec, "build") as fetch_build,
            patch.object(WebSearchSpec, "build") as search_build,
        ):
            result = registry.resolve_typed(
                ToolResolutionRequest(tool_names=["WebFetch", "WebSearch"])
            )
        fetch_build.assert_not_called()
        search_build.assert_not_called()
        assert result.tools == []
        assert sorted(result.skipped_denied) == ["WebFetch", "WebSearch"]


# ── The decision model's Hugging Face download ──────────────────────


class FakeRouter:
    """Stands in for ``laya.Router``: records how it was built and under
    which hub mode, without importing torch."""

    def __init__(self, fails: bool = False) -> None:
        self.built: list[bool] = []
        self._fails = fails

    def __call__(self, **kwargs):
        self.built.append(decisions_model.hub.HF_HUB_OFFLINE)
        if self._fails:
            raise OSError("a checkpoint is missing from the cache")
        return object()


@pytest.fixture
def offline_hub(monkeypatch, tmp_path):
    """An air-gapped decisions service over an empty cache in ``tmp_path``.

    Every global the load touches is registered with monkeypatch first, so
    the switch to offline mode is undone after the test.
    """
    monkeypatch.setenv(air_gap.AIR_GAPPED_ENV, "1")
    monkeypatch.setattr(decisions_model.hub, "HF_HUB_CACHE", str(tmp_path / "hub"))
    monkeypatch.setattr(decisions_model.hub, "HF_HUB_OFFLINE", False)
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        monkeypatch.delenv(name, raising=False)
    router = FakeRouter()
    monkeypatch.setitem(sys.modules, "laya", types.SimpleNamespace(Router=router))
    return router


def _seed_cache(tmp_path: Path) -> None:
    repo = "models--" + decisions_model.BUNDLE_REPO.replace("/", "--")
    (tmp_path / "hub" / repo / "snapshots" / "abc123").mkdir(parents=True)


class TestTheDecisionModel:
    def test_an_empty_cache_is_refused_without_loading(self, offline_hub):
        with pytest.raises(DecisionsUnavailable) as refused:
            decisions_model.LayaModel().load()
        assert offline_hub.built == []
        assert "air-gapped" in str(refused.value)
        assert "prefetch_models" in str(refused.value), "says how to pre-seed it"

    def test_a_seeded_cache_loads_with_the_hub_offline(self, offline_hub, tmp_path):
        _seed_cache(tmp_path)
        decisions_model.LayaModel().load()
        assert offline_hub.built == [True]
        # And it stays offline: the router loads later checkpoints lazily.
        assert decisions_model.hub.HF_HUB_OFFLINE is True
        assert os.environ["HF_HUB_OFFLINE"] == "1"

    def test_an_incomplete_cache_is_not_retried_online(self, offline_hub, monkeypatch, tmp_path):
        _seed_cache(tmp_path)
        failing = FakeRouter(fails=True)
        monkeypatch.setitem(sys.modules, "laya", types.SimpleNamespace(Router=failing))
        with pytest.raises(DecisionsUnavailable) as refused:
            decisions_model.LayaModel().load()
        assert failing.built == [True], "one offline attempt, no online fallback"
        assert "incomplete" in str(refused.value)

    def test_the_backend_hands_the_switch_to_its_children(self):
        env: dict[str, str] = {}
        air_gap.export_to(env, False)
        assert env == {}
        air_gap.export_to(env, True)
        assert air_gap.air_gapped_by_environment(env)

    def test_a_stand_in_settings_object_does_not_switch_it(self):
        env: dict[str, str] = {}
        air_gap.export_to(env, object())  # type: ignore[arg-type]
        assert env == {}


# ── /plugin update ──────────────────────────────────────────────────


def _local_plugin(root: Path, name: str = "acme-tool") -> Path:
    repo = root / "plugin-src"
    (repo / ".claude-plugin").mkdir(parents=True)
    (repo / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name}))
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "v1"], check=True)
    return repo


class TestPluginUpdate:
    @pytest.fixture
    def installed(self, tmp_path) -> tuple[Path, Path]:
        """A plugin installed from a local repository, under air-gap."""
        repo = _local_plugin(tmp_path)
        data = tmp_path / "data"
        PluginInstaller(data_dir=data, source_policy=PluginSourcePolicy(air_gapped=True)).install(
            str(repo)
        )
        return data, repo

    @staticmethod
    def _installer(data: Path) -> PluginInstaller:
        return PluginInstaller(data_dir=data, source_policy=PluginSourcePolicy(air_gapped=True))

    def test_the_source_is_recorded_at_install(self, installed):
        data, repo = installed
        assert load_state(data).sources == {"acme-tool": str(repo)}

    def test_a_local_source_still_updates(self, installed):
        data, _repo = installed
        git = GitRecorder(passthrough=True)
        with patch("ember_code.core.plugins.git.subprocess.run", git):
            self._installer(data).update("acme-tool")
        assert any(c[:2] == ["git", "fetch"] for c in git.calls)

    def test_a_public_source_is_refused_before_git_runs(self, installed):
        data, _repo = installed
        state = load_state(data)
        state.sources["acme-tool"] = "https://github.com/acme/acme-tool"
        save_state(state, data_dir=data)
        git = GitRecorder()
        with (
            patch("ember_code.core.plugins.git.subprocess.run", git),
            pytest.raises(PluginError, match="air-gapped"),
        ):
            self._installer(data).update("acme-tool")
        assert git.calls == []

    def test_a_plugin_installed_before_sources_were_recorded_is_checked_by_its_remote(
        self, installed
    ):
        data, _repo = installed
        state = load_state(data)
        state.sources.clear()
        save_state(state, data_dir=data)
        subprocess.run(
            [
                "git",
                "-C",
                str(data / "plugins" / "acme-tool"),
                "remote",
                "set-url",
                "origin",
                "git@github.com:acme/acme-tool.git",
            ],
            check=True,
        )
        git = GitRecorder(passthrough=True)
        with (
            patch("ember_code.core.plugins.git.subprocess.run", git),
            pytest.raises(PluginError, match="air-gapped"),
        ):
            self._installer(data).update("acme-tool")
        # Only the local read of its config; no fetch.
        assert [c[:2] for c in git.calls] == [["git", "config"]]

    def test_removal_forgets_the_source(self, installed):
        data, _repo = installed
        self._installer(data).remove("acme-tool")
        assert load_state(data).sources == {}

    def test_install_itself_refuses_a_public_url(self, tmp_path):
        git = GitRecorder()
        with (
            patch("ember_code.core.plugins.git.subprocess.run", git),
            pytest.raises(PluginError, match="air-gapped"),
        ):
            self._installer(tmp_path).install("https://github.com/acme/acme-tool")
        assert git.calls == []
