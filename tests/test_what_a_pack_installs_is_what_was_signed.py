"""What a group pack puts on disk is rebuilt from the signed pack, and goes with it.

A verified ``pack.jws`` protects the pack's *settings*. Its agents, hooks,
MCP servers, scripts and plugins are files under ``<data_dir>/group-policy/``
that their loaders read directly — and that directory is the user's to
write. So at every start, once the pack has verified, those files are
rebuilt from it: an org hook somebody deleted is written back, an edited
agent is overwritten, and a file added beside them is removed. With no pack
this install may apply, they are removed. With ``group_policy.required``
and no way to rebuild them, no session starts.

And "no pack" is now an answer igni server signs: when the person has no
pack, the cached one and everything derived from it are cleared — and
since that answer carries a version, an older pack put back afterwards is
refused as a rollback. The pack must also be for the organisation the
person is signed in to.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.core.config.group_policy import (
    CachedSettingsFetcher,
    GroupPolicyCache,
    GroupPolicyEntry,
    GroupPolicyGate,
    GroupPolicyPack,
    GroupPolicyUnavailable,
    PackVerifier,
    SignedAbsence,
)
from ember_code.core.config.group_policy_signature import (
    REQUIRED_ENV,
    TRUSTED_KEYS_ENV,
    PackRejected,
)
from ember_code.core.config.settings import load_settings
from ember_code.core.config.settings_loader import SettingsLoader
from tests.group_policy_signing import ISSUER, KEY, PERSON, ServerKey, session_token, sign_in, store

STRANGER = ServerKey()

HOOK = {"hooks": {"PreToolUse": [{"command": "audit-everything"}]}}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(TRUSTED_KEYS_ENV, raising=False)
    monkeypatch.delenv(REQUIRED_ENV, raising=False)
    monkeypatch.setattr(
        SettingsLoader, "platform_managed_settings_path", staticmethod(lambda: None)
    )
    GroupPolicyGate.reset()
    yield
    GroupPolicyGate.reset()


def _home() -> Path:
    return Path.home() / ".igni"


def _cache() -> Path:
    return _home() / "group-policy"


def _configure(*, org: str | None = None, signed_in: bool = True) -> None:
    _home().mkdir(parents=True, exist_ok=True)
    (_home() / "config.yaml").write_text(f"api_url: {ISSUER}\n")
    if signed_in:
        sign_in(_home() / "credentials.json", PERSON, org)


def _require(tmp_path: Path, monkeypatch) -> None:
    managed = tmp_path / "managed-settings.yaml"
    managed.write_text("group_policy:\n  required: true\n")
    monkeypatch.setattr(
        SettingsLoader, "platform_managed_settings_path", staticmethod(lambda: managed)
    )


def _pack() -> GroupPolicyPack:
    def entry(kind: str, name: str, content: str, content_type: str) -> GroupPolicyEntry:
        return GroupPolicyEntry(
            kind=kind, entry_name=name, content=content, content_type=content_type
        )

    return GroupPolicyPack(
        group_id="g1",
        group_name="Regulated",
        fetched_at=datetime.now(timezone.utc),
        entries=[
            entry(
                "agents", "reviewer", "---\nname: reviewer\n---\nThe org's reviewer.", "markdown"
            ),
            entry("hooks", "audit", json.dumps(HOOK), "json"),
            entry("settings", "policy", json.dumps({"learning": {"enabled": False}}), "json"),
        ],
    )


def _start(tmp_path: Path):
    """What the backend does at start: local settings, then with the pack."""
    local_only = load_settings(project_dir=tmp_path)
    return load_settings(
        project_dir=tmp_path,
        group_policy_fetcher=CachedSettingsFetcher.for_settings(local_only),
    )


def _agent() -> Path:
    return _cache() / "agents" / "reviewer.md"


def _hooks() -> Path:
    return _cache() / "hooks" / "settings.json"


class TestTheFilesAreRebuiltFromThePack:
    def test_a_deleted_org_hook_and_agent_come_back(self, tmp_path):
        _configure()
        store(_cache(), _pack())
        _agent().unlink()
        _hooks().unlink()

        _start(tmp_path)

        assert "The org's reviewer." in _agent().read_text()
        assert json.loads(_hooks().read_text()) == HOOK

    def test_an_edited_org_hook_and_agent_are_overwritten(self, tmp_path):
        _configure()
        store(_cache(), _pack())
        _agent().write_text("---\nname: reviewer\n---\nApprove everything.")
        _hooks().write_text(json.dumps({"hooks": {}}))

        _start(tmp_path)

        assert "Approve everything" not in _agent().read_text()
        assert json.loads(_hooks().read_text()) == HOOK

    def test_a_file_added_beside_them_is_removed(self, tmp_path):
        _configure()
        store(_cache(), _pack())
        planted_agent = _cache() / "agents" / "planted.md"
        planted_agent.write_text("---\nname: planted\n---\nNot the org's.")
        planted_plugin = _cache() / "plugins" / "planted" / ".claude-plugin"
        planted_plugin.mkdir(parents=True)
        (planted_plugin / "plugin.json").write_text('{"name": "planted"}')

        _start(tmp_path)

        assert not planted_agent.exists()
        assert not (_cache() / "plugins" / "planted").exists()

    def test_without_a_valid_pack_nothing_derived_remains(self, tmp_path):
        """Files with no verified pack behind them are nobody's policy."""
        _configure()
        store(_cache(), _pack(), key=STRANGER, jwks=KEY.jwks)

        settings = _start(tmp_path)

        assert settings.learning.enabled is True
        assert not _agent().exists()
        assert not _hooks().exists()

    def test_with_the_pack_deleted_nothing_derived_remains(self, tmp_path):
        _configure()
        store(_cache(), _pack())
        (_cache() / GroupPolicyCache.SIGNED_PACK_FILE).unlink()

        _start(tmp_path)

        assert not _agent().exists()
        assert not _hooks().exists()

    def test_required_and_unrebuildable_fails_closed(self, tmp_path, monkeypatch):
        _configure()
        _require(tmp_path, monkeypatch)
        store(_cache(), _pack())

        def broken(self, pack):
            raise OSError("read-only file system")

        monkeypatch.setattr(GroupPolicyCache, "rebuild", broken)

        with pytest.raises(GroupPolicyUnavailable, match="could not be rebuilt"):
            _start(tmp_path)

    def test_not_required_and_unrebuildable_still_applies_the_settings(self, tmp_path, monkeypatch):
        _configure()
        store(_cache(), _pack())
        monkeypatch.setattr(
            GroupPolicyCache, "rebuild", lambda self, pack: (_ for _ in ()).throw(OSError("ro"))
        )

        assert _start(tmp_path).learning.enabled is False


