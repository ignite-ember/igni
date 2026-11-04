"""`host.directory`: the four calls, for the host's organisation only."""

from __future__ import annotations

from igni_service import IgniService
from igni_service.testing import FakeIgni, ManualClock
from igni_service.urls import IgniPaths

from .conftest import ORG, OTHER_ORG


async def test_the_organisation_is_the_one_the_host_is_for(igni: IgniService) -> None:
    organisation = await igni.host(ORG).directory.organisation()

    assert organisation.org_id == ORG
    assert organisation.name == "Acme"
    assert organisation.tenant_number == 7
    assert organisation.domains == ["acme.test"]


async def test_members_are_searched_by_prefix_and_former_ones_on_request(igni: IgniService) -> None:
    directory = igni.host(ORG).directory

    everybody = await directory.members()
    found = await directory.members(query="sa")
    with_former = await directory.members(include_former=True)

    assert [m.user_id for m in everybody] == ["u-ada", "u-max", "u-sam"]
    assert [m.user_id for m in found] == ["u-sam"]
    assert "u-gone" in {m.user_id for m in with_former}
    assert everybody[0].role == "admin"


async def test_the_limit_is_kept_within_what_igni_answers(
    igni: IgniService, fake: FakeIgni
) -> None:
    await igni.host(ORG).directory.members(limit=10_000)
    await igni.host(ORG).directory.members(limit=0)

    limits = [seen.query["limit"][0] for seen in fake.requests_to(f"{IgniPaths.DIRECTORY}/members")]
    assert limits == ["500", "1"]


async def test_one_person_present_or_former_and_none_for_a_stranger(igni: IgniService) -> None:
    directory = igni.host(ORG).directory

    present = await directory.person("u-max")
    former = await directory.person("u-gone")
    stranger = await directory.person("u-eve")

    assert present is not None and present.email == "max@acme.test"
    assert former is not None and not former.is_active
    assert stranger is None


async def test_resolve_answers_under_every_spelling_and_only_for_this_organisation(
    igni: IgniService,
) -> None:
    resolved = await igni.host(ORG).directory.resolve(
        ["MAX@acme.test", "u-sam", "u-eve", "nobody", ""]
    )

    assert set(resolved) == {"max@acme.test", "u-max", "sam@acme.test", "u-sam"}
    assert resolved["u-max"] is resolved["max@acme.test"]


async def test_resolved_people_are_kept_thirty_seconds(
    igni: IgniService, fake: FakeIgni, clock: ManualClock
) -> None:
    directory = igni.host(ORG).directory
    path = f"{IgniPaths.DIRECTORY}/people/resolve"

    await directory.resolve(["u-max"])
    await directory.resolve(["max@acme.test", "u-max"])
    assert len(fake.requests_to(path)) == 1

    clock.advance(31)
    await directory.resolve(["u-max"])
    assert len(fake.requests_to(path)) == 2


async def test_two_organisations_never_share_answers(igni: IgniService) -> None:
    ours = await igni.host(ORG).directory.resolve(["u-eve"])
    theirs = await igni.host(OTHER_ORG).directory.resolve(["u-eve"])

    assert ours == {}
    assert set(theirs) == {"u-eve", "eve@other.test"}
