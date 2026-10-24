"""igni calling the service about one person: erasure and export."""

from __future__ import annotations

import httpx

from igni_service import ExportSection, IgniService, Subject
from igni_service.testing import FakeIgni
from igni_service.urls import IgniPaths

from .conftest import ORG, bearer


async def test_an_erasure_reaches_the_handler_with_the_person_in_that_organisation(
    igni: IgniService, fake: FakeIgni, http: httpx.AsyncClient
) -> None:
    erased: list[Subject] = []

    @igni.on_erase
    async def erase(subject: Subject) -> None:
        erased.append(subject)

    response = await http.delete("/v1/igni/subjects/u-max", headers=bearer(fake.igni_token(ORG)))

    assert response.status_code == 204
    [subject] = erased
    assert (subject.user_id, subject.org_id, subject.email, subject.name) == (
        "u-max",
        ORG,
        "max@acme.test",
        "Max Planck",
    )


async def test_an_erasure_runs_by_id_alone_when_the_directory_is_away(
    igni: IgniService, fake: FakeIgni, http: httpx.AsyncClient
) -> None:
    erased: list[Subject] = []

    @igni.on_erase
    async def erase(subject: Subject) -> None:
        erased.append(subject)

    fake.failing[IgniPaths.DIRECTORY] = 503

    response = await http.delete("/v1/igni/subjects/u-max", headers=bearer(fake.igni_token(ORG)))

    assert response.status_code == 204
    assert erased == [Subject(user_id="u-max", org_id=ORG)]


async def test_an_export_answers_the_sections_the_handler_gives(
    igni: IgniService, fake: FakeIgni, http: httpx.AsyncClient
) -> None:
    @igni.on_export
    async def export(subject: Subject) -> list[ExportSection]:
        return [ExportSection(name="Documents", rows=[{"slug": "runbook", "owner": subject.email}])]

    response = await http.get(
        "/v1/igni/subjects/u-max/export", headers=bearer(fake.igni_token(ORG))
    )

    assert response.status_code == 200
    assert response.json() == {
        "sections": [{"name": "Documents", "rows": [{"slug": "runbook", "owner": "max@acme.test"}]}]
    }


async def test_only_igni_may_call_and_only_with_a_token_for_this_service(
    igni: IgniService, fake: FakeIgni, http: httpx.AsyncClient
) -> None:
    @igni.on_erase
    async def erase(subject: Subject) -> None:
        raise AssertionError("must not be reached")

    for token in (
        fake.person_token("u-ada", org_id=ORG),
        fake.igni_token(ORG, aud="http://another-service.test"),
        fake.igni_token(ORG, sub="https://not-igni.test"),
    ):
        response = await http.delete("/v1/igni/subjects/u-max", headers=bearer(token))
        assert response.status_code == 401
    assert (await http.delete("/v1/igni/subjects/u-max")).status_code == 401


async def test_a_service_without_handlers_does_not_claim_to_have_erased_anything(
    fake: FakeIgni, http: httpx.AsyncClient
) -> None:
    erase = await http.delete("/v1/igni/subjects/u-max", headers=bearer(fake.igni_token(ORG)))
    export = await http.get("/v1/igni/subjects/u-max/export", headers=bearer(fake.igni_token(ORG)))

    assert erase.status_code == 501
    assert export.status_code == 501