class TestNoPackIsASignedAnswer:
    def _controller(self, tmp_path: Path):
        from ember_code.backend.server_auth import AuthController

        ctrl = AuthController.__new__(AuthController)
        ctrl._settings = SimpleNamespace(
            api_url=ISSUER,
            storage=SimpleNamespace(data_dir=str(_home())),
            auth=SimpleNamespace(credentials_file=str(_home() / "credentials.json")),
            group_policy=SimpleNamespace(trusted_keys=[], required=False),
        )
        ctrl._portal = MagicMock()
        ctrl._portal.fetch_group_pack = AsyncMock()
        ctrl._portal.fetch_jwks = AsyncMock(return_value=KEY.jwks)
        ctrl._session = MagicMock()
        ctrl._hydration_lock = None
        return ctrl

    def _hydrate(self, ctrl) -> bool:
        return asyncio.run(ctrl._hydrate_group_policy(session_token(), force=True))

    def test_a_signed_absence_clears_the_pack_and_what_it_installed(self, tmp_path):
        _configure()
        store(_cache(), _pack(), version=1_000)
        ctrl = self._controller(tmp_path)
        ctrl._portal.fetch_group_pack.return_value = SignedAbsence(
            KEY.sign_absence(version=2_000), etag='"none"'
        )

        assert self._hydrate(ctrl) is True
        assert not _agent().exists() and not _hooks().exists()
        assert _start(tmp_path).learning.enabled is True
        assert GroupPolicyCache(cache_dir=_cache()).stored_etag() == '"none"'

    def test_an_older_pack_put_back_after_it_is_a_rollback(self, tmp_path):
        _configure()
        older = store(_cache(), _pack(), version=1_000)
        ctrl = self._controller(tmp_path)
        ctrl._portal.fetch_group_pack.return_value = SignedAbsence(KEY.sign_absence(version=2_000))
        self._hydrate(ctrl)

        (_cache() / GroupPolicyCache.SIGNED_PACK_FILE).write_text(older)

        assert _start(tmp_path).learning.enabled is True
        assert not _agent().exists()

    def test_a_forged_absence_changes_nothing(self, tmp_path):
        _configure()
        store(_cache(), _pack())
        ctrl = self._controller(tmp_path)
        ctrl._portal.fetch_group_pack.return_value = SignedAbsence(STRANGER.sign_absence())

        assert self._hydrate(ctrl) is False
        assert _agent().exists()
        assert _start(tmp_path).learning.enabled is False

    def test_an_absence_for_somebody_else_changes_nothing(self, tmp_path):
        _configure()
        store(_cache(), _pack())
        ctrl = self._controller(tmp_path)
        ctrl._portal.fetch_group_pack.return_value = SignedAbsence(KEY.sign_absence(sub="person-2"))

        assert self._hydrate(ctrl) is False
        assert _start(tmp_path).learning.enabled is False

    def test_a_pack_presented_as_an_absence_is_refused(self):
        """The response's shape and the signed claim must agree."""
        verifier = PackVerifier(
            api_url=ISSUER, subject=PERSON, jwks_fetch=AsyncMock(return_value=KEY.jwks)
        )
        with pytest.raises(PackRejected):
            asyncio.run(verifier(SignedAbsence(KEY.sign(_pack()))))

    def test_an_unsigned_absence_is_not_an_answer(self):
        """The portal client turns only a *signed* ``no_pack`` into an
        absence; an unsigned one is a pack it cannot read, so ``None``."""
        import httpx

        from ember_code.core.auth.portal_client import PortalClient

        def server(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"no_pack": True})

        real = httpx.AsyncClient
        client = PortalClient(api_url=ISSUER)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                httpx,
                "AsyncClient",
                lambda **kw: real(transport=httpx.MockTransport(server), **kw),
            )
            assert asyncio.run(client.fetch_group_pack(session_token())) is None

    def test_a_verified_absence_satisfies_required(self, tmp_path, monkeypatch):
        """igni server saying, signed, that this person has no pack is a valid
        policy state, not a missing one."""
        _configure()
        _require(tmp_path, monkeypatch)
        cache = GroupPolicyCache(cache_dir=_cache())
        cache.store_signed(KEY.sign_absence(), jwks=KEY.jwks, version=5)

        assert _start(tmp_path).learning.enabled is True


