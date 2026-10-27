"""A person removed from an organisation can do nothing from that moment.

Not for the ten minutes their access token would otherwise live: the
service keeps igni's revocation list (``GET /v1/platform/revocations``)
and refuses any token whose session, producer or incarnation it names.
The list fails closed: once it cannot be refreshed within its bound,
nothing is accepted until it can be again.
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
    """The service's feed, run until a test is done, polling without waiting."""

    def __init__(self, igni: IgniService) -> None:
        self.igni = igni
        self.polls = 0
        self.stop = asyncio.Event()

        async def sleep(seconds: float) -> None:
            assert seconds == RevocationFeed.POLL_SECONDS, "igni answered at once: poll"
            self.polls += 1
            await asyncio.sleep(0.001)

        igni.revocation_feed = RevocationFeed(
            igni.server,
            igni.revocation_feed._endpoint,
            igni.revocations,
            sleep=sleep,
        )

    async def settle(self) -> None:
        """Until the feed has asked igni again since now."""
        start = self.polls
        for _ in range(500):
            if self.polls > start + 1:
                return
            await asyncio.sleep(0.002)
        raise AssertionError("the feed did not poll")


@pytest.fixture
async def feed(igni: IgniService) -> AsyncIterator[Feed]:
    await igni.registration.start()
    running = Feed(igni)
    task = asyncio.create_task(igni.revocation_feed.run(running.stop))
    await running.settle()
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
    feed: Feed, fake: FakeIgni, http: httpx.AsyncClient
) -> None:
    ada = fake.person_token("u-ada", org_id=ORG)
    assert (await http.get("/me", headers=bearer(ada))).status_code == 200

    fake.remove_producer("person:u-ada", org_id=ORG)
    await feed.settle()

    assert (await http.get("/me", headers=bearer(ada))).status_code == 401
    other = fake.person_token("u-max", org_id=ORG)
    assert (await http.get("/me", headers=bearer(other))).status_code == 200


async def test_only_in_the_organisation_they_were_removed_from(
    feed: Feed, fake: FakeIgni, igni: IgniService
) -> None:
    fake.remove_producer("person:u-eve", org_id=ORG)
    await feed.settle()
    eve_elsewhere = fake.person_token("u-eve", org_id=OTHER_ORG)
    assert (await igni.verifier.person(eve_elsewhere)).user_id == "u-eve"


async def test_a_revoked_session_and_incarnation(
    feed: Feed, fake: FakeIgni, igni: IgniService
) -> None:
    signed_in = fake.person_token("u-max", org_id=ORG, sid="s-1")
    again = fake.person_token("u-max", org_id=ORG, sid="s-2")
    old_life = fake.app_token("reporter", org_id=ORG, incarnation="i-1")
    fake.revoke_session("s-1")
    fake.revoke_incarnation("i-1")
    await feed.settle()

    with pytest.raises(Revoked):
        await igni.verifier.person(signed_in)
    assert (await igni.verifier.person(again)).user_id == "u-max", "another sign-in is untouched"
    with pytest.raises(Revoked):
        await igni.verifier.claims(old_life, use="app")


async def test_it_fails_closed_when_it_cannot_be_refreshed(
    fake: FakeIgni, igni: IgniService, clock: ManualClock, http: httpx.AsyncClient
) -> None:
    ada = fake.person_token("u-ada", org_id=ORG)
    igni.revocations.arm()  # a feed keeps it — and igni has not answered since
    assert (await http.get("/me", headers=bearer(ada))).status_code == 200, "within its bound"

    clock.advance(igni.revocations.max_staleness + 1)
    with pytest.raises(RevocationsStale):
        await igni.verifier.person(ada)
    assert (await http.get("/me", headers=bearer(ada))).status_code == 503

    await igni.registration.start()
    igni.revocations.heard(await igni.revocation_feed.pull())
    assert (await http.get("/me", headers=bearer(ada))).status_code == 200, "fresh again"


async def test_while_igni_fails_it_keeps_polling(feed: Feed, fake: FakeIgni) -> None:
    fake.failing["/v1/platform/revocations"] = 503
    before = feed.polls
    await feed.settle()
    assert feed.polls > before, "every two seconds, while igni fails"
    fake.failing.clear()
    fake.remove_producer("person:u-sam", org_id=ORG)
    await feed.settle()
    assert len(feed.igni.revocations) == 1


async def test_a_service_that_does_not_keep_the_list_checks_nothing(
    fake: FakeIgni, igni: IgniService, clock: ManualClock
) -> None:
    clock.advance(3600)
    ada = fake.person_token("u-ada", org_id=ORG)
    assert (await igni.verifier.person(ada)).user_id == "u-ada"


def test_the_bound_is_configurable(fake: FakeIgni) -> None:
    service = fake.service(fake.settings(revocations_max_staleness=5))
    assert service.revocations.max_staleness == 5
