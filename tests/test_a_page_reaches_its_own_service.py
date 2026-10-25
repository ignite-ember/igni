"""An extension's page talks to its own service directly (``bridge.service``).

The security model, as tests:

* The frame's ``connect-src`` is the page itself, the extension's **own
  service** and the hosts it **declared** in ``permissions.egress`` —
  shown to the administrator before approval — and nothing else.
* Every one of them over ``https``/``wss`` only; plain ``http``/``ws``
  only to this machine, in development. Exact origins.
* The page's token is for the extension's own service and nothing else:
  the audience is looked up from the caller, never asked for, so it is
  not another extension's service and never an egress host.
* The frame's other CSP directives stay as strict as they were.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from ember_code.backend.session_orchestrator import SessionOrchestrator
from ember_code.extensions.installer import ExtensionInstaller
from ember_code.extensions.manifest import ExtensionManifest
from ember_code.extensions.origin import ExtensionOrigins
from ember_code.extensions.permissions import (
    Egress,
    PermissionDenied,
    Permissions,
    ServiceOrigin,
)
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState
from ember_code.extensions.rpc import ExtensionRpc
from ember_code.extensions.service_token import (
    IgniSession,
    NoService,
    PageServiceTokens,
    ServiceTokenExchange,
)
from ember_code.extensions.store import ExtensionStore
from ember_code.protocol import messages as msg
from ember_code.protocol.rpc import RpcMethod
from tests.extension_distribution_helpers import API_URL, SESSION, FakeIgni

NOTES = "https://notes.example.com"
LEDGER = "https://ledger.example.com"


def manifest_of(**changes: Any) -> ExtensionManifest:
    raw: dict[str, Any] = {
        "id": "acme.notes",
        "version": "1.0.0",
        "contract": ">=1.0 <2",
        "trust": "unsigned",
        "dev": {"command": ["node", "server.js"]},
    }
    raw.update(changes)
    return ExtensionManifest.model_validate(raw)


def connect_src(csp: str) -> list[str]:
    for directive in csp.split(";"):
        name, _, value = directive.strip().partition(" ")
        if name == "connect-src":
            return value.split()
    raise AssertionError(f"no connect-src in {csp!r}")


# ── Which origins a page may reach ─────────────────────────────────


def test_the_service_is_its_origin_over_https_and_wss_and_nothing_wider():
    service = ServiceOrigin.parse("https://notes.example.com:8443/api/v1")
    assert service is not None
    assert service.csp_sources() == [
        "https://notes.example.com:8443",
        "wss://notes.example.com:8443",
    ]


def test_service_and_declared_egress_are_the_whole_list():
    egress = Egress(manifest_of(permissions={"egress": ["maps.example.org"]}))
    assert egress.csp_connect_src(ServiceOrigin.parse(NOTES)).split() == [
        "'self'",
        "https://notes.example.com",
        "wss://notes.example.com",
        "https://maps.example.org",
        "wss://maps.example.org",
    ]


def test_no_service_and_no_egress_is_the_page_itself():
    assert Egress(manifest_of()).csp_connect_src(None) == "'self'"


@pytest.mark.parametrize(
    "url",
    ["http://notes.example.com", "ftp://notes.example.com", "notes.example.com", "", None],
)
def test_a_service_a_page_may_not_reach_is_no_service(url):
    assert ServiceOrigin.parse(url) is None


def test_plain_http_to_this_machine_only_in_development():
    assert ServiceOrigin.parse("http://localhost:8000") is None
    local = ServiceOrigin.parse("http://localhost:8000", development=True)
    assert local is not None
    assert local.csp_sources() == ["http://localhost:8000", "ws://localhost:8000"]
    assert ServiceOrigin.parse("http://notes.example.com", development=True) is None


def test_a_localhost_egress_host_gets_plain_http_only_in_development():
    manifest = manifest_of(permissions={"egress": ["localhost:9000"]})
    assert "http://localhost:9000" not in Egress(manifest).csp_connect_src()
    developing = Egress(manifest, development=True).csp_connect_src()
    assert "http://localhost:9000" in developing
    assert "ws://localhost:9000" in developing


def test_an_endpoint_igni_dials_is_https_except_this_machine_in_development():
    declared = {"permissions": {"egress": ["scanner.internal", "localhost:9000"]}}
    with pytest.raises(PermissionDenied, match="not https"):
        Permissions(manifest_of(**declared)).egress.check_endpoint("http://scanner.internal/mcp")
    with pytest.raises(PermissionDenied, match="not https"):
        Permissions(manifest_of(**declared)).egress.check_endpoint("http://localhost:9000/mcp")
    Permissions(manifest_of(**declared), development=True).egress.check_endpoint(
        "http://localhost:9000/mcp"
    )


# ── What the installer serves ──────────────────────────────────────


class StubMcp:
    def __init__(self) -> None:
        self.configs: dict[str, Any] = {}

    async def connect(self, name: str) -> Any:
        return SimpleNamespace(ok=True, reason="")

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


def page_bundle(tmp_path: Path, **changes: Any) -> Path:
    bundle = tmp_path / "dev"
    (bundle / "ui").mkdir(parents=True)
    (bundle / "ui" / "index.html").write_text("<!doctype html><title>Notes</title>")
    raw = {
        "id": "acme.notes",
        "version": "0.1.0",
        "contract": ">=1.0 <2",
        "trust": "unsigned",
        "dev": {"command": ["python", "server.py"]},
        "contributes": {
            "pages": [{"kind": "acme.notes", "label": "Notes"}],
            "settings": [
                {"key": "serviceUrl", "type": "string", "label": "Service", "default": ""}
            ],
        },
        **changes,
    }
    (bundle / "manifest.json").write_text(json.dumps(raw))
    return bundle


async def install_page(
    tmp_path: Path, service_url: str, **changes: Any
) -> tuple[ExtensionOrigins, ExtensionInstaller]:
    store = ExtensionStore(tmp_path / "store")
    bundle = page_bundle(tmp_path, **changes)
    if service_url:
        store.save_settings(store.read(bundle), {"serviceUrl": service_url})
    registry = ExtensionRegistry()
    origins = ExtensionOrigins()
    installer = ExtensionInstaller(
        registry=registry,
        rpc=ExtensionRpc(registry),
        mcp=StubMcp(),
        origins=origins,
        allow_development=True,
        store=store,
    )
    entry = await installer.install(bundle)
    assert entry.state is ExtensionState.READY, entry.reason
    return origins, installer


async def served_csp(origins: ExtensionOrigins, extension_id: str) -> str:
    url = origins.url_for(extension_id)
    assert url is not None
    async with httpx.AsyncClient(trust_env=False) as client:
        response = await client.get(f"{url}/index.html")
    assert response.status_code == 200
    return response.headers["content-security-policy"]


async def test_the_frame_s_csp_admits_exactly_its_service_and_its_egress(tmp_path):
    origins, installer = await install_page(
        tmp_path,
        "https://notes.example.com/api",
        permissions={"egress": ["maps.example.org"]},
    )
    try:
        csp = await served_csp(origins, "acme.notes")
        assert connect_src(csp) == [
            "'self'",
            "https://notes.example.com",
            "wss://notes.example.com",
            "https://maps.example.org",
            "wss://maps.example.org",
        ]
        # The URL as configured — it is the token's audience, the same
        # one the process's token is for.
        assert origins.service_url_for("acme.notes") == "https://notes.example.com/api"
    finally:
        await installer.uninstall("acme.notes")


async def test_the_frame_s_other_directives_stay_strict(tmp_path):
    origins, installer = await install_page(tmp_path, NOTES)
    try:
        csp = await served_csp(origins, "acme.notes")
        for directive in (
            "default-src 'self'",
            "script-src 'self'",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'none'",
            "font-src 'self'",
        ):
            assert directive in csp
        assert "notes.example.com" not in csp.replace(
            "connect-src " + " ".join(connect_src(csp)), ""
        ), "the service reaches connect-src and nowhere else"
    finally:
        await installer.uninstall("acme.notes")


async def test_no_service_known_means_none_in_the_csp(tmp_path):
    origins, installer = await install_page(tmp_path, "")
    try:
        assert connect_src(await served_csp(origins, "acme.notes")) == ["'self'"]
        assert origins.service_url_for("acme.notes") is None
    finally:
        await installer.uninstall("acme.notes")


async def test_a_plain_http_service_off_this_machine_is_not_admitted(tmp_path):
    origins, installer = await install_page(tmp_path, "http://notes.example.com")
    try:
        assert connect_src(await served_csp(origins, "acme.notes")) == ["'self'"]
        assert origins.service_url_for("acme.notes") is None
    finally:
        await installer.uninstall("acme.notes")


# ── Whose token a page gets ────────────────────────────────────────


@pytest.fixture
def igni() -> FakeIgni:
    return FakeIgni()


@pytest.fixture
def exchange(igni) -> ServiceTokenExchange:
    return ServiceTokenExchange(
        api_url=lambda: API_URL,
        session=lambda: IgniSession(access_token=SESSION, org_id="org-1"),
        transport=igni.transport(),
    )


@pytest.fixture
async def two_pages(tmp_path):
    origins = ExtensionOrigins()
    for extension_id, service in (("acme.notes", NOTES), ("acme.ledger", LEDGER)):
        root = tmp_path / extension_id
        root.mkdir()
        (root / "index.html").write_text("<!doctype html>")
        await origins.serve(extension_id, root, service_url=service)
    bare = tmp_path / "acme.bare"
    bare.mkdir()
    await origins.serve("acme.bare", bare)
    yield origins
    await origins.stop_all()


async def test_a_page_s_token_is_for_its_own_service(two_pages, exchange, igni):
    forms: list[dict[str, list[str]]] = []
    igni.on_token = forms.append
    tokens = PageServiceTokens(origins=two_pages, exchange=exchange)

    notes = await tokens.for_page("acme.notes")
    ledger = await tokens.for_page("acme.ledger")

    assert notes.service_url == NOTES and notes.access_token.endswith(NOTES)
    assert ledger.service_url == LEDGER and ledger.access_token.endswith(LEDGER)
    assert [form["audience"] for form in forms] == [[NOTES], [LEDGER]]


async def test_no_service_no_origin_no_token(two_pages, exchange, igni):
    tokens = PageServiceTokens(origins=two_pages, exchange=exchange)
    with pytest.raises(NoService):
        await tokens.for_page("acme.bare")
    with pytest.raises(NoService):
        await tokens.for_page("acme.not-installed")
    assert igni.tokens_issued == 0


async def test_the_page_and_its_process_share_one_exchange(two_pages, exchange, igni):
    await exchange.token_for(NOTES)  # the process asked first
    await PageServiceTokens(origins=two_pages, exchange=exchange).for_page("acme.notes")
    assert igni.tokens_issued == 1


class Sent:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    async def send(self, message: Any) -> None:
        self.messages.append(message)


def orchestrator_with(tokens: PageServiceTokens | None) -> tuple[SessionOrchestrator, Sent]:
    orchestrator = SessionOrchestrator.__new__(SessionOrchestrator)
    sent = Sent()
    orchestrator._transport = sent
    orchestrator._page_tokens = tokens
    return orchestrator, sent


async def test_the_rpc_answers_the_caller_s_own_service(two_pages, exchange):
    orchestrator, sent = orchestrator_with(PageServiceTokens(origins=two_pages, exchange=exchange))
    request = msg.RPCRequest(
        id="r1",
        method=RpcMethod.EXTENSION_SERVICE_TOKEN,
        # An audience in the arguments is not read: the caller decides.
        args={"audience": LEDGER},
        caller="acme.notes",
    )
    await orchestrator._handle_service_token(request)
    result = sent.messages[0].result
    assert result["ok"] is True
    assert result["service_url"] == NOTES
    assert result["access_token"].endswith(NOTES)


async def test_the_rpc_refuses_a_call_from_no_extension(two_pages, exchange):
    orchestrator, sent = orchestrator_with(PageServiceTokens(origins=two_pages, exchange=exchange))
    request = msg.RPCRequest(id="r1", method=RpcMethod.EXTENSION_SERVICE_TOKEN, args={})
    await orchestrator._handle_service_token(request)
    assert sent.messages[0].error


async def test_the_rpc_says_when_there_is_no_service(two_pages, exchange):
    orchestrator, sent = orchestrator_with(PageServiceTokens(origins=two_pages, exchange=exchange))
    request = msg.RPCRequest(
        id="r1", method=RpcMethod.EXTENSION_SERVICE_TOKEN, args={}, caller="acme.bare"
    )
    await orchestrator._handle_service_token(request)
    result = sent.messages[0].result
    assert result["ok"] is False
    assert "no service" in result["error"]