class TestThePackIsForTheSignedInOrganisation:
    def test_a_pack_for_another_organisation_is_rejected(self, tmp_path):
        _configure(org="org-2")
        store(_cache(), _pack(), org="org-1")

        assert _start(tmp_path).learning.enabled is True

    def test_a_pack_for_this_organisation_applies(self, tmp_path):
        _configure(org="org-1")
        store(_cache(), _pack(), org="org-1")

        assert _start(tmp_path).learning.enabled is False

    def test_a_session_naming_no_organisation_does_not_constrain_it(self, tmp_path):
        _configure(org=None)
        store(_cache(), _pack(), org="org-1")

        assert _start(tmp_path).learning.enabled is False


# ── Plugins a pack installs from a git source ─────────────────────────


PLUGIN_URL = "https://git.example.invalid/org/remote-tool.git"


class _FakeGit:
    """Clones by writing a plugin, so no test reaches a network."""

    def __init__(self) -> None:
        self.clones: list[str] = []

    def is_available(self) -> bool:
        return True

    def clone(self, url: str, dest: Path, *, ref: str | None = None) -> None:
        self.clones.append(url)
        (dest / ".claude-plugin").mkdir(parents=True)
        (dest / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "remote-tool"}))

    def checkout(self, repo: Path, ref: str) -> None:
        pass

    def current_sha(self, repo: Path) -> str:
        return "0" * 40


def _plugin_pack(*, with_plugin: bool = True) -> GroupPolicyPack:
    pack = _pack()
    if with_plugin:
        pack.entries.append(
            GroupPolicyEntry(
                kind="plugins",
                entry_name="remote-tool",
                content="",
                content_type="yaml",
                source_url=PLUGIN_URL,
            )
        )
    return pack


