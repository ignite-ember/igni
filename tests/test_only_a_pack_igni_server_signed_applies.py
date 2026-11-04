"""igni applies an organisation's group policy only if igni server issued it.

The policy is fetched from igni server and cached under
``<data_dir>/group-policy/``, where it is applied at the next start — above
CLI flags. That directory is the user's to write. Unsigned, the cache could
be edited, swapped for another person's pack, rolled back, or simply
deleted, by the user, by malware running as them, or by a file somebody
planted, and igni treated every one of those as "the organisation's policy"
or as "no policy".

Now igni server signs each person's pack (a compact JWS under the key it
signs tokens with, ``kid`` in the header), igni stores it exactly as
received (``pack.jws``), and verifies it before any of it is merged:

* the signature, against a pinned key (``group_policy.trusted_keys``,
  managed-only) or else igni server's published JWKS;
* ``iss`` against ``api_url``, ``sub`` against the person signed in;
* ``version`` not older than the newest pack already accepted.

A pack that fails any of these is never merged. With the managed-only
``group_policy.required`` set, igni then tries one live fetch, and failing
that starts no agent session and says how to get a valid pack.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import click
import httpx
import pytest

from ember_code.core.config.group_policy import (
    CachedSettingsFetcher,
    GroupPolicyCache,
    GroupPolicyEntry,
    GroupPolicyGate,
    GroupPolicyPack,
    GroupPolicyUnavailable,
    PackVerifier,
)
from ember_code.core.config.group_policy_signature import (
    REQUIRED_ENV,
    TRUSTED_KEYS_ENV,
    PackRejected,
    verify_pack,
)
from ember_code.core.config.settings import load_settings
from ember_code.core.config.settings_loader import SettingsLoader
from tests.group_policy_signing import ISSUER, KEY, PERSON, ServerKey, session_token, sign_in, store

#: A key igni server never held — what a forger signs with.
STRANGER = ServerKey()


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


def _cache_dir() -> Path:
    return _home() / "group-policy"


def _pack(payload: dict | None = None) -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g1",
        group_name="Regulated",
        fetched_at=datetime.now(timezone.utc),
        entries=[
            GroupPolicyEntry(
                kind="settings",
                entry_name="policy",
                content=json.dumps(payload or {"learning": {"enabled": False}}),
                content_type="json",
            )
        ],
    )


def _configure(
    *, api_url: str = ISSUER, signed_in_as: str | None = PERSON, extra: str = ""
) -> None:
    """This install: configured for ``api_url``, with ``signed_in_as`` signed in."""
    _home().mkdir(parents=True, exist_ok=True)
    (_home() / "config.yaml").write_text(f"api_url: {api_url}\n{extra}")
    if signed_in_as:
        sign_in(_home() / "credentials.json", signed_in_as)


def _managed(tmp_path: Path, monkeypatch, text: str) -> None:
    managed = tmp_path / "managed-settings.yaml"
    managed.write_text(text)
    monkeypatch.setattr(
        SettingsLoader, "platform_managed_settings_path", staticmethod(lambda: managed)
    )


def _settings(tmp_path: Path):
    """Settings the way the backend builds them: local first, then with the pack."""
    local_only = load_settings(project_dir=tmp_path)
    return load_settings(
        project_dir=tmp_path,
        group_policy_fetcher=CachedSettingsFetcher.for_settings(local_only),
    )


def _applied(tmp_path: Path) -> bool:
    return _settings(tmp_path).learning.enabled is False


def _segments(compact: str) -> list[str]:
    return compact.split(".")


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _claims(compact: str) -> dict:
    segment = _segments(compact)[1]
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


class TestAValidPackIsApplied:
    def test_a_pack_igni_server_signed_for_this_person_applies(self, tmp_path):
        _configure()
        store(_cache_dir(), _pack())

        assert _applied(tmp_path)

    def test_it_is_stored_exactly_as_received(self, tmp_path):
        _configure()
        compact = store(_cache_dir(), _pack())

        assert (_cache_dir() / GroupPolicyCache.SIGNED_PACK_FILE).read_text() == compact


class TestAForgedPackIsIgnored:
    def test_an_edited_pack_is_ignored(self, tmp_path):
        _configure()
        compact = store(_cache_dir(), _pack({"learning": {"enabled": True}}))
        head, _, signature = _segments(compact)
        claims = _claims(compact)
        claims["policy"]["entries"][0]["content"] = json.dumps({"learning": {"enabled": False}})
        (_cache_dir() / GroupPolicyCache.SIGNED_PACK_FILE).write_text(
            f"{head}.{_b64(claims)}.{signature}"
        )

        assert not _applied(tmp_path)

    def test_a_pack_signed_by_another_key_is_ignored(self, tmp_path):
        _configure()
        store(_cache_dir(), _pack(), key=STRANGER, jwks=KEY.jwks)

        assert not _applied(tmp_path)

    def test_an_unsigned_pack_is_ignored(self, tmp_path):
        _configure()
        store(_cache_dir(), _pack())
        unsigned = f"{_b64({'alg': 'none', 'typ': 'igni-group-policy+jwt'})}.{_b64(_claims((_cache_dir() / 'pack.jws').read_text()))}."
        (_cache_dir() / GroupPolicyCache.SIGNED_PACK_FILE).write_text(unsigned)

        assert not _applied(tmp_path)

    def test_a_plain_settings_file_is_never_read(self, tmp_path):
        """What the cache used to be: an unsigned JSON file of settings."""
        _configure()
        _cache_dir().mkdir(parents=True)
        (_cache_dir() / GroupPolicyCache.SETTINGS_FILE).write_text(
            json.dumps({"learning": {"enabled": False}})
        )

        assert not _applied(tmp_path)

    def test_a_token_is_not_a_pack(self, tmp_path):
        """Same key, different object: igni server's session tokens are signed
        with it too, and one must not pass for a pack."""
        _configure()
        store(_cache_dir(), _pack())
        token = KEY.sign_claims(
            {**_claims((_cache_dir() / "pack.jws").read_text()), "token_use": "session"},
            header={"typ": "JWT"},
        )
        (_cache_dir() / GroupPolicyCache.SIGNED_PACK_FILE).write_text(token)

        assert not _applied(tmp_path)

    def test_the_cached_keys_cannot_be_swapped_under_a_pin(self, tmp_path, monkeypatch):
        """Unpinned, the cached JWKS is only as safe as the directory. Pinned,
        replacing it with a forger's key buys nothing."""
        _configure()
        _managed(tmp_path, monkeypatch, f"group_policy:\n  trusted_keys: [{KEY.thumbprint}]\n")
        store(_cache_dir(), _pack(), key=STRANGER, jwks=STRANGER.jwks)

        assert not _applied(tmp_path)


