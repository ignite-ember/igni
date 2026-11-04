"""In an app's or a space's VM the service token comes from the producer's own token.

igni server's service-identity §9.2: the same token exchange a person's
session goes through, with the app's or the space's token as
``subject_token``; the result is an ``app`` or ``space`` token for the
service's URL. The VM's token is the one it keeps from its credential
(``IGNI_ACCESS_TOKEN_FILE``), and the caller igni names to its
extensions is the app or the space, never a person.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from ember_code.extensions.principal import PrincipalKind, SignedInPerson
from ember_code.extensions.service_token import CredentialsSession, ServiceTokenExchange
from ember_code.extensions.store import ExtensionStore

SERVICE = "https://index.acme.test"


def _token(**claims: Any) -> str:
    def part(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    return f"{part(b'{}')}.{part(json.dumps(claims).encode())}.sig"


@pytest.fixture
def vm_token(cloud_layer: Any, tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "token"
    monkeypatch.setenv("IGNI_ACCESS_TOKEN_FILE", str(path))
    return path


@pytest.mark.parametrize(
    ("claims", "kind"),
    [
        ({"sub": "reporter", "kind": "app", "name": "Reporter"}, PrincipalKind.APP),
        (
            {"sub": "sp-1", "kind": "space", "app": "reporter", "external_id": "cust-9"},
            PrincipalKind.SPACE,
        ),
    ],
)
async def test_the_producers_own_token_is_exchanged_for_its_services(
    vm_token: Path, tmp_path: Path, claims: dict[str, Any], kind: PrincipalKind
):
    own = _token(org="org-acme", exp=int(time.time()) + 900, **claims)
    vm_token.write_text(own)
    forms: list[dict[str, str]] = []

    def igni(request: httpx.Request) -> httpx.Response:
        forms.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return httpx.Response(
            200,
            json={
                "access_token": f"{kind.value}-for-the-service",
                "expires_in": 600,
                "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
            },
        )

    exchange = ServiceTokenExchange(
        api_url=lambda: "https://igni.test",
        session=CredentialsSession(),
        transport=httpx.MockTransport(igni),
    )
    first = await exchange.token_for(SERVICE)
    again = await exchange.token_for(SERVICE)
    assert first.access_token == again.access_token == f"{kind.value}-for-the-service"
    assert forms == [
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": own,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "audience": SERVICE,
            "organisation": "org-acme",
        }
    ], "one exchange, cached as a person's is"

    principal = SignedInPerson(None, ExtensionStore(tmp_path / "store"))()
    assert principal is not None and principal.kind is kind and principal.group is None
    if kind is PrincipalKind.SPACE:
        assert (principal.app, principal.external_id) == ("reporter", "cust-9")
        assert principal.as_meta()["igni/principal"]["external_id"] == "cust-9"
    else:
        assert "app" not in principal.as_meta()["igni/principal"]
