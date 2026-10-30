"""`host.send_event`: the §15 wire, idempotent retries, and refusals."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_service import EventRefused, IgniService, Producer
from igni_service.testing import FakeIgni
from igni_service.urls import IgniPaths

from .conftest import ORG


async def test_an_event_names_its_producer_and_goes_on_the_wire_as_igni_reads_it(
    igni: IgniService, fake: FakeIgni
) -> None:
    accepted = await igni.host(ORG).send_event(
        Producer.person("u-max"), "claim.created", {"id": "CL-20431"}, event_key="claim:CL-20431"
    )

    assert accepted.event_id and not accepted.duplicate
    [seen] = fake.requests_to(IgniPaths.SERVICE_EVENTS)
    assert seen.headers["authorization"].startswith("Bearer service-")
    assert seen.json_body() == {
        "producer": {"kind": "person", "id": "u-max"},
        "type": "claim.created",
        "payload": {"id": "CL-20431"},
        "event_key": "claim:CL-20431",
    }
    [stored] = fake.events
    assert stored.org_id == ORG and stored.producer == Producer.person("u-max")


async def test_the_same_event_key_is_stored_once(igni: IgniService, fake: FakeIgni) -> None:
    host = igni.host(ORG)
    first = await host.send_event(Producer.person("u-max"), "claim.created", event_key="k1")
    again = await host.send_event(Producer.person("u-max"), "claim.created", event_key="k1")

    assert again.duplicate and again.event_id == first.event_id
    assert len(fake.events) == 1


async def test_an_app_can_be_the_producer(igni: IgniService, fake: FakeIgni) -> None:
    await igni.host(ORG).send_event(Producer.app("claims-bot"), "claim.created")

    assert fake.events[0].producer.kind == "app"


async def test_a_producer_outside_the_organisation_is_refused(
    igni: IgniService, fake: FakeIgni
) -> None:
    with pytest.raises(EventRefused) as refused:
        await igni.host(ORG).send_event(Producer.person("u-eve"), "claim.created")

    assert refused.value.status == 404
    assert fake.events == []


async def test_igni_failing_is_retried_then_reported(igni: IgniService, fake: FakeIgni) -> None:
    from igni_service import IgniUnavailable

    fake.failing[IgniPaths.SERVICE_EVENTS] = 503
    igni.events._sleep = _no_sleep  # type: ignore[attr-defined]
    with pytest.raises(IgniUnavailable):
        await igni.host(ORG).send_event(Producer.person("u-max"), "claim.created")

    assert len(fake.requests_to(IgniPaths.SERVICE_EVENTS)) == 3


def test_a_mistake_is_caught_before_it_is_sent() -> None:
    from igni_service.events import ServiceEventRequest

    with pytest.raises(ValidationError):
        ServiceEventRequest(producer=Producer.person("u-max"), type="")
    with pytest.raises(ValidationError):
        ServiceEventRequest(
            producer=Producer.person("u-max"), type="x", payload={"blob": "x" * 70_000}
        )
    with pytest.raises(ValidationError):
        Producer(kind="space", id="s1")  # type: ignore[arg-type]


async def _no_sleep(_: float) -> None:
    return None
