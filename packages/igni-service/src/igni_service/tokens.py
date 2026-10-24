"""Tokens igni signed for this service, checked against igni's keys.

A token is accepted only when all of these hold — each closes a
different door:

* the header names RS256 (chosen here, never by the token) and a `kid`
  igni publishes, and the signature verifies under that key;
* `iss` is igni's issuer;
* `aud` is **this service's own URL** — a token igni minted for another
  service, or for igni itself, is not a token for this one;
* `token_use` is the kind the route takes: `user` for a person, `igni`
  for igni calling this service — a refresh token is not an access token,
  and a person is not igni;
* `exp` is in the future, and `iat` and `sub` are present.

Everything a service needs about a person is in their token. Nothing is
asked of igni per request; somebody removed from the organisation stops
getting in when their ten-minute token expires and igni will not renew it.
"""

from __future__ import annotations

from typing import Any

import jwt
from pydantic import BaseModel, ConfigDict

from igni_service.clock import Clock
from igni_service.crypto import ALGORITHM
from igni_service.errors import IgniError
from igni_service.jwks import IgniKeys
from igni_service.server import IgniServer


class TokenUse:
    """`token_use` values a service accepts."""

    USER = "user"
    IGNI = "igni"


class TokenRefused(IgniError):
    """The token is not one this service accepts. Deliberately says no more."""


class Person(BaseModel):
    """A person, as their token describes them, in the one organisation it names."""

    model_config = ConfigDict(frozen=True)

    user_id: str
    org_id: str
    org_name: str = ""
    #: `admin` or `member` in `org_id`.
    org_role: str = "member"
    email: str = ""
    name: str = ""
    super_admin: bool = False

    @property
    def is_org_admin(self) -> bool:
        return self.org_role == "admin"

    @property
    def display_name(self) -> str:
        return self.name or self.email

    @classmethod
    def from_claims(cls, claims: dict[str, Any]) -> Person:
        return cls(
            user_id=str(claims.get("sub") or ""),
            org_id=str(claims.get("org") or ""),
            org_name=str(claims.get("org_name") or ""),
            org_role=str(claims.get("org_role") or "member"),
            email=str(claims.get("email") or ""),
            name=str(claims.get("name") or ""),
            super_admin=claims.get("super_admin") is True,
        )


class IgniCall(BaseModel):
    """igni itself calling this service, for one organisation."""

    model_config = ConfigDict(frozen=True)

    org_id: str


class TokenVerifier:
    """Checks tokens igni signed for this service."""

    #: How far in the future an `iat` may be, for clocks that disagree.
    LEEWAY = 60.0

    def __init__(self, igni: IgniServer, keys: IgniKeys, *, clock: Clock | None = None) -> None:
        self._igni = igni
        self._keys = keys
        self._clock = clock or Clock()

    async def claims(self, token: str, *, use: str) -> dict[str, Any]:
        """The verified claims of a `use` token for this service.

        `TokenRefused` for a token that is not one; `IgniUnavailable` when
        it cannot be checked at all because igni's keys or issuer cannot
        be had.
        """
        bare = (token or "").removeprefix("Bearer ").strip()
        try:
            header = jwt.get_unverified_header(bare)
        except jwt.PyJWTError:
            raise TokenRefused() from None
        if header.get("alg") != ALGORITHM:
            raise TokenRefused()
        key = await self._keys.key(str(header.get("kid") or ""))
        if key is None:
            raise TokenRefused()
        issuer = await self._igni.issuer()
        try:
            claims: dict[str, Any] = jwt.decode(
                bare,
                key.public_key(),
                algorithms=[ALGORITHM],
                audience=self._igni.own_url,
                issuer=issuer,
                # `exp` and `iat` are compared below against this object's
                # clock, so the moment a token stops working is testable
                # without waiting for it.
                options={
                    "require": ["exp", "iat", "sub", "iss", "aud"],
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
        except (jwt.PyJWTError, ValueError):
            raise TokenRefused() from None
        if claims.get("token_use") != use:
            raise TokenRefused()
        try:
            expires, issued = float(claims["exp"]), float(claims["iat"])
        except (TypeError, ValueError):
            raise TokenRefused() from None
        now = self._clock.wall()
        if now >= expires or issued > now + self.LEEWAY:
            raise TokenRefused()
        return claims

    async def person(self, token: str) -> Person:
        """The person a `user` token names. `TokenRefused` for anything else."""
        person = Person.from_claims(await self.claims(token, use=TokenUse.USER))
        if not person.user_id or not person.org_id:
            # Every user token names one organisation. One that does not
            # is not one igni minted for a person.
            raise TokenRefused()
        return person

    async def igni_call(self, token: str) -> IgniCall:
        """igni itself, for the organisation an `igni` token names."""
        claims = await self.claims(token, use=TokenUse.IGNI)
        org_id = str(claims.get("org") or "")
        if not org_id or claims.get("sub") != await self._igni.issuer():
            raise TokenRefused()
        return IgniCall(org_id=org_id)