class TestAPackForSomebodyElseIsRejected:
    def test_another_persons_pack(self, tmp_path):
        _configure(signed_in_as="person-2")
        store(_cache_dir(), _pack(), sub=PERSON)

        assert not _applied(tmp_path)

    def test_another_servers_pack(self, tmp_path):
        _configure(api_url="https://igni.other.invalid")
        store(_cache_dir(), _pack(), iss=ISSUER)

        assert not _applied(tmp_path)

    def test_nobody_signed_in(self, tmp_path):
        _configure(signed_in_as=None)
        store(_cache_dir(), _pack())

        assert not _applied(tmp_path)

    def test_the_issuer_comparison_ignores_a_trailing_slash(self, tmp_path):
        _configure(api_url=ISSUER + "/")
        store(_cache_dir(), _pack())

        assert _applied(tmp_path)


class TestARollbackIsRejected:
    def test_an_older_pack_put_back_is_refused(self, tmp_path):
        _configure()
        older = store(_cache_dir(), _pack(), version=1_000)
        store(_cache_dir(), _pack({"learning": {"enabled": True}}), version=2_000)
        (_cache_dir() / GroupPolicyCache.SIGNED_PACK_FILE).write_text(older)

        assert not _applied(tmp_path)

    def test_the_current_pack_is_not_a_rollback(self, tmp_path):
        _configure()
        store(_cache_dir(), _pack(), version=2_000)

        assert _applied(tmp_path)

    def test_the_check_itself(self):
        compact = KEY.sign(_pack(), version=5)
        with pytest.raises(PackRejected, match="rollback"):
            verify_pack(compact, jwks=KEY.jwks, api_url=ISSUER, subject=PERSON, min_version=6)


