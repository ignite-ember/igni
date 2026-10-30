"""A person removed from an organisation can do nothing from that moment.

igni's service-identity §21. Not for the ten minutes their access token
would otherwise live: while the service runs it follows the revocations
of every organisation it serves (``GET /v1/revocations?scope=served``,
one cursor, ``If-None-Match`` → 304) and refuses a token whose subject,
kind and organisation a watermark names and whose ``iat_ms`` is at or
before it. The check fails closed: tokens are refused until the
revocations have been read, and whenever they go unrefreshed past the
bound.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import Depends, FastAPI

from igni_service import IgniService, Person
from igni_service.revocations import RevocationFeed, RevocationsStale, Revoked
from igni_service.testing import FakeIgni, ManualClock

from .conftest import ORG, OTHER_ORG, bearer


class Feed:
    """The service's feed, running, with polls that do not wait."""

    def __init__(self, igni: IgniService) -> None:
        self.igni = igni
        self.polls = 0
        self.stop = asyncio.Event()

        async def sleep(seconds: float) -> None:
            assert seconds == RevocationFeed.POLL_SECONDS
            self.polls += 1
            await asyncio.sleep(0.001)

        igni.revocation_feed = RevocationFeed(
            igni.server,
            igni.revocation_feed._endpoint,
            igni.revocations,
            clock=igni.clock,
            sleep=sleep,
        )
        igni.verifier.revocations = igni.revocation_feed

    async def settle(self) -> None:
        """Until the feed has asked igni twice more since now."""
        start = self.polls
        for _ in range(1000):
            if self.polls > start + 1:
                return
            await asyncio.sleep(0.002)
        raise AssertionError("the feed did not poll")


@pytest.fixture
async def feed(igni: IgniService) -> AsyncIterator[Feed]:
    await igni.registration.start()
    running = Feed(igni)
    task = asyncio.create_task(igni.revocation_feed.run(running.stop))
    await asyncio.sleep(0)
    yield running
    running.stop.set()
    await task


@pytest.fixture
async def http(igni: IgniService) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()

    @app.get("/me")
    async def me(person: Person = Depends(igni.person)) -> Person:
        return person

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://service.test"
    ) as client:
        yield client


async def test_a_removed_member_is_refused_at_once(
    feed: Feed, fake: FakeIgni, http: httpx.AsyncClient, clock: ManualClock
) -> None:
    ada = fake.person_token("u-ada", org_id=ORG)
    assert (await http.get("/me", headers=bearer(ada))).status_code == 200

    clock.advance(5)
    fake.publish_revocation("u-ada", org_id=ORG)
    await feed.settle()

    assert (await http.get("/me", headers=bearer(ada))).status_code == 401
    max_ = fake.person_token("u-max", org_id=ORG)
    assert (await http.get("/me", headers=bearer(max_))).status_code == 200

    clock.advance(0.001)  # a millisecond later is after the watermark
    added_back = fake.person_token("u-ada", org_id=ORG)
    assert (await http.get("/me", headers=bearer(added_back))).status_code == 200, (
        "signed in again, after the watermark"
    )


async def test_only_in_its_organisation_unless_every_one(
    feed: Feed, fake: FakeIgni, igni: IgniService, clock: ManualClock
) -> None:
    eve_here = fake.person_token("u-eve", org_id=ORG)
    eve_there = fake.person_token("u-eve", org_id=OTHER_ORG)
    fake.publish_revocation("u-eve", org_id=OTHER_ORG)
    await feed.settle()
    with pytest.raises(Revoked):
        await igni.verifier.person(eve_there)
    assert (await igni.verifier.person(eve_here)).user_id == "u-eve"

    fake.publish_revocation("u-eve", org_id=None, reason="sessions_withdrawn")
    await feed.settle()
    with pytest.raises(Revoked):
        await igni.verifier.person(eve_here)


async def test_an_app_and_a_space_are_revoked_by_kind(
    feed: Feed, fake: FakeIgni, igni: IgniService
) -> None:
    app = fake.app_token("reporter", org_id=ORG, name="Reporter")
    space = fake.space_token("s-42", org_id=ORG, app_id="reporter", external_id="cust-9")
    fake.publish_revocation("reporter", org_id=ORG, kind="app", reason="removed")
    await feed.settle()
    with pytest.raises(Revoked):
        await igni.verifier.claims(app, use="app")
    assert (await igni.verifier.claims(space, use="space"))["sub"] == "s-42"


async def test_it_fails_closed_until_read_and_when_stale(
    fake: FakeIgni, igni: IgniService, clock: ManualClock
) -> None:
    ada = fake.person_token("u-ada", org_id=ORG)
    fake.failing["/v1/revocations"] = 503
    igni.revocations.max_staleness = 0.05
    await igni.registration.start()
    feed = Feed(igni)
    task = asyncio.create_task(igni.revocation_feed.run(feed.stop))
    await asyncio.sleep(0)
    with pytest.raises(RevocationsStale):
        await igni.verifier.person(ada)  # never read: refused

    fake.failing.clear()
    await feed.settle()
    assert (await igni.verifier.person(ada)).user_id == "u-ada"

    fake.failing["/v1/revocations"] = 503
    clock.advance(1)  # past the bound with nothing heard
    await feed.settle()
    with pytest.raises(RevocationsStale):
        await igni.verifier.person(ada)
    feed.stop.set()
    await task


async def test_a_service_that_does_not_run_the_feed_checks_nothing(
    fake: FakeIgni, igni: IgniService
) -> None:
    fake.publish_revocation("u-ada", org_id=ORG)
    ada = fake.person_token("u-ada", org_id=ORG)
    assert (await igni.verifier.person(ada)).user_id == "u-ada"


def test_the_bound_is_configurable(fake: FakeIgni) -> None:
    service = fake.service(fake.settings(revocations_max_staleness=5))
    assert service.revocations.max_staleness == 5


async def test_one_long_poll_for_every_organisation_and_a_304_when_nothing_is_new(
    feed: Feed, fake: FakeIgni, igni: IgniService
) -> None:
    await feed.settle()
    pulls = fake.requests_to("/v1/revocations")
    assert pulls and all(p.query.get("scope") == ["served"] for p in pulls)
    assert any(p.headers.get("if-none-match") for p in pulls[1:]), "asked with its cursor"
    assert {o.org_id for o in igni.revocation_feed.served} == {ORG, OTHER_ORG}


async def test_it_follows_from_start_up_with_a_token_for_no_organisation(
    feed: Feed, fake: FakeIgni
) -> None:
    await feed.settle()
    pulls = fake.requests_to("/v1/revocations") + fake.requests_to("/v1/organisations/served")
    tokens = {p.headers["authorization"].removeprefix("Bearer ") for p in pulls}
    assert tokens and all(t.startswith("revocations-") for t in tokens), "no organisation's"
    grants = [s.form() for s in fake.requests_to("/v1/oauth/token")]
    assert {"grant_type": "client_credentials", "scope": "revocations"} in [
        {k: v for k, v in g.items() if k in ("grant_type", "scope", "organisation")} for g in grants
    ]


async def test_that_token_reads_nothing_else(fake: FakeIgni, igni: IgniService) -> None:
    await igni.registration.start()
    await igni.revocation_feed.pull()
    token = igni.revocation_feed._token
    response = await igni.server.http.get(
        igni.server.url("/v1/directory/users"), headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401
