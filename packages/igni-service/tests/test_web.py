"""Signing in to the service's own pages through igni: code flow, PKCE, sessions, renewal."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from igni_service import IgniService, KeyCipher, Person, SqlWebSessionStore, WebSession
from igni_service.tables import IgniTables
from igni_service.testing import FakeIgni, ManualClock
from igni_service.urls import IgniPaths
from igni_service.web import Pkce, ReturnTo
from igni_service.web_sessions import LoginAttempt

from .conftest import ORG

SERVICE = "https://service.test"
PAGE = {"Accept": "text/html,application/xhtml+xml"}


@pytest.fixture
def igni(fake: FakeIgni) -> IgniService:
    fake.add_member(ORG, "u-root", email="root@acme.test", name="Root", super_admin=True)
    return fake.service(fake.settings(public_base_url=SERVICE))


@pytest.fixture
def web_app(igni: IgniService) -> FastAPI:
    app = FastAPI()
    app.include_router(igni.web.router)

    @app.get("/settings")
    async def settings(person: Person = Depends(igni.web.person)) -> Person:
        return person

    @app.get("/admin")
    async def admin(person: Person = Depends(igni.web.super_admin)) -> dict[str, str]:
        return {"admin": person.user_id}

    return app


@pytest.fixture
async def browser(web_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=web_app)
    async with httpx.AsyncClient(transport=transport, base_url=SERVICE) as client:
        yield client


async def sign_in(
    browser: httpx.AsyncClient, fake: FakeIgni, *, user_id: str = "u-max", return_to: str = "/"
) -> httpx.Response:
    login = await browser.get("/auth/login", params={"return_to": return_to})
    assert login.status_code == 303
    back = fake.authorize(login.headers["location"], user_id=user_id, org_id=ORG)
    return await browser.get(back)


async def test_login_sends_the_browser_to_igni_with_pkce_and_a_state(
    browser: httpx.AsyncClient, igni: IgniService, fake: FakeIgni
) -> None:
    login = await browser.get("/auth/login")

    location = urlsplit(login.headers["location"])
    query = {k: v[0] for k, v in parse_qs(location.query).items()}
    assert f"{location.scheme}://{location.netloc}{location.path}" == fake.authorization_endpoint
    assert query["response_type"] == "code"
    assert query["client_id"] == await igni.registration.client_id()
    assert query["redirect_uri"] == f"{SERVICE}/auth/callback"
    assert query["code_challenge_method"] == "S256"
    assert len(query["code_challenge"]) == 43
    assert browser.cookies.get("igni_login") == query["state"]
    cookie = login.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie


async def test_the_callback_opens_a_session_and_the_browser_never_holds_a_token(
    browser: httpx.AsyncClient, fake: FakeIgni
) -> None:
    back = await sign_in(browser, fake, return_to="/settings?tab=people")

    assert back.status_code == 303
    assert back.headers["location"] == "/settings?tab=people"
    cookie = back.headers["set-cookie"]
    assert "igni_session=" in cookie
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    assert "eyJ" not in cookie and "refresh-" not in cookie

    page = await browser.get("/settings")
    assert page.status_code == 200
    assert page.json()["user_id"] == "u-max"
    assert page.json()["org_id"] == ORG
    assert "eyJ" not in page.text


async def test_the_registration_names_the_callback_without_being_told(
    igni: IgniService, fake: FakeIgni
) -> None:
    await igni.registration.register()

    [body] = [seen.json_body() for seen in fake.requests_to(IgniPaths.CLIENTS)]
    assert body["redirect_uris"] == [f"{SERVICE}/auth/callback"]


@pytest.mark.parametrize(
    "return_to",
    ["//evil.test/", "https://evil.test/", "/\\evil.test", "evil", "javascript:alert(1)", ""],
)
async def test_return_to_is_only_ever_a_path_here(
    browser: httpx.AsyncClient, fake: FakeIgni, return_to: str
) -> None:
    back = await sign_in(browser, fake, return_to=return_to)

    assert back.headers["location"] == "/"


def test_return_to_keeps_a_path_and_its_query() -> None:
    assert ReturnTo.safe("/docs/a?b=c#d") == "/docs/a?b=c#d"


async def test_a_callback_this_browser_did_not_start_is_refused(
    browser: httpx.AsyncClient, fake: FakeIgni, web_app: FastAPI
) -> None:
    login = await browser.get("/auth/login")
    back = fake.authorize(login.headers["location"], user_id="u-max", org_id=ORG)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web_app), base_url=SERVICE
    ) as stranger:
        refused = await stranger.get(back)

    assert refused.status_code == 400
    assert "igni_session" not in refused.headers.get("set-cookie", "")


async def test_a_code_and_a_state_work_once(browser: httpx.AsyncClient, fake: FakeIgni) -> None:
    login = await browser.get("/auth/login")
    back = fake.authorize(login.headers["location"], user_id="u-max", org_id=ORG)
    state = parse_qs(urlsplit(back).query)["state"][0]

    assert (await browser.get(back)).status_code == 303
    browser.cookies.set("igni_login", state, domain="service.test", path="/auth")
    assert (await browser.get(back)).status_code == 400


async def test_an_error_from_igni_is_said_plainly(browser: httpx.AsyncClient) -> None:
    response = await browser.get("/auth/callback", params={"error": "access_denied"})

    assert response.status_code == 400
    assert "access_denied" in response.text


async def test_a_page_sends_a_stranger_to_sign_in_and_an_api_call_gets_401(
    browser: httpx.AsyncClient,
) -> None:
    page = await browser.get("/settings?tab=people", headers=PAGE)
    api = await browser.get("/settings")

    assert page.status_code == 303
    assert page.headers["location"] == "/auth/login?return_to=%2Fsettings%3Ftab%3Dpeople"
    assert api.status_code == 401


async def test_the_session_is_renewed_before_the_token_runs_out(
    browser: httpx.AsyncClient, fake: FakeIgni, clock: ManualClock
) -> None:
    await sign_in(browser, fake)
    clock.advance(600 - 59)

    page = await browser.get("/settings")

    assert page.status_code == 200
    assert fake.grants == ["authorization_code", "refresh_token"]
    assert (await browser.get("/settings")).status_code == 200
    assert fake.grants == ["authorization_code", "refresh_token"]


async def test_igni_refusing_the_renewal_signs_the_person_out(
    browser: httpx.AsyncClient, fake: FakeIgni, clock: ManualClock
) -> None:
    await sign_in(browser, fake)
    fake.deactivate(ORG, "u-max")
    clock.advance(600)

    assert (await browser.get("/settings")).status_code == 401
    assert (await browser.get("/settings")).status_code == 401
    assert fake.grants == ["authorization_code", "refresh_token"]


async def test_a_session_outlives_igni_being_briefly_away(
    browser: httpx.AsyncClient, fake: FakeIgni, clock: ManualClock
) -> None:
    await sign_in(browser, fake)
    fake.failing[IgniPaths.TOKEN] = 503
    clock.advance(600 - 30)

    assert (await browser.get("/settings")).status_code == 200

    clock.advance(60)
    assert (await browser.get("/settings")).status_code == 503


async def test_admin_pages_are_for_super_admins(
    browser: httpx.AsyncClient, fake: FakeIgni, web_app: FastAPI
) -> None:
    await sign_in(browser, fake, user_id="u-max")
    assert (await browser.get("/admin")).status_code == 403

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web_app), base_url=SERVICE
    ) as root:
        await sign_in(root, fake, user_id="u-root")
        assert (await root.get("/admin")).json() == {"admin": "u-root"}


async def test_logout_ends_the_session(browser: httpx.AsyncClient, fake: FakeIgni) -> None:
    await sign_in(browser, fake)
    session_id = browser.cookies["igni_session"]

    out = await browser.post("/auth/logout")

    assert out.status_code == 303
    browser.cookies.set("igni_session", session_id, domain="service.test")
    assert (await browser.get("/settings")).status_code == 401


async def test_plain_http_in_development_drops_secure(fake: FakeIgni) -> None:
    igni = fake.service(fake.settings(public_base_url="http://service.localhost:8100"))
    app = FastAPI()
    app.include_router(igni.web.router)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://service.localhost:8100"
    ) as browser:
        login = await browser.get("/auth/login")

    assert "secure" not in login.headers["set-cookie"].lower()


def test_pkce_is_s256_of_the_verifier() -> None:
    # RFC 7636 appendix B.
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert Pkce.challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


async def test_the_sql_store_keeps_sessions_sealed_and_logins_single_use(
    tmp_path: Path, clock: ManualClock
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'web.db'}")
    store = SqlWebSessionStore(engine, KeyCipher(KeyCipher.generate()), clock=clock)
    session = WebSession(
        person=Person(user_id="u-max", org_id=ORG),
        access_token="eyJ-access",
        refresh_token="refresh-1",
        access_expires_at=clock.wall() + 600,
        expires_at=clock.wall() + 3600,
    )
    try:
        await store.put("cookie-value", session)
        assert await store.get("cookie-value") == session
        renewed = session.model_copy(update={"refresh_token": "refresh-2"})
        await store.put("cookie-value", renewed)
        assert await store.get("cookie-value") == renewed
        async with engine.connect() as connection:
            rows = (await connection.execute(select(IgniTables.web_session))).all()
        assert len(rows) == 1
        assert "cookie-value" not in rows[0].id_hash
        assert "eyJ-access" not in rows[0].sealed and "refresh-2" not in rows[0].sealed

        await store.put_login(
            LoginAttempt(state="s1", verifier="v1", return_to="/", expires_at=clock.wall() + 600)
        )
        assert (await store.take_login("s1")) is not None
        assert await store.take_login("s1") is None

        clock.advance(3601)
        assert await store.get("cookie-value") is None
        await store.delete("cookie-value")
    finally:
        await engine.dispose()