class TestRequiredFailsClosed:
    def test_no_pack_and_nobody_signed_in_refuses_to_start(self, tmp_path, monkeypatch):
        _configure(signed_in_as=None)
        _managed(tmp_path, monkeypatch, "group_policy:\n  required: true\n")

        with pytest.raises(GroupPolicyUnavailable, match="sign in"):
            _settings(tmp_path)

    def test_an_invalid_pack_refuses_to_start(self, tmp_path, monkeypatch):
        _configure()
        _managed(tmp_path, monkeypatch, "group_policy:\n  required: true\n")
        store(_cache_dir(), _pack(), key=STRANGER, jwks=KEY.jwks)
        # Signed in, so igni tries igni server first; it is unreachable.
        monkeypatch.setattr(
            "ember_code.core.config.group_policy.fetch_signed_pack_now", lambda *a, **k: False
        )

        with pytest.raises(GroupPolicyUnavailable, match="Connect to"):
            _settings(tmp_path)

    def test_a_live_fetch_is_tried_first(self, tmp_path, monkeypatch):
        """Signed in, with no usable pack: one fetch, and it is applied."""
        _configure()
        _managed(tmp_path, monkeypatch, "group_policy:\n  required: true\n")
        signed = KEY.sign(_pack())

        def igni_server(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/.well-known/jwks.json":
                return httpx.Response(200, json=KEY.jwks)
            assert request.url.path == "/v1/portal/me/group/pack"
            assert request.headers["authorization"] == f"Bearer {session_token()}"
            return httpx.Response(200, json={"group_id": "g1", "signed_pack": signed})

        from ember_code.core.activity import http as busy_http

        real_client = busy_http.BusyClient
        monkeypatch.setattr(
            busy_http,
            "BusyClient",
            lambda **kwargs: real_client(transport=httpx.MockTransport(igni_server), **kwargs),
        )

        assert _settings(tmp_path).learning.enabled is False
        assert (_cache_dir() / GroupPolicyCache.SIGNED_PACK_FILE).read_text() == signed

    def test_the_environment_can_require_it(self, tmp_path, monkeypatch):
        _configure(signed_in_as=None)
        monkeypatch.setenv(REQUIRED_ENV, "1")

        with pytest.raises(GroupPolicyUnavailable):
            _settings(tmp_path)

    def test_not_required_an_invalid_pack_is_ignored_with_a_warning(self, tmp_path, caplog):
        _configure()
        store(_cache_dir(), _pack(), key=STRANGER, jwks=KEY.jwks)

        with caplog.at_level("WARNING", logger="ember_code.core.config.group_policy"):
            settings = _settings(tmp_path)

        assert settings.learning.enabled is True
        assert any("ignored" in r.getMessage() for r in caplog.records)

    def test_the_backend_boots_but_runs_no_agent(self, tmp_path, monkeypatch):
        """The backend has to come up — signing in is how a valid pack is
        fetched — but no run starts, and the refusal says what to do."""
        from ember_code.backend.app import BackendApp
        from ember_code.backend.run_controller import RunController
        from ember_code.protocol import messages as msg

        _configure(signed_in_as=None)
        _managed(tmp_path, monkeypatch, "group_policy:\n  required: true\n")
        app = BackendApp(
            socket_path=None,
            ws_port=None,
            project_dir=tmp_path,
            resume_session_id=None,
            additional_dirs=None,
        )

        app._load_settings_with_group_policy()

        assert GroupPolicyGate.reason() and "sign in" in GroupPolicyGate.reason()
        controller = RunController.__new__(RunController)
        controller._phase = __import__(
            "ember_code.backend.run_controller", fromlist=["RunPhase"]
        ).RunPhase.idle
        controller._session = MagicMock()

        async def first_message():
            async for proto in controller._run_locked("hello", None):
                return proto

        refused = asyncio.run(first_message())
        assert isinstance(refused, msg.Error) and "group policy" in refused.text
        controller._session.main_team.arun.assert_not_called()

    def test_the_cli_starts_no_session(self, monkeypatch, tmp_path):
        from ember_code.cli import cli

        _configure(signed_in_as=None)
        _managed(tmp_path, monkeypatch, "group_policy:\n  required: true\n")

        with pytest.raises(click.ClickException, match="group policy"):
            cli.main(["-m", "hello"], standalone_mode=False)


class TestOnlyAnAdministratorPinsAKey:
    def test_a_pin_in_the_managed_file_is_honoured(self, tmp_path, monkeypatch):
        _configure()
        _managed(tmp_path, monkeypatch, f"group_policy:\n  trusted_keys: [{KEY.thumbprint}]\n")
        store(_cache_dir(), _pack())

        assert _settings(tmp_path).group_policy.trusted_keys == [KEY.thumbprint]
        assert _applied(tmp_path)

    def test_a_managed_pin_refuses_every_other_key(self, tmp_path, monkeypatch):
        _configure()
        _managed(tmp_path, monkeypatch, f"group_policy:\n  trusted_keys: [{STRANGER.thumbprint}]\n")
        store(_cache_dir(), _pack())

        assert not _applied(tmp_path)

    def test_a_pin_in_the_environment_is_honoured(self, tmp_path, monkeypatch):
        _configure()
        monkeypatch.setenv(TRUSTED_KEYS_ENV, STRANGER.thumbprint)
        store(_cache_dir(), _pack())

        assert _settings(tmp_path).group_policy.trusted_keys == [STRANGER.thumbprint]
        assert not _applied(tmp_path)

    def test_a_pin_in_the_users_config_is_ignored(self, tmp_path):
        """Were it honoured, a user could pin a key of their own. Here it would
        refuse the genuine pack; ignored, the genuine pack applies."""
        _configure(
            extra=f"group_policy:\n  trusted_keys: [{STRANGER.thumbprint}]\n  required: true\n"
        )
        store(_cache_dir(), _pack())

        settings = _settings(tmp_path)

        assert settings.group_policy.trusted_keys == []
        assert settings.group_policy.required is False
        assert settings.learning.enabled is False

    def test_a_pin_in_a_projects_config_is_ignored(self, tmp_path):
        _configure()
        (tmp_path / ".igni").mkdir()
        (tmp_path / ".igni" / "config.yaml").write_text(
            f"group_policy:\n  trusted_keys: [{STRANGER.thumbprint}]\n"
        )
        store(_cache_dir(), _pack())

        assert _settings(tmp_path).group_policy.trusted_keys == []
        assert _applied(tmp_path)

    def test_the_group_cannot_pin_its_own_key_or_unrequire_itself(self, tmp_path, monkeypatch):
        _configure()
        _managed(tmp_path, monkeypatch, "group_policy:\n  required: true\n")
        store(
            _cache_dir(),
            _pack({"group_policy": {"required": False, "trusted_keys": ["x"]}}),
        )

        settings = _settings(tmp_path)

        assert settings.group_policy.required is True
        assert settings.group_policy.trusted_keys == []


class TestTheFetchIsVerifiedToo:
    """``AuthController._hydrate_group_policy`` writes nothing it cannot verify."""

    def _controller(self, tmp_path: Path, *, api_url: str = ISSUER, pins: list | None = None):
        from ember_code.backend.server_auth import AuthController

        ctrl = AuthController.__new__(AuthController)
        ctrl._settings = SimpleNamespace(
            api_url=api_url,
            storage=SimpleNamespace(data_dir=str(tmp_path)),
            auth=SimpleNamespace(credentials_file=str(tmp_path / "credentials.json")),
            group_policy=SimpleNamespace(trusted_keys=pins or [], required=False),
        )
        ctrl._portal = MagicMock()
        ctrl._portal.fetch_group_pack = AsyncMock()
        ctrl._portal.fetch_jwks = AsyncMock(return_value=KEY.jwks)
        ctrl._hydration_lock = None
        return ctrl

    def test_a_genuine_pack_is_stored_with_its_keys_and_version(self, tmp_path):
        ctrl = self._controller(tmp_path)
        ctrl._portal.fetch_group_pack.return_value = KEY.signed(_pack(), version=42)

        assert asyncio.run(ctrl._hydrate_group_policy(session_token(), force=True)) is True
        cache = GroupPolicyCache(cache_dir=tmp_path / "group-policy")
        assert cache.signed_pack_text()
        assert cache.cached_jwks() == KEY.jwks
        assert cache.applied_version() == 42

    def test_a_forged_pack_from_the_wire_writes_nothing(self, tmp_path):
        ctrl = self._controller(tmp_path)
        ctrl._portal.fetch_group_pack.return_value = STRANGER.signed(_pack())

        assert asyncio.run(ctrl._hydrate_group_policy(session_token(), force=True)) is False
        assert not (tmp_path / "group-policy" / GroupPolicyCache.SIGNED_PACK_FILE).exists()

    def test_an_unsigned_pack_from_the_wire_writes_nothing(self, tmp_path):
        ctrl = self._controller(tmp_path)
        ctrl._portal.fetch_group_pack.return_value = _pack()

        assert asyncio.run(ctrl._hydrate_group_policy(session_token(), force=True)) is False
        assert not (tmp_path / "group-policy").exists() or not any(
            (tmp_path / "group-policy").iterdir()
        )

    def test_unpinned_keys_are_not_trusted_over_plain_http(self, tmp_path):
        verifier = PackVerifier(
            api_url="http://igni.corp.invalid",
            subject=PERSON,
            jwks_fetch=AsyncMock(return_value=KEY.jwks),
        )
        pack = KEY.signed(_pack(), iss="http://igni.corp.invalid")

        with pytest.raises(PackRejected, match="pin"):
            asyncio.run(verifier(pack))

    def test_a_pinned_key_is_trusted_over_plain_http(self, tmp_path):
        verifier = PackVerifier(
            api_url="http://igni.corp.invalid",
            subject=PERSON,
            pins=(KEY.thumbprint,),
            jwks_fetch=AsyncMock(return_value=KEY.jwks),
        )
        pack = KEY.signed(_pack(), iss="http://igni.corp.invalid")

        assert asyncio.run(verifier(pack)).pack.to_settings_dict() == {
            "learning": {"enabled": False}
        }

    def test_what_is_applied_is_the_signed_content(self, tmp_path):
        """The response carries plain fields beside the signature; only the
        signed content is materialised."""
        signed = KEY.sign(_pack())
        tampered = _pack({"learning": {"enabled": True}}).model_copy(update={"signed_pack": signed})
        verifier = PackVerifier(
            api_url=ISSUER, subject=PERSON, jwks_fetch=AsyncMock(return_value=KEY.jwks)
        )

        accepted = asyncio.run(verifier(tampered))

        assert accepted.pack.to_settings_dict() == {"learning": {"enabled": False}}
