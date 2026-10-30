"""igni telling the service that an organisation was deleted."""

from __future__ import annotations

import httpx

from igni_service import IgniService
from igni_service.testing import FakeIgni

from .conftest import ORG, OTHER_ORG, bearer


async def test_a_deleted_organisation_reaches_the_handler_with_its_id(
    igni: IgniService, fake: FakeIgni, http: httpx.AsyncClient
) -> None:
    deleted: list[str] = []

    @igni.on_organisation_deleted
    async def organisation_deleted(org_id: str) -> None:
        deleted.append(org_id)

    response = await fake.delete_organisation(ORG).send(http)

    assert response.status_code == 204
    assert deleted == [ORG]
    assert ORG not in fake.organisations


async def test_telling_it_again_is_confirmed_again(
    igni: IgniService, fake: FakeIgni, http: httpx.AsyncClient
) -> None:
    deleted: list[str] = []

    @igni.on_organisation_deleted
    async def organisation_deleted(org_id: str) -> None:
        deleted.append(org_id)

    deletion = fake.delete_organisation(ORG)
    first = await deletion.send(http)
    second = await deletion.send(http)

    assert (first.status_code, second.status_code) == (204, 204)
    assert deleted == [ORG, ORG]


async def test_a_token_for_one_organisation_cannot_delete_another(
    igni: IgniService, fake: FakeIgni, http: httpx.AsyncClient
) -> None:
    @igni.on_organisation_deleted
    async def organisation_deleted(org_id: str) -> None:
        raise AssertionError("must not be reached")

    response = await http.delete(
        f"/v1/igni/organisations/{OTHER_ORG}", headers=bearer(fake.igni_token(ORG))
    )

    assert response.status_code == 403


async def test_only_igni_may_call_and_only_with_a_token_for_this_service(
    igni: IgniService, fake: FakeIgni, http: httpx.AsyncClient
) -> None:
    @igni.on_organisation_deleted
    async def organisation_deleted(org_id: str) -> None:
        raise AssertionError("must not be reached")

    for token in (
        fake.person_token("u-ada", org_id=ORG),
        fake.igni_token(ORG, aud="http://another-service.test"),
        fake.igni_token(ORG, sub="https://not-igni.test"),
    ):
        response = await http.delete(f"/v1/igni/organisations/{ORG}", headers=bearer(token))
        assert response.status_code == 401
    assert (await http.delete(f"/v1/igni/organisations/{ORG}")).status_code == 401


async def test_a_service_without_a_handler_does_not_confirm(
    fake: FakeIgni, http: httpx.AsyncClient
) -> None:
    response = await fake.delete_organisation(ORG).send(http)

    assert response.status_code == 501
