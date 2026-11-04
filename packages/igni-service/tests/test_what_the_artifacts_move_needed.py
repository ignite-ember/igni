"""Gaps the first real service found, closed in the SDK rather than worked around.

Artifacts checks a token from a WebSocket greeting (no header), serves
public links that do more for somebody signed in, and shares one fake
igni across a test session.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from fastapi import Depends, FastAPI

from igni_service import IgniService, Person, TokenRefused
from igni_service.testing import FakeIgni


@pytest.fixture
def fake() -> FakeIgni:
    fake = FakeIgni()
    fake.add_organisation("org-1", name="Acme")
    fake.add_member("org-1", "u-1", email="ada@acme.com", name="Ada")
    return fake


@pytest.fixture
def igni(fake: FakeIgni) -> IgniService:
    return fake.service()


async def test_a_bare_token_names_its_person(fake: FakeIgni, igni: IgniService) -> None:
    person = await igni.person_of(fake.person_token("u-1", org_id="org-1"))

    assert (person.user_id, person.org_id) == ("u-1", "org-1")


async def test_a_bare_token_is_refused_without_an_http_error(igni: IgniService) -> None:
    with pytest.raises(TokenRefused):
        await igni.person_of("not-a-token")


async def test_signing_in_is_optional_but_a_bad_token_is_not(
    fake: FakeIgni, igni: IgniService
) -> None:
    app = FastAPI()

    @app.get("/read")
    async def read(person: Person | None = Depends(igni.optional_person)) -> dict[str, str]:
        return {"who": person.user_id if person else "anybody"}

    signed_in = {"Authorization": f"Bearer {fake.person_token('u-1', org_id='org-1')}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://service") as client:
        assert (await client.get("/read")).json() == {"who": "anybody"}
        assert (await client.get("/read", headers=signed_in)).json() == {"who": "u-1"}
        broken = await client.get("/read", headers={"Authorization": "Bearer broken"})
        assert broken.status_code == 401


async def test_a_token_lifetime_may_be_a_duration(fake: FakeIgni, igni: IgniService) -> None:
    token = fake.token("u-1", org="org-1", lifetime=timedelta(minutes=5))

    assert (await igni.person_of(token)).user_id == "u-1"


def test_reset_forgets_what_happened_and_keeps_who_exists(fake: FakeIgni) -> None:
    fake.failing["/v1/directory"] = 503
    fake.grants.append("client_credentials")
    fake.reset()

    assert fake.failing == {}
    assert fake.grants == []
    assert fake.deliveries == []
    assert "u-1" in fake.organisations["org-1"].members
