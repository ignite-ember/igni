"""The service's own tokens: one per organisation, renewed early, with fresh assertions."""

from __future__ import annotations

import jwt
import pytest

from igni_service import IgniService, IgniUnavailable
from igni_service.testing import FakeIgni, ManualClock
from igni_service.urls import IgniPaths

from .conftest import ORG, OTHER_ORG


async def test_a_token_is_kept_per_organisation(igni: IgniService, fake: FakeIgni) -> None:
    first = await igni.tokens.token(ORG)
    again = await igni.tokens.token(ORG)
    other = await igni.tokens.token(OTHER_ORG)

    assert first == again
    assert other != first
    forms = [seen.form() for seen in fake.requests_to(IgniPaths.TOKEN)]
    assert [form["organisation"] for form in forms] == [ORG, OTHER_ORG]
    assert all(form["grant_type"] == "client_credentials" for form in forms)


async def test_a_token_is_renewed_a_minute_before_it_expires(
    igni: IgniService, fake: FakeIgni, clock: ManualClock
) -> None:
    first = await igni.tokens.token(ORG)
    clock.advance(600 - 61)
    assert await igni.tokens.token(ORG) == first

    clock.advance(2)

    assert await igni.tokens.token(ORG) != first
    assert len(fake.requests_to(IgniPaths.TOKEN)) == 2


async def test_each_assertion_is_signed_by_the_service_key_for_igni_and_used_once(
    igni: IgniService, fake: FakeIgni, clock: ManualClock
) -> None:
    await igni.tokens.token(ORG)
    await igni.tokens.token(OTHER_ORG)

    assertions = [seen.form()["client_assertion"] for seen in fake.requests_to(IgniPaths.TOKEN)]
    client_id = await igni.registration.client_id()
    claims = [jwt.decode(a, options={"verify_signature": False}) for a in assertions]
    headers = [jwt.get_unverified_header(a) for a in assertions]
    assert len({c["jti"] for c in claims}) == 2
    for claim, header in zip(claims, headers, strict=True):
        assert claim["iss"] == claim["sub"] == client_id
        assert claim["aud"] == FakeIgni.ISSUER
        assert 0 < claim["exp"] - claim["iat"] <= 300
        assert header == {"alg": "RS256", "kid": await igni.thumbprint(), "typ": "JWT"}


async def test_a_token_igni_stops_honouring_is_replaced_once(
    igni: IgniService, fake: FakeIgni
) -> None:
    await igni.host(ORG).directory.organisation()
    fake.expire_service_tokens()

    organisation = await igni.host(ORG).directory.organisation()

    assert organisation.org_id == ORG
    assert len(fake.requests_to(IgniPaths.TOKEN)) == 2


async def test_a_revoked_service_gets_no_token(igni: IgniService, fake: FakeIgni) -> None:
    await igni.tokens.token(ORG)
    fake.revoke()
    igni.tokens.forget()

    with pytest.raises(IgniUnavailable, match="invalid_client"):
        await igni.tokens.token(ORG)


async def test_an_unapproved_service_is_told_so_in_igni_words(clock: ManualClock) -> None:
    fake = FakeIgni(clock=clock, require_approval=True)
    igni = fake.service()

    with pytest.raises(IgniUnavailable, match="invalid_client"):
        await igni.tokens.token(ORG)

    fake.approve()
    assert await igni.tokens.token(ORG)


async def test_a_token_names_one_organisation(igni: IgniService) -> None:
    with pytest.raises(IgniUnavailable, match="one organisation"):
        await igni.tokens.token("")
