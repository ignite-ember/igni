"""A model on igni server's gateway sends the current token, not the one it was built with.

The integration run (all components real) found every chat in a VM
refused with 401: the model was built before the VM had exchanged its
credential, so it sent ``Bearer not-set``; the token it would have got
lasts fifteen minutes and was never read again; and the client installed
after every run had no auth at all. The model's client now reads the
token on each request (:class:`CloudTokenAuth`), and keeps doing so
across the client replaced after a run.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from ember_code.backend.model_http_client_manager import ModelHttpClientManager
from ember_code.core.auth.credentials import Credentials, CredentialsStore
from ember_code.core.auth.http_auth import CloudTokenAuth
from ember_code.core.config.llm_call_logger import LlmCallLogger
from ember_code.core.config.model_entry import CLOUD_TOKEN_SENTINEL, ModelRegistryEntry
from ember_code.core.config.provider_builders import OpenAILikeBuilder


def _token(sub: str) -> str:
    def part(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    claims = {"sub": sub, "org": "org-acme", "exp": int(time.time()) + 900}
    return f"{part(b'{}')}.{part(json.dumps(claims).encode())}.sig"


class SignIn:
    """The credentials file, as a sign-in or the VM's exchange writes it."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def as_(self, token: str) -> None:
        CredentialsStore(str(self.path)).save(Credentials.new(token, "ada@acme.test"))


class Gateway:
    """igni server's gateway: records the bearer, refuses a token it does not know."""

    def __init__(self, *accepted: str) -> None:
        self.accepted = set(accepted)
        self.seen: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        bearer = request.headers.get("Authorization", "").removeprefix("Bearer ")
        self.seen.append(bearer)
        if bearer in self.accepted:
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(401, json={"detail": "Invalid or expired token"})

    async def call(self, auth: httpx.Auth | None) -> int:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(self.handler), auth=auth
        ) as client:
            response = await client.post(
                "https://igni.test/v1/chat/completions",
                headers={"Authorization": "Bearer not-set"},  # what the SDK puts there
            )
        return response.status_code


@pytest.fixture
def sign_in(tmp_path: Path) -> SignIn:
    return SignIn(tmp_path / "credentials.json")


def _model(sign_in: SignIn, *, cloud_token: str | None) -> Any:
    entry = ModelRegistryEntry(
        model_id="qwen", url="https://igni.test/v1", api_key=CLOUD_TOKEN_SENTINEL
    )
    return OpenAILikeBuilder().build(
        entry,
        cloud_token=cloud_token,
        llm_logger=LlmCallLogger(),
        cloud_auth=CloudTokenAuth(str(sign_in.path)),
    )


async def test_the_first_chat_after_binding_without_a_token(sign_in: SignIn) -> None:
    model = _model(sign_in, cloud_token=None)  # built before the VM had a token
    ada = _token("u-ada")
    sign_in.as_(ada)  # the VM's credential exchange, afterwards
    gateway = Gateway(ada)

    assert await gateway.call(model.http_client.auth) == 200
    assert gateway.seen == [ada]


async def test_a_run_after_the_token_rotated(sign_in: SignIn) -> None:
    first, second = _token("u-ada"), _token("u-ada") + "x"
    sign_in.as_(first)
    model = _model(sign_in, cloud_token=first)
    sign_in.as_(second)  # fifteen minutes later
    gateway = Gateway(second)

    assert await gateway.call(model.http_client.auth) == 200
    assert gateway.seen == [second]


async def test_a_token_rotated_while_a_request_was_out_is_tried_again(sign_in: SignIn) -> None:
    old, new = _token("u-ada"), _token("u-ada") + "x"
    sign_in.as_(old)
    auth = CloudTokenAuth(str(sign_in.path))
    gateway = Gateway(new)
    original = gateway.handler

    def rotate_then_answer(request: httpx.Request) -> httpx.Response:
        sign_in.as_(new)
        return original(request)

    gateway.handler = rotate_then_answer  # type: ignore[method-assign]
    assert await gateway.call(auth) == 200
    assert gateway.seen == [old, new], "once more, with the token that replaced it"


async def test_a_second_run_after_the_client_was_replaced(sign_in: SignIn) -> None:
    ada = _token("u-ada")
    sign_in.as_(ada)
    model = _model(sign_in, cloud_token=ada)
    team = type("Team", (), {"model": model})()
    before = model.http_client

    await ModelHttpClientManager().close_and_replace(team)  # type: ignore[arg-type]

    assert model.http_client is not before
    assert model.http_client.auth is before.auth, "the token still read at each request"
    assert model.http_client.timeout == before.timeout
    assert await Gateway(ada).call(model.http_client.auth) == 200


def test_a_key_of_its_own_is_left_alone(sign_in: SignIn) -> None:
    entry = ModelRegistryEntry(model_id="gpt", url="https://api.example.com/v1", api_key="sk-1")
    model = OpenAILikeBuilder().build(
        entry,
        cloud_token=None,
        llm_logger=LlmCallLogger(),
        cloud_auth=CloudTokenAuth(str(sign_in.path)),
    )
    assert not isinstance(model.http_client.auth, CloudTokenAuth)
    assert entry.resolve_key().cloud is False
    assert ModelRegistryEntry(model_id="q", api_key=CLOUD_TOKEN_SENTINEL).resolve_key().cloud
