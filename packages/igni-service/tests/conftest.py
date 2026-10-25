"""One fake igni per test, a service wired to it, and the app around them."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import Depends, FastAPI

from igni_service import IgniService, Person
from igni_service.testing import FakeIgni, ManualClock

ORG = "org-acme"
OTHER_ORG = "org-other"


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def fake(clock: ManualClock) -> FakeIgni:
    fake = FakeIgni(clock=clock)
    fake.add_organisation(ORG, name="Acme", slug="acme", tenant_number=7, domains=["acme.test"])
    fake.add_member(ORG, "u-ada", email="ada@acme.test", name="Ada Lovelace", role="admin")
    fake.add_member(ORG, "u-max", email="max@acme.test", name="Max Planck")
    fake.add_member(ORG, "u-sam", email="sam@acme.test", name="Sam Patel")
    fake.add_member(ORG, "u-gone", email="gone@acme.test", name="Gone Person", status="deactivated")
    fake.add_member(OTHER_ORG, "u-eve", email="eve@other.test", name="Eve")
    return fake


@pytest.fixture
def igni(fake: FakeIgni) -> IgniService:
    return fake.service()


@pytest.fixture
def app(igni: IgniService) -> FastAPI:
    app = FastAPI(lifespan=igni.lifespan)
    igni.install(app)

    @app.get("/me")
    async def me(person: Person = Depends(igni.person)) -> Person:
        return person

    return app


@pytest.fixture
async def http(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://service.test") as client:
        yield client


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
