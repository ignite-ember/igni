"""`ext.service`: the person's token from the host, attached, kept, renewed."""

from __future__ import annotations

import httpx
import pytest
from websockets.asyncio.server import ServerConnection, serve

from igni_extension import HostUnavailable, IgniExtension, NotConfigured, NotSignedIn
from igni_extension.testing import FakeHostTokenEndpoint, ManualClock, TokenClaims


class FakeService:
    """The extension's own service: answers who the bearer is, or 401 for listed tokens."""

    def __init__(self) -> None:
        self.seen: list[str] = []
        self.refuse: set[str] = set()

    def handle(self, request: httpx.Request) -> httpx.Response:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        self.seen.append(token)
        if token in self.refuse:
            return httpx.Response(401)
        return httpx.Response(200, json={"path": request.url.path})


@pytest.fixture
def host() -> FakeHostTokenEndpoint:
    return FakeHostTokenEndpoint()


@pytest.fixture
def service() -> FakeService:
    return FakeService()


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def ext(host: FakeHostTokenEndpoint, service: FakeService, clock: ManualClock) -> IgniExtension:
    return IgniExtension(
        "acme.example",
        environ=host.environment(),
        token_transport=host.transport,
        service_transport=httpx.MockTransport(service.handle),
        clock=clock,
    )


async def test_requests_go_to_the_service_with_the_person_token(
    ext: IgniExtension, host: FakeHostTokenEndpoint, service: FakeService
) -> None:
    response = await ext.service.get("/v1/things/runbook")

    assert response.json() == {"path": "/v1/things/runbook"}
    assert service.seen == host.issued
    [asked] = host.requests
    assert asked.headers["authorization"] == f"Bearer {host.secret}"


async def test_the_token_is_kept_and_renewed_shortly_before_it_expires(
    ext: IgniExtension, host: FakeHostTokenEndpoint, clock: ManualClock
) -> None:
    for _ in range(3):
        await ext.service.get("/v1/things")
    assert len(host.issued) == 1

    clock.advance(600 - 29)
    await ext.service.get("/v1/things")

    assert len(host.issued) == 2


async def test_a_token_the_service_refuses_is_replaced_once(
    ext: IgniExtension, host: FakeHostTokenEndpoint, service: FakeService
) -> None:
    await ext.service.get("/v1/things")
    service.refuse.add(host.issued[0])

    response = await ext.service.get("/v1/things")

    assert response.status_code == 200
    assert len(host.issued) == 2
    assert service.seen[-2:] == host.issued


async def test_nobody_signed_in_is_not_signed_in(
    ext: IgniExtension, host: FakeHostTokenEndpoint
) -> None:
    host.sign_out()

    with pytest.raises(NotSignedIn):
        await ext.service.get("/v1/things")
    assert ext.tokens.signed_in is None


async def test_signing_out_is_noticed_at_the_next_renewal(
    ext: IgniExtension, host: FakeHostTokenEndpoint, clock: ManualClock
) -> None:
    await ext.service.get("/v1/things")
    host.sign_out()
    clock.advance(600)

    with pytest.raises(NotSignedIn):
        await ext.service.get("/v1/things")


async def test_who_is_signed_in_is_read_from_the_last_token(ext: IgniExtension) -> None:
    await ext.service.get("/v1/things")

    signed_in = ext.tokens.signed_in
    assert signed_in is not None
    assert (signed_in.user_id, signed_in.org_id, signed_in.email) == (
        "u-ada",
        "org-acme",
        "ada@acme.test",
    )


async def test_a_host_that_refuses_or_is_gone_is_host_unavailable(
    host: FakeHostTokenEndpoint,
) -> None:
    wrong_secret = IgniExtension(
        "acme.example",
        environ=host.environment(IGNI_SERVICE_TOKEN_SECRET="not-it"),
        token_transport=host.transport,
    )
    gone = IgniExtension(
        "acme.example",
        environ=host.environment(),
        token_transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("gone"))
        ),
    )

    with pytest.raises(HostUnavailable, match="401"):
        await wrong_secret.tokens.token()
    with pytest.raises(HostUnavailable, match="did not answer"):
        await gone.tokens.token()


async def test_a_process_igni_did_not_give_a_service_says_so() -> None:
    ext = IgniExtension("acme.example", environ={})

    with pytest.raises(NotConfigured, match="IGNI_SERVICE_URL"):
        _ = ext.service
    with pytest.raises(NotConfigured, match="IGNI_SERVICE_TOKEN_URL"):
        await ext.tokens.token()


async def test_an_anonymous_request_carries_no_token_and_asks_the_host_for_none(
    ext: IgniExtension, host: FakeHostTokenEndpoint, service: FakeService
) -> None:
    host.sign_out()  # works with nobody signed in, too

    response = await ext.service.anonymous.get("/v1/public/runbook")

    assert response.json() == {"path": "/v1/public/runbook"}
    assert service.seen == [""]
    assert host.requests == []


async def test_a_websocket_to_the_service_is_opened_as_the_person(
    host: FakeHostTokenEndpoint,
) -> None:
    seen: list[str] = []

    async def room(socket: ServerConnection) -> None:
        assert socket.request is not None
        seen.append(socket.request.headers.get("Authorization", ""))
        seen.append(socket.request.path)
        async for frame in socket:
            await socket.send(f"echo {frame!s}")

    async with serve(room, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        ext = IgniExtension(
            "acme.example",
            environ=host.environment(IGNI_SERVICE_URL=f"http://127.0.0.1:{port}/api"),
            token_transport=host.transport,
        )
        async with ext.service.websocket("/v1/rooms/runbook") as socket:
            await socket.send("hello")
            assert await socket.recv() == "echo hello"
        await ext.aclose()

    assert seen == [f"Bearer {host.issued[-1]}", "/api/v1/rooms/runbook"]


async def test_tokens_can_come_from_a_minter_the_test_supplies() -> None:
    minted: list[str] = []

    def minter(claims: TokenClaims) -> str:
        token = f"signed-for-{claims.sub}-{claims.aud}"
        minted.append(token)
        return token

    host = FakeHostTokenEndpoint(minter=minter, user_id="u-grace")
    ext = IgniExtension("acme.example", environ=host.environment(), token_transport=host.transport)

    token = await ext.tokens.token()

    assert token == f"signed-for-u-grace-{FakeHostTokenEndpoint.SERVICE_URL}"
    assert minted == host.issued == [token]
