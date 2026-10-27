"""An app's and a space's tokens for this service (igni's service-identity §9.2, §17).

A service accepts them only when it says so (`accepted_callers`); by
default it takes people only, and an app's token is refused rather than
taken for a person's. What it accepts arrives as a typed caller: a
`Person`, an `App`, or a `Space` with its app and external id.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from fastapi import Depends, FastAPI

from igni_service import App, Caller, CallerKind, IgniService, Person, Space
from igni_service.testing import FakeIgni
from igni_service.tokens import TokenRefused

from .conftest import ORG, bearer

EVERYBODY = frozenset(CallerKind)


async def _client(service: IgniService) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI(lifespan=service.lifespan)
    service.install(app)

    @app.get("/who")
    async def who(caller: Caller = Depends(service.caller)) -> dict[str, str]:
        return {"kind": caller.kind.value, **{k: str(v) for k, v in caller.model_dump().items()}}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://service.test") as client:
        yield client


@pytest.fixture
async def everybody(fake: FakeIgni) -> AsyncIterator[httpx.AsyncClient]:
    async for client in _client(fake.service(accepted_callers=EVERYBODY)):
        yield client


@pytest.fixture
async def people_only(fake: FakeIgni) -> AsyncIterator[httpx.AsyncClient]:
    async for client in _client(fake.service()):
        yield client


async def test_an_app_and_a_space_arrive_typed(
    everybody: httpx.AsyncClient, fake: FakeIgni
) -> None:
    app = await everybody.get(
        "/who", headers=bearer(fake.app_token("reporter", org_id=ORG, name="Reporter"))
    )
    assert app.json() == {"kind": "app", "app_id": "reporter", "org_id": ORG, "name": "Reporter"}
    space = await everybody.get(
        "/who",
        headers=bearer(
            fake.space_token("sp-1", org_id=ORG, app_id="reporter", external_id="cust-9")
        ),
    )
    assert space.json() == {
        "kind": "space",
        "space_id": "sp-1",
        "org_id": ORG,
        "app_id": "reporter",
        "external_id": "cust-9",
    }
    person = await everybody.get("/who", headers=bearer(fake.person_token("u-ada", org_id=ORG)))
    assert person.json()["kind"] == "person" and person.json()["user_id"] == "u-ada"


async def test_by_default_a_service_takes_people_only(
    people_only: httpx.AsyncClient, fake: FakeIgni
) -> None:
    app = await people_only.get("/who", headers=bearer(fake.app_token("reporter", org_id=ORG)))
    space = await people_only.get(
        "/who",
        headers=bearer(fake.space_token("sp-1", org_id=ORG, app_id="r", external_id="c")),
    )
    person = await people_only.get("/who", headers=bearer(fake.person_token("u-ada", org_id=ORG)))
    assert (app.status_code, space.status_code, person.status_code) == (401, 401, 200)


async def test_an_app_cannot_pass_for_a_person(fake: FakeIgni) -> None:
    service = fake.service(accepted_callers=EVERYBODY)
    with pytest.raises(TokenRefused):
        await service.person_of(fake.app_token("reporter", org_id=ORG))
    with pytest.raises(TokenRefused):
        await service.verifier.caller(
            fake.app_token("reporter", org_id=ORG), accept={CallerKind.SPACE}
        )


@pytest.mark.parametrize(
    "token",
    [
        lambda f: f.app_token("reporter", org_id=ORG, aud="http://another-service.test"),
        lambda f: f.app_token("reporter", org_id=""),
        lambda f: f.space_token("sp-1", org_id=ORG, app_id="", external_id="c"),
        lambda f: f.space_token(
            "sp-1", org_id=ORG, app_id="r", external_id="c", aud=FakeIgni.ISSUER
        ),
    ],
    ids=["other-service", "no-org", "space-without-app", "igni-audience"],
)
async def test_what_an_app_or_space_token_must_hold(
    fake: FakeIgni, token: Callable[[FakeIgni], str]
) -> None:
    service = fake.service(accepted_callers=EVERYBODY)
    with pytest.raises(TokenRefused):
        await service.caller_of(token(fake))


def test_the_kinds_are_typed() -> None:
    assert isinstance(Person(user_id="u", org_id="o"), Person)
    assert App(app_id="a", org_id="o").kind is CallerKind.APP
    assert Space(space_id="s", org_id="o", app_id="a").kind is CallerKind.SPACE
