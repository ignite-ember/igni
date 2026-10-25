"""An extension's token for its own service, fetched by the host.

igni server's ``docs/service-identity.md`` §9. The extension never holds the
person's igni session. The host trades it at igni's token endpoint
(RFC 8693) for a ten-minute token whose audience is the extension's
service, caches it, renews it shortly before it expires, and serves it
on a loopback endpoint to the process holding the right secret:
``401`` for any other, ``503`` when nobody is signed in.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from ember_code.backend.login_coordinator import LoginCoordinator
from ember_code.backend.schemas_rpc import LoginResult
from ember_code.extensions.installer import ExtensionInstaller
from ember_code.extensions.python_env import PythonEnvironments
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState
from ember_code.extensions.rpc import ExtensionRpc
from ember_code.extensions.service_token import (
    ACCESS_TOKEN_TYPE,
    ENV_SERVICE_URL,
    ENV_TOKEN_SECRET,
    ENV_TOKEN_URL,
    TOKEN_EXCHANGE_GRANT,
    IgniSession,
    NotSignedIn,
    ServiceTokenEndpoint,
    ServiceTokenExchange,
    TokenExchangeFailed,
)
from ember_code.extensions.store import DistributionRecord, ExtensionStore
from tests.extension_distribution_helpers import API_URL, SESSION, FakeIgni, build_wheel, manifest

NOTES = "https://notes.example.com"
LEDGER = "https://ledger.example.com"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Signed:
    """Who is signed in, changeable mid-test."""

    def __init__(self) -> None:
        self.session: IgniSession | None = IgniSession(access_token=SESSION, org_id="org-1")

    def __call__(self) -> IgniSession | None:
        return self.session


@pytest.fixture
def igni() -> FakeIgni:
    return FakeIgni()


@pytest.fixture
def signed() -> Signed:
    return Signed()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def exchange(igni, signed, clock) -> ServiceTokenExchange:
    return ServiceTokenExchange(
        api_url=lambda: API_URL, session=signed, clock=clock, transport=igni.transport()
    )


# ── The exchange ───────────────────────────────────────────────────


async def test_the_session_is_exchanged_for_the_service_s_audience(exchange, igni):
    forms: list[dict[str, list[str]]] = []
    igni.on_token = forms.append

    token = await exchange.token_for(NOTES)

    assert token.access_token == f"token-1-for-{NOTES}"
    assert forms == [
        {
            "grant_type": [TOKEN_EXCHANGE_GRANT],
            "subject_token": [SESSION],
            "subject_token_type": [ACCESS_TOKEN_TYPE],
            "audience": [NOTES],
            "organisation": ["org-1"],
        }
    ]
    assert igni.requests[0].url == httpx.URL(f"{API_URL}/v1/oauth/token")


async def test_a_fresh_token_is_served_from_the_cache(exchange, igni, clock):
    first = await exchange.token_for(NOTES)
    clock.now += 300
    second = await exchange.token_for(NOTES)
    assert second.access_token == first.access_token
    assert igni.tokens_issued == 1
    assert exchange.remaining(second) == 300


async def test_it_is_renewed_shortly_before_it_expires(exchange, igni, clock):
    await exchange.token_for(NOTES)
    clock.now += 600 - 59  # inside the last minute
    renewed = await exchange.token_for(NOTES)
    assert igni.tokens_issued == 2
    assert renewed.access_token == f"token-2-for-{NOTES}"
    assert exchange.remaining(renewed) == 600


async def test_each_service_has_its_own_token(exchange, igni):
    notes = await exchange.token_for(NOTES)
    ledger = await exchange.token_for(LEDGER)
    assert notes.access_token != ledger.access_token
    assert igni.tokens_issued == 2


async def test_concurrent_asks_make_one_exchange(exchange, igni):
    tokens = await asyncio.gather(*(exchange.token_for(NOTES) for _ in range(5)))
    assert {t.access_token for t in tokens} == {f"token-1-for-{NOTES}"}
    assert igni.tokens_issued == 1


async def test_signing_in_as_somebody_else_never_serves_the_last_person_s_token(
    exchange, igni, signed
):
    await exchange.token_for(NOTES)
    signed.session = IgniSession(access_token="another-person")
    igni_tokens_before = igni.tokens_issued
    await exchange.token_for(NOTES)
    assert igni.tokens_issued == igni_tokens_before + 1


async def test_signed_out_is_not_signed_in_and_forgets_the_cache(exchange, igni, signed):
    await exchange.token_for(NOTES)
    signed.session = None
    with pytest.raises(NotSignedIn):
        await exchange.token_for(NOTES)
    signed.session = IgniSession(access_token=SESSION)
    await exchange.token_for(NOTES)
    assert igni.tokens_issued == 2, "nothing cached may outlive a sign-out"


async def test_no_server_configured_is_not_signed_in(igni, signed):
    exchange = ServiceTokenExchange(api_url=lambda: "", session=signed, transport=igni.transport())
    with pytest.raises(NotSignedIn, match="no igni server"):
        await exchange.token_for(NOTES)


async def test_a_refusal_from_igni_is_reported(exchange, igni):
    igni.token_status = 400
    with pytest.raises(TokenExchangeFailed, match="400"):
        await exchange.token_for(NOTES)


# ── The loopback endpoint ──────────────────────────────────────────


@pytest.fixture
async def endpoint(exchange):
    served = ServiceTokenEndpoint(exchange)
    yield served
    await served.stop()


async def _get(url: str, *, secret: str | None = None, **kwargs: Any) -> httpx.Response:
    headers = kwargs.pop("headers", {})
    if secret is not None:
        headers["Authorization"] = f"Bearer {secret}"
    async with httpx.AsyncClient(trust_env=False) as client:
        return await client.request(kwargs.pop("method", "GET"), url, headers=headers, **kwargs)


async def test_a_grant_is_three_variables_and_a_loopback_url(endpoint):
    env = await endpoint.grant("acme.notes", NOTES)
    assert set(env) == {ENV_SERVICE_URL, ENV_TOKEN_URL, ENV_TOKEN_SECRET}
    assert env[ENV_SERVICE_URL] == NOTES
    assert env[ENV_TOKEN_URL].startswith("http://127.0.0.1:")
    assert len(env[ENV_TOKEN_SECRET]) >= 32


async def test_the_right_secret_gets_the_token(endpoint):
    env = await endpoint.grant("acme.notes", NOTES)
    response = await _get(env[ENV_TOKEN_URL], secret=env[ENV_TOKEN_SECRET])
    assert response.status_code == 200
    assert response.json() == {
        "access_token": f"token-1-for-{NOTES}",
        "expires_in": 600,
        "service_url": NOTES,
    }
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("authorization", ["Bearer wrong", "Basic c2VjcmV0", None])
async def test_any_other_secret_is_401(endpoint, authorization, igni):
    env = await endpoint.grant("acme.notes", NOTES)
    headers = {"Authorization": authorization} if authorization else {}
    response = await _get(env[ENV_TOKEN_URL], headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert igni.tokens_issued == 0, "a refused caller must not cost an exchange"


async def test_not_signed_in_is_503(endpoint, signed):
    env = await endpoint.grant("acme.notes", NOTES)
    signed.session = None
    response = await _get(env[ENV_TOKEN_URL], secret=env[ENV_TOKEN_SECRET])
    assert response.status_code == 503
    assert "signed in" in response.json()["error"]


async def test_the_secret_decides_the_audience(endpoint):
    """Two extensions, two services, one listener: a process gets its
    own service's token and cannot ask for another's."""
    notes = await endpoint.grant("acme.notes", NOTES)
    ledger = await endpoint.grant("acme.ledger", LEDGER)
    assert notes[ENV_TOKEN_URL] == ledger[ENV_TOKEN_URL]
    got = await _get(ledger[ENV_TOKEN_URL], secret=ledger[ENV_TOKEN_SECRET])
    assert got.json()["service_url"] == LEDGER
    assert got.json()["access_token"].endswith(LEDGER)


async def test_a_revoked_secret_is_401(endpoint):
    env = await endpoint.grant("acme.notes", NOTES)
    endpoint.revoke("acme.notes")
    response = await _get(env[ENV_TOKEN_URL], secret=env[ENV_TOKEN_SECRET])
    assert response.status_code == 401


async def test_a_new_grant_replaces_the_old_secret(endpoint):
    old = await endpoint.grant("acme.notes", NOTES)
    new = await endpoint.grant("acme.notes", NOTES)
    assert (await _get(old[ENV_TOKEN_URL], secret=old[ENV_TOKEN_SECRET])).status_code == 401
    assert (await _get(new[ENV_TOKEN_URL], secret=new[ENV_TOKEN_SECRET])).status_code == 200


async def test_only_get(endpoint):
    env = await endpoint.grant("acme.notes", NOTES)
    response = await _get(env[ENV_TOKEN_URL], secret=env[ENV_TOKEN_SECRET], method="POST")
    assert response.status_code == 405
    assert response.headers["allow"] == "GET"


async def test_another_host_is_refused(endpoint):
    """DNS rebinding: a page whose name resolves to 127.0.0.1 arrives
    with its own Host header."""
    env = await endpoint.grant("acme.notes", NOTES)
    response = await _get(
        env[ENV_TOKEN_URL], secret=env[ENV_TOKEN_SECRET], headers={"Host": "evil.example"}
    )
    assert response.status_code == 421


async def test_another_path_is_404(endpoint):
    env = await endpoint.grant("acme.notes", NOTES)
    url = env[ENV_TOKEN_URL].replace("/v1/service-token", "/v1/other")
    assert (await _get(url, secret=env[ENV_TOKEN_SECRET])).status_code == 404


async def test_it_listens_on_loopback_only(endpoint):
    await endpoint.start()
    sockets = endpoint._server.sockets  # the bound address, not the URL we print
    assert {s.getsockname()[0] for s in sockets} == {"127.0.0.1"}


# ── What the installer tells a process ─────────────────────────────


class StubMcp:
    def __init__(self) -> None:
        self.configs: dict[str, Any] = {}

    async def connect(self, name: str) -> Any:
        return type("Ok", (), {"ok": True, "reason": ""})()

    async def disconnect_one(self, name: str) -> None:
        self.configs.pop(name, None)

    def get_client(self, name: str) -> None:
        return None

    def get_tools(self, name: str) -> dict[str, Any]:
        return {}

    def require_confirmation(self, server: str, tools: set[str]) -> None:
        pass

    async def tool_annotations(self, name: str) -> dict[str, Any]:
        return {}


def dev_bundle(tmp_path: Path, **changes: Any) -> Path:
    bundle = tmp_path / "dev"
    bundle.mkdir()
    raw = {
        "id": "acme.notes",
        "version": "0.1.0",
        "contract": ">=1.0 <2",
        "trust": "unsigned",
        "dev": {"command": ["python", "server.py"]},
        "contributes": {
            "settings": [{"key": "serviceUrl", "type": "string", "label": "Service", "default": ""}]
        },
        **changes,
    }
    (bundle / "manifest.json").write_text(json.dumps(raw))
    return bundle


async def test_a_development_install_names_its_service_by_setting(tmp_path, endpoint):
    store = ExtensionStore(tmp_path / "store")
    bundle = dev_bundle(tmp_path)
    store.save_settings(store.read(bundle), {"serviceUrl": NOTES})
    registry = ExtensionRegistry()
    mcp = StubMcp()
    installer = ExtensionInstaller(
        registry=registry,
        rpc=ExtensionRpc(registry),
        mcp=mcp,
        allow_development=True,
        store=store,
        service_tokens=endpoint,
    )
    entry = await installer.install(bundle)
    assert entry.state is ExtensionState.READY, entry.reason
    env = mcp.configs["ext:acme.notes"].env
    assert env[ENV_SERVICE_URL] == NOTES
    response = await _get(env[ENV_TOKEN_URL], secret=env[ENV_TOKEN_SECRET])
    assert response.status_code == 200

    await installer.uninstall("acme.notes")
    after = await _get(env[ENV_TOKEN_URL], secret=env[ENV_TOKEN_SECRET])
    assert after.status_code == 401, "a stopped extension's secret must stop working"


async def test_no_service_means_no_token_variables(tmp_path, endpoint):
    store = ExtensionStore(tmp_path / "store")
    registry = ExtensionRegistry()
    mcp = StubMcp()
    installer = ExtensionInstaller(
        registry=registry,
        rpc=ExtensionRpc(registry),
        mcp=mcp,
        allow_development=True,
        store=store,
        service_tokens=endpoint,
    )
    await installer.install(dev_bundle(tmp_path))
    assert not {ENV_SERVICE_URL, ENV_TOKEN_URL, ENV_TOKEN_SECRET} & set(
        mcp.configs["ext:acme.notes"].env
    )


async def test_a_bundle_cannot_point_its_own_token_url_elsewhere(tmp_path, endpoint):
    """Its own ``dev.env`` comes first and the grant overwrites it."""
    store = ExtensionStore(tmp_path / "store")
    bundle = dev_bundle(
        tmp_path,
        dev={
            "command": ["python", "server.py"],
            "env": {ENV_TOKEN_URL: "http://attacker.example/", ENV_SERVICE_URL: "x"},
        },
    )
    store.save_settings(store.read(bundle), {"serviceUrl": NOTES})
    registry = ExtensionRegistry()
    mcp = StubMcp()
    await ExtensionInstaller(
        registry=registry,
        rpc=ExtensionRpc(registry),
        mcp=mcp,
        allow_development=True,
        store=store,
        service_tokens=endpoint,
    ).install(bundle)
    env = mcp.configs["ext:acme.notes"].env
    assert env[ENV_TOKEN_URL].startswith("http://127.0.0.1:")
    assert env[ENV_SERVICE_URL] == NOTES


async def test_a_distributed_python_extension_fetches_its_token(tmp_path, endpoint, igni):
    """End to end on this machine: installed offline from its wheels,
    started with the three variables, and the extension's own code
    gets its service's token from the host — never the session."""
    bundle = tmp_path / "bundle"
    build_wheel(bundle / "wheels")
    (bundle / "manifest.json").write_text(json.dumps(manifest()))
    registry = ExtensionRegistry()
    mcp = StubMcp()
    installer = ExtensionInstaller(
        registry=registry,
        rpc=ExtensionRpc(registry),
        mcp=mcp,
        environments=PythonEnvironments(tmp_path / "envs"),
        service_tokens=endpoint,
    )
    entry = await installer.install(
        bundle,
        distribution=DistributionRecord(
            extension_id="acme.notes", version="1.0.0", sha256="a" * 64, service_url=NOTES
        ),
    )
    assert entry.state is ExtensionState.READY, entry.reason
    config = mcp.configs["ext:acme.notes"]
    assert SESSION not in json.dumps(config.env)

    process = await asyncio.create_subprocess_exec(
        config.command,
        stdout=asyncio.subprocess.PIPE,
        env={**config.env, "PATH": "/usr/bin:/bin"},
    )
    out, _ = await asyncio.wait_for(process.communicate(), timeout=30)
    answer = json.loads(out)
    assert answer["access_token"] == f"token-1-for-{NOTES}"
    assert answer["service_url"] == NOTES
    assert answer["service_url_env"] == NOTES


# ── Sign-in brings the extensions ──────────────────────────────────


class FakeBackend:
    def __init__(self, ok: bool) -> None:
        self.ok = ok

    async def login(self, on_status: Any = None) -> LoginResult:
        return LoginResult(ok=self.ok, email="p@example.com" if self.ok else "", error="")

    def reload_cloud_credentials(self) -> None:
        return None


class FakePush:
    async def on_login_status(self, text: str) -> None:
        return None

    async def on_login_result(self, *, success: bool, result: str) -> None:
        return None


@pytest.mark.parametrize("ok", [True, False])
async def test_a_sign_in_schedules_a_sync_and_a_failed_one_does_not(ok):
    login = LoginCoordinator(backend=FakeBackend(ok), push_bridge=FakePush())  # type: ignore[arg-type]
    calls: list[str] = []
    login.on_signed_in(lambda: calls.append("sync"))
    await login.start()
    await login._task
    assert calls == (["sync"] if ok else [])
