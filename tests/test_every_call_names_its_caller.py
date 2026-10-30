"""Every call into an extension says whom it is for.

``docs/EXTENSION_ROUTING_AND_CONCURRENCY.md`` §6. A tool call, a page's
call and a slash command carry the principal in the MCP request's
``_meta["igni/principal"]``; a hook carries it on its frame. On this
backend it is the person signed in here — their id, organisation and
the one group they belong to — and a process asking the token endpoint for anybody else's
token is refused.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from mcp.server.fastmcp import Context, FastMCP
from mcp.shared.message import SessionMessage

from ember_code.core.auth.credentials import Credentials, CredentialsStore
from ember_code.core.hooks.events import HookEvent
from ember_code.core.hooks.extension_gate import DECIDING
from ember_code.core.mcp.client import MCPClientManager
from ember_code.core.mcp.config import MCPServerConfig, MCPTransport
from ember_code.core.mcp.request_meta import RequestMetaSession
from ember_code.extensions.host.hooks import ExtensionHooks
from ember_code.extensions.host.wire import HookAnswer
from ember_code.extensions.principal import Principal, PrincipalKind, SignedInPerson
from ember_code.extensions.registry import ExtensionRegistry
from ember_code.extensions.service_token import (
    ENV_TOKEN_SECRET,
    ENV_TOKEN_URL,
    IgniSession,
    ServiceTokenEndpoint,
    ServiceTokenExchange,
)
from ember_code.extensions.store import ExtensionStore
from tests.extension_distribution_helpers import API_URL, SESSION, FakeIgni
from tests.host_model_helpers import manifest

ADA = Principal(kind=PrincipalKind.PERSON, id="u-ada", org="org-acme", group="g-legal")


def _jwt(claims: dict[str, Any]) -> str:
    def part(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    return f"{part(b'{}')}.{part(json.dumps(claims).encode())}.sig"


def _signed_in(tmp_path: Path, **claims: Any) -> str:
    path = tmp_path / "credentials.json"
    CredentialsStore(str(path)).save(Credentials.new(_jwt(claims), "ada@acme.test"))
    return str(path)


# ── Who it is: the person signed in here ─────────────────────────────


class TestTheSignedInPerson:
    def test_is_named_from_their_session(self, tmp_path: Path) -> None:
        credentials = _signed_in(tmp_path, sub="u-ada", org="org-acme", group="g-legal")

        principal = SignedInPerson(credentials, ExtensionStore(tmp_path / "store"))()

        assert principal == ADA

    def test_their_group_comes_from_the_last_listing_when_there_was_one(
        self, tmp_path: Path
    ) -> None:
        """The session's is a snapshot of up to 30 days ago; the listing's was read when it listed."""
        credentials = _signed_in(tmp_path, sub="u-ada", org="org-acme", group="g-legal")
        store = ExtensionStore(tmp_path / "store")
        store.record_group("g-finance")

        principal = SignedInPerson(credentials, store)()

        assert principal is not None and principal.group == "g-finance"

    def test_a_listing_that_put_them_in_no_group_is_believed(self, tmp_path: Path) -> None:
        """Taken out of their group since the session was issued: the listing is the fresher word."""
        credentials = _signed_in(tmp_path, sub="u-ada", org="org-acme", group="g-legal")
        store = ExtensionStore(tmp_path / "store")
        store.record_group(None)

        principal = SignedInPerson(credentials, store)()

        assert principal is not None and principal.group is None

    def test_nobody_signed_in_is_no_principal(self, tmp_path: Path) -> None:
        person = SignedInPerson(str(tmp_path / "none.json"), ExtensionStore(tmp_path / "store"))

        assert person() is None
        assert person.meta() is None

    def test_it_travels_under_igni_principal(self) -> None:
        assert ADA.as_meta() == {
            "igni/principal": {
                "kind": "person",
                "id": "u-ada",
                "org": "org-acme",
                "group": "g-legal",
                "session": None,
            }
        }


# ── A tool call: in the request's _meta ──────────────────────────────


class TestAToolCallCarriesIt:
    async def test_every_tool_call_carries_the_providers_meta(self) -> None:
        server = FastMCP("acme.example")

        @server.tool()
        async def whoami(ctx: Context) -> str:  # type: ignore[type-arg]
            meta = ctx.request_context.meta
            return json.dumps(meta.model_dump().get("igni/principal") if meta else None)

        to_server_send, to_server_receive = anyio.create_memory_object_stream[
            SessionMessage | Exception
        ](8)
        to_client_send, to_client_receive = anyio.create_memory_object_stream[
            SessionMessage | Exception
        ](8)
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                server._mcp_server.run,
                to_server_receive,
                to_client_send,
                server._mcp_server.create_initialization_options(),
            )
            async with RequestMetaSession(
                to_client_receive, to_server_send, request_meta=ADA.as_meta
            ) as session:
                await session.initialize()
                result = await session.call_tool("whoami", {})
            tasks.cancel_scope.cancel()

        assert json.loads(result.content[0].text) == ADA.as_meta()[Principal.META_KEY]  # type: ignore[union-attr]

    async def test_only_an_extensions_server_is_given_it(self, tmp_path: Path) -> None:
        opened: list[tuple[str, Any]] = []

        class Binding:
            async def open(self, name: str, config: MCPServerConfig, **kwargs: Any) -> object:
                opened.append((name, kwargs.get("request_meta")))
                return object()

        manager = MCPClientManager(tmp_path, stdio_binding=Binding())  # type: ignore[arg-type]
        manager.extension_request_meta = ADA.as_meta
        for name, source in (("mcp_ext:acme.notes", "extension"), ("filesystem", "user")):
            config = MCPServerConfig(name=name, type=MCPTransport.stdio, command="x", source=source)
            await manager._open_transport(name, config)

        assert opened == [("mcp_ext:acme.notes", ADA.as_meta), ("filesystem", None)]


# ── A hook: on its frame ──────────────────────────────────────────────


async def test_a_hook_frame_names_its_principal() -> None:
    registry = ExtensionRegistry()
    registry.install(manifest("acme.a", hooks=["PreToolUse", "SessionStart"]))
    hooks = ExtensionHooks(timeout=0.5)
    hooks.attach(registry)
    hooks.set_principal(lambda: ADA)
    frames: list[dict[str, Any]] = []

    def send(frame: str) -> None:
        message = json.loads(frame)
        frames.append(message)
        if message["id"] is not None:
            hooks.answered("acme.a", HookAnswer(hook=message["id"], outcome="allow"))

    hooks.connect("acme.a", send)
    await hooks.ask(
        DECIDING[HookEvent.PRE_TOOL_USE], {"session_id": "s", "tool_name": "x", "tool_args": {}}
    )
    hooks.tell("SessionStart", {"session_id": "s"})

    assert [frame["principal"] for frame in frames] == [ADA.model_dump(mode="json")] * 2


# ── A token: only the signed-in person's ──────────────────────────────


@pytest.fixture
async def endpoint() -> Any:
    igni = FakeIgni()
    exchange = ServiceTokenExchange(
        api_url=lambda: API_URL,
        session=lambda: IgniSession(access_token=SESSION, org_id="org-acme"),
        transport=igni.transport(),
    )
    served = ServiceTokenEndpoint(exchange, principal=lambda: ADA)
    yield served
    await served.stop()


async def _ask(endpoint: ServiceTokenEndpoint, *, named: str | None) -> httpx.Response:
    env = await endpoint.grant("acme.notes", "https://notes.example.com")
    headers = {"Authorization": f"Bearer {env[ENV_TOKEN_SECRET]}"}
    if named is not None:
        headers[Principal.TOKEN_HEADER] = named
    async with httpx.AsyncClient(trust_env=False) as client:
        return await client.get(env[ENV_TOKEN_URL], headers=headers)


class TestTheTokenEndpoint:
    async def test_a_token_for_the_signed_in_person_is_served(
        self, endpoint: ServiceTokenEndpoint
    ) -> None:
        assert (await _ask(endpoint, named="person:u-ada")).status_code == 200

    async def test_unnamed_is_the_signed_in_person_as_before(
        self, endpoint: ServiceTokenEndpoint
    ) -> None:
        assert (await _ask(endpoint, named=None)).status_code == 200

    @pytest.mark.parametrize("named", ["person:u-grace", "app:a-ledger"])
    async def test_anybody_else_is_refused(
        self, endpoint: ServiceTokenEndpoint, named: str
    ) -> None:
        response = await _ask(endpoint, named=named)

        assert response.status_code == 403
        assert "signed in" in response.json()["error"]
