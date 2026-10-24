"""A person's token: every check §4 asks for, and what happens when igni is away."""

from __future__ import annotations

import httpx
import pytest
from fastapi import Depends, FastAPI

from igni_service import IgniService, Person
from igni_service.crypto import RsaKeyPair
from igni_service.testing import FakeIgni, ManualClock
from igni_service.tokens import TokenRefused
from igni_service.urls import IgniPaths

from .conftest import ORG, OTHER_ORG, bearer


async def test_a_person_token_names_the_person_and_their_organisation(
    http: httpx.AsyncClient, fake: FakeIgni
) -> None:
    response = await http.get("/me", headers=bearer(fake.person_token("u-ada", org_id=ORG)))

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "u-ada",
        "org_id": ORG,
        "org_name": "Acme",
        "org_role": "admin",
        "email": "ada@acme.test",
        "name": "Ada Lovelace",
        "super_admin": False,
    }


async def test_no_token_is_refused_with_a_bearer_challenge(http: httpx.AsyncClient) -> None:
    response = await http.get("/me")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "http://another-service.test"},
        {"aud": FakeIgni.ISSUER},
        {"iss": "https://not-igni.test"},
        {"use": "refresh"},
        {"use": "session"},
        {"use": "service"},
        {"use": "igni"},
        {"org": ""},
    ],
    ids=[
        "other-service",
        "igni-audience",
        "other-issuer",
        "refresh",
        "session",
        "service",
        "igni",
        "no-org",
    ],
)
async def test_a_token_that_is_not_for_this_service_or_not_a_person_is_refused(
    http: httpx.AsyncClient, fake: FakeIgni, overrides: dict[str, str]
) -> None:
    token = fake.person_token("u-ada", org_id=ORG, **overrides)

    response = await http.get("/me", headers=bearer(token))

    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}


async def test_an_expired_token_is_refused(
    http: httpx.AsyncClient, fake: FakeIgni, clock: ManualClock
) -> None:
    token = fake.person_token("u-ada", org_id=ORG)
    assert (await http.get("/me", headers=bearer(token))).status_code == 200

    clock.advance(601)

    assert (await http.get("/me", headers=bearer(token))).status_code == 401


async def test_a_token_signed_by_a_key_igni_does_not_publish_is_refused(
    http: httpx.AsyncClient, fake: FakeIgni
) -> None:
    impostor = RsaKeyPair.generate()

    same_kid = fake.person_token("u-ada", org_id=ORG, key=impostor)
    own_kid = fake.person_token("u-ada", org_id=ORG, key=impostor, kid="made-up")

    assert (await http.get("/me", headers=bearer(same_kid))).status_code == 401
    assert (await http.get("/me", headers=bearer(own_kid))).status_code == 401


async def test_the_algorithm_is_chosen_here_not_by_the_token(http: httpx.AsyncClient) -> None:
    hs256 = (
        "eyJhbGciOiJIUzI1NiIsImtpZCI6Imlnbmkta2V5LTEifQ."
        "eyJzdWIiOiJ1LWFkYSIsIm9yZyI6Im9yZy1hY21lIn0."
        "c2lnbmF0dXJl"
    )
    unsigned = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ1LWFkYSJ9."

    assert (await http.get("/me", headers=bearer(hs256))).status_code == 401
    assert (await http.get("/me", headers=bearer(unsigned))).status_code == 401
    assert (await http.get("/me", headers=bearer("not a jwt"))).status_code == 401


async def test_igni_keys_are_kept_for_max_age(
    http: httpx.AsyncClient, fake: FakeIgni, clock: ManualClock
) -> None:
    for _ in range(3):
        token = fake.person_token("u-ada", org_id=ORG)
        assert (await http.get("/me", headers=bearer(token))).status_code == 200
    assert len(fake.requests_to(IgniPaths.JWKS)) == 1

    clock.advance(fake.max_age + 1)
    await http.get("/me", headers=bearer(fake.person_token("u-ada", org_id=ORG)))

    assert len(fake.requests_to(IgniPaths.JWKS)) == 2