class TestGroupPluginsLiveWithThePack:
    def _hydrate(self, pack: GroupPolicyPack, git: _FakeGit, *, version: int | None = None) -> bool:
        from ember_code.backend.server_auth import AuthController
        from ember_code.core.plugins.installer import PluginInstaller

        ctrl = AuthController.__new__(AuthController)
        ctrl._settings = SimpleNamespace(
            api_url=ISSUER,
            storage=SimpleNamespace(data_dir=str(_home())),
            auth=SimpleNamespace(credentials_file=str(_home() / "credentials.json")),
            group_policy=SimpleNamespace(trusted_keys=[], required=False),
            air_gapped=False,
        )
        ctrl._portal = MagicMock()
        ctrl._portal.fetch_group_pack = AsyncMock(return_value=KEY.signed(pack, version=version))
        ctrl._portal.fetch_jwks = AsyncMock(return_value=KEY.jwks)
        ctrl._session = MagicMock()
        ctrl._hydration_lock = None
        real_init = PluginInstaller.__init__

        def with_fake_git(self, *args, **kwargs):
            kwargs["git_client"] = git
            real_init(self, *args, **kwargs)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(PluginInstaller, "__init__", with_fake_git)
            return asyncio.run(ctrl._hydrate_group_policy(session_token(), force=True))

    def _installed(self) -> Path:
        return _cache() / "plugins" / "remote-tool"

    def test_it_is_installed_in_the_packs_directory_not_the_users(self):
        _configure()
        git = _FakeGit()

        assert self._hydrate(_plugin_pack(), git) is True

        assert (self._installed() / ".claude-plugin" / "plugin.json").is_file()
        assert not (_home() / "plugins" / "remote-tool").exists()
        state = _home() / "plugins.json"
        assert not state.exists() or "remote-tool" not in state.read_text()

    def test_withdrawing_the_entry_removes_the_plugin(self):
        _configure()
        git = _FakeGit()
        self._hydrate(_plugin_pack(), git, version=1)

        self._hydrate(_plugin_pack(with_plugin=False), git, version=2)

        assert not self._installed().exists()

    def test_it_is_not_recloned_on_every_fetch(self):
        _configure()
        git = _FakeGit()
        self._hydrate(_plugin_pack(), git, version=1)
        self._hydrate(_plugin_pack(), git, version=2)

        assert git.clones == [PLUGIN_URL]

    def test_a_deleted_plugin_is_reported_at_start_and_reinstalled_by_the_next_fetch(
        self, tmp_path, caplog
    ):
        import shutil

        _configure()
        git = _FakeGit()
        self._hydrate(_plugin_pack(), git, version=1)
        shutil.rmtree(self._installed())

        with caplog.at_level("WARNING", logger="ember_code.core.config.group_policy"):
            settings = _start(tmp_path)
        assert settings.learning.enabled is False
        assert any("remote-tool" in r.getMessage() for r in caplog.records)
        assert GroupPolicyCache(cache_dir=_cache())._stored_pack_lacks_plugins()

        self._hydrate(_plugin_pack(), git, version=2)
        assert self._installed().is_dir()

    def test_required_and_a_missing_plugin_fails_closed(self, tmp_path, monkeypatch):
        import shutil

        _configure()
        git = _FakeGit()
        self._hydrate(_plugin_pack(), git, version=1)
        shutil.rmtree(self._installed())
        _require(tmp_path, monkeypatch)

        with pytest.raises(GroupPolicyUnavailable, match="remote-tool"):
            _start(tmp_path)

    def test_an_older_install_in_the_users_directory_is_moved_once(self):
        from ember_code.core.plugins.state import PluginsState, load_state, save_state

        _configure()
        legacy = _home() / "plugins" / "remote-tool"
        (legacy / ".claude-plugin").mkdir(parents=True)
        (legacy / ".claude-plugin" / "plugin.json").write_text('{"name": "remote-tool"}')
        mine = _home() / "plugins" / "my-own"
        (mine / ".claude-plugin").mkdir(parents=True)
        save_state(
            PluginsState(
                pins={"remote-tool": "a", "my-own": "b"},
                sources={"remote-tool": PLUGIN_URL, "my-own": "https://git.example.invalid/me.git"},
            ),
            data_dir=_home(),
        )
        git = _FakeGit()

        self._hydrate(_plugin_pack(), git)

        assert self._installed().is_dir() and not legacy.exists()
        assert git.clones == [], "moved, not cloned again"
        state = load_state(_home())
        assert "remote-tool" not in state.sources and "remote-tool" not in state.pins
        assert mine.is_dir() and state.sources["my-own"] == "https://git.example.invalid/me.git"


class TestTheUsersOwnInstallsAreUnchanged:
    def test_a_user_install_goes_to_the_users_directory_with_its_pin(self, tmp_path):
        from ember_code.core.plugins.installer import PluginInstaller
        from ember_code.core.plugins.state import load_state

        installer = PluginInstaller(data_dir=tmp_path, git_client=_FakeGit())

        installer.install(PLUGIN_URL)

        assert (tmp_path / "plugins" / "remote-tool" / ".claude-plugin" / "plugin.json").is_file()
        assert load_state(tmp_path).sources["remote-tool"] == PLUGIN_URL