async def test_a_new_key_is_fetched_at_once_when_a_token_names_it(
    http: httpx.AsyncClient, fake: FakeIgni
) -> None:
    await http.get("/me", headers=bearer(fake.person_token("u-ada", org_id=ORG)))
    fake.rotate()

    response = await http.get("/me", headers=bearer(fake.person_token("u-ada", org_id=ORG)))

    assert response.status_code == 200
    assert len(fake.requests_to(IgniPaths.JWKS)) == 2


async def test_made_up_key_ids_do_not_become_a_stream_of_requests_to_igni(
    http: httpx.AsyncClient, fake: FakeIgni, clock: ManualClock
) -> None:
    await http.get("/me", headers=bearer(fake.person_token("u-ada", org_id=ORG)))
    for n in range(5):
        token = fake.person_token("u-ada", org_id=ORG, kid=f"made-up-{n}")
        assert (await http.get("/me", headers=bearer(token))).status_code == 401
    assert len(fake.requests_to(IgniPaths.JWKS)) == 2

    clock.advance(31)
    await http.get("/me", headers=bearer(fake.person_token("u-ada", org_id=ORG, kid="again")))

    assert len(fake.requests_to(IgniPaths.JWKS)) == 3


async def test_a_token_that_cannot_be_checked_is_503_not_401(
    http: httpx.AsyncClient, fake: FakeIgni
) -> None:
    fake.failing[IgniPaths.JWKS] = 500

    response = await http.get("/me", headers=bearer(fake.person_token("u-ada", org_id=ORG)))

    assert response.status_code == 503


async def test_cached_keys_keep_answering_while_igni_is_away(
    http: httpx.AsyncClient, fake: FakeIgni, clock: ManualClock
) -> None:
    await http.get("/me", headers=bearer(fake.person_token("u-ada", org_id=ORG)))
    fake.failing[IgniPaths.JWKS] = 500
    clock.advance(fake.max_age + 1)

    response = await http.get("/me", headers=bearer(fake.person_token("u-ada", org_id=ORG)))

    assert response.status_code == 200


async def test_the_audience_is_the_service_url_as_igni_normalises_it(fake: FakeIgni) -> None:
    igni = fake.service(fake.settings(public_base_url="HTTP://Service.TEST/"))

    person = await igni.verifier.person(fake.person_token("u-max", org_id=ORG))

    assert person.user_id == "u-max"
    assert igni.server.own_url == "http://service.test"


async def test_igni_calling_is_an_igni_token_naming_igni_as_its_subject(
    igni: IgniService, fake: FakeIgni
) -> None:
    call = await igni.verifier.igni_call(fake.igni_token(ORG))
    assert call.org_id == ORG

    with pytest.raises(TokenRefused):
        await igni.verifier.igni_call(fake.igni_token(ORG, sub="someone-else"))
    with pytest.raises(TokenRefused):
        await igni.verifier.igni_call(fake.person_token("u-ada", org_id=ORG))
    with pytest.raises(TokenRefused):
        await igni.verifier.igni_call(fake.igni_token(""))


async def test_org_admin_is_an_admin_of_the_organisation_the_token_is_for(
    igni: IgniService, fake: FakeIgni
) -> None:
    fake.add_member(ORG, "u-root", email="root@acme.test", super_admin=True)
    fake.add_member(OTHER_ORG, "u-max", role="admin")
    app = FastAPI()

    @app.get("/admin")
    async def admin(person: Person = Depends(igni.org_admin)) -> dict[str, str]:
        return {"admin": person.user_id, "org": person.org_id}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://service.test"
    ) as http:
        ada = await http.get("/admin", headers=bearer(fake.person_token("u-ada", org_id=ORG)))
        max_here = await http.get("/admin", headers=bearer(fake.person_token("u-max", org_id=ORG)))
        max_there = await http.get(
            "/admin", headers=bearer(fake.person_token("u-max", org_id=OTHER_ORG))
        )
        root = await http.get("/admin", headers=bearer(fake.person_token("u-root", org_id=ORG)))
        nobody = await http.get("/admin")

    assert ada.json() == {"admin": "u-ada", "org": ORG}
    assert max_here.status_code == 403, "a member of this organisation is not its admin"
    assert max_there.json() == {"admin": "u-max", "org": OTHER_ORG}
    assert root.status_code == 403, "a super admin is not every organisation's admin"
    assert nobody.status_code == 401
