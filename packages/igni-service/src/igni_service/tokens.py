"""Tokens igni signed for this service, checked against igni's keys.

A token is accepted only when all of these hold — each closes a
different door:

* the header names RS256 (chosen here, never by the token) and a `kid`
  igni publishes, and the signature verifies under that key;
* `iss` is igni's issuer;
* `aud` is **this service's own URL** — a token igni minted for another
  service, or for igni itself, is not a token for this one;
* `token_use` is the kind the route takes: `user` for a person, `igni`
  for igni calling this service, and — only where the service accepts them
  (`accepted_callers`) — `app` and `space` for an app or an app's end user
  (igni's service-identity §9.2, §17); a refresh token is not an access
  token, a person is not igni, and an app is not a person;
* `exp` is in the future, and `iat` and `sub` are present.

Everything a service needs about a person is in their token. Nothing is
asked of igni per request; somebody removed from the organisation stops
getting in when their ten-minute token expires and igni will not renew it.
"""

from __future__ import annotations

from collections.abc import Collection
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import jwt
from pydantic import BaseModel, ConfigDict

from igni_service.clock import Clock
from igni_service.crypto import ALGORITHM
from igni_service.errors import IgniError
from igni_service.jwks import IgniKeys
from igni_service.server import IgniServer

if TYPE_CHECKING:
    from igni_service.revocations import RevocationFeed


class TokenUse:
    """`token_use` values a service accepts."""

    USER = "user"
    IGNI = "igni"
    #: An app acting as itself, exchanged for this service (igni's service-identity §9.2).
    APP = "app"
    #: One end user of an app, a producer of its own (§17), exchanged for this service.
    SPACE = "space"


class CallerKind(StrEnum):
    """Who a request may come from: a person, an app, or an app's space."""

    PERSON = "person"
    APP = "app"
    SPACE = "space"

    @property
    def token_use(self) -> str:
        return TokenUse.USER if self is CallerKind.PERSON else self.value


#: Everything about a token's signature, said as one thing: which check
#: failed (algorithm, key id, signature) is not for whoever holds it.
_NOT_SIGNED = "the token is not signed by igni server's keys"
_NO_ORGANISATION = "the token names no person, or no organisation"


class TokenRefused(IgniError):
    """The token is not one this service accepts, and why.

    The reason is said only in terms anybody holding the token could read
    from it themselves — expired, for another audience, the wrong kind —
    or as "not signed by igni" for everything about its signature. It goes
    in the 401's RFC 6750 challenge and the service's log, never in the
    body, which stays "Not authenticated".
    """

    def __init__(self, reason: str = "the token is not one this service accepts") -> None:
        super().__init__(reason)
        self.reason = reason


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
    def kind(self) -> CallerKind:
        return CallerKind.PERSON

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


class App(BaseModel):
    """An app acting as itself: igni's `app` token for this service."""

    model_config = ConfigDict(frozen=True)

    app_id: str
    org_id: str
    name: str = ""

    @property
    def kind(self) -> CallerKind:
        return CallerKind.APP

    @classmethod
    def from_claims(cls, claims: dict[str, Any]) -> App:
        return cls(
            app_id=str(claims.get("sub") or ""),
            org_id=str(claims.get("org") or ""),
            name=str(claims.get("name") or ""),
        )


class Space(BaseModel):
    """One end user of an app, a producer of its own: igni's `space` token for this service."""

    model_config = ConfigDict(frozen=True)

    space_id: str
    org_id: str
    #: The app the space belongs to.
    app_id: str
    #: The app's own name for its end user.
    external_id: str = ""

    @property
    def kind(self) -> CallerKind:
        return CallerKind.SPACE

    @classmethod
    def from_claims(cls, claims: dict[str, Any]) -> Space:
        return cls(
            space_id=str(claims.get("sub") or ""),
            org_id=str(claims.get("org") or ""),
            app_id=str(claims.get("app") or ""),
            external_id=str(claims.get("external_id") or ""),
        )


#: Whoever a request comes from, typed: a person, an app or an app's space.
Caller = Person | App | Space

#: What a service accepts unless it says otherwise: people only.
PEOPLE_ONLY: frozenset[CallerKind] = frozenset({CallerKind.PERSON})


class IgniCall(BaseModel):
    """igni itself calling this service, for one organisation."""

    model_config = ConfigDict(frozen=True)

    org_id: str


class TokenVerifier:
    """Checks tokens igni signed for this service."""

    #: How far in the future an `iat` may be, for clocks that disagree.
    LEEWAY = 60.0

    def __init__(
        self,
        igni: IgniServer,
        keys: IgniKeys,
        *,
        clock: Clock | None = None,
        revocations: RevocationFeed | None = None,
    ) -> None:
        self._igni = igni
        self._keys = keys
        self._clock = clock or Clock()
        #: What igni has revoked since a token was issued (`igni_service.revocations`).
        self.revocations = revocations

    async def claims(self, token: str, *, use: str | Collection[str]) -> dict[str, Any]:
        """The verified claims of a `use` token (or one of several) for this service.

        `TokenRefused` for a token that is not one; `IgniUnavailable` when
        it cannot be checked at all because igni's keys or issuer cannot
        be had.
        """
        bare = (token or "").removeprefix("Bearer ").strip()
        try:
            header = jwt.get_unverified_header(bare)
        except jwt.PyJWTError:
            raise TokenRefused("the token is not a JWT") from None
        if header.get("alg") != ALGORITHM:
            raise TokenRefused(_NOT_SIGNED)
        key = await self._keys.key(str(header.get("kid") or ""))
        if key is None:
            raise TokenRefused(_NOT_SIGNED)
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
        except jwt.InvalidAudienceError:
            raise TokenRefused(
                f"the token is for another audience; this service's is {self._igni.own_url}"
            ) from None
        except jwt.InvalidIssuerError:
            raise TokenRefused("the token was issued by somebody other than igni server") from None
        except jwt.MissingRequiredClaimError as exc:
            raise TokenRefused(f"the token has no {exc.claim!r} claim") from None
        except (jwt.PyJWTError, ValueError):
            raise TokenRefused(_NOT_SIGNED) from None
        allowed = {use} if isinstance(use, str) else set(use)
        if claims.get("token_use") not in allowed:
            raise TokenRefused(
                f"the wrong kind of token: token_use {claims.get('token_use')!r}, "
                f"and this takes {' or '.join(sorted(allowed))}"
            )
        try:
            expires, issued = float(claims["exp"]), float(claims["iat"])
        except (TypeError, ValueError):
            raise TokenRefused("the token's exp or iat is not a number") from None
        now = self._clock.wall()
        if now >= expires:
            raise TokenRefused("the token has expired")
        if issued > now + self.LEEWAY:
            raise TokenRefused("the token was issued in the future; check this machine's clock")
        if self.revocations is not None and claims.get("token_use") != TokenUse.IGNI:
            # A removed person is refused at once, not when their token
            # expires; igni's own calls are not anybody's session.
            await self.revocations.check(claims)
        return claims

    async def person(self, token: str) -> Person:
        """The person a `user` token names. `TokenRefused` for anything else."""
        person = Person.from_claims(await self.claims(token, use=TokenUse.USER))
        if not person.user_id or not person.org_id:
            # Every user token names one organisation. One that does not
            # is not one igni minted for a person.
            raise TokenRefused(_NO_ORGANISATION)
        return person

    async def caller(self, token: str, *, accept: Collection[CallerKind] = PEOPLE_ONLY) -> Caller:
        """Whoever a token names, if it is a kind in `accept`. `TokenRefused` otherwise.

        People only unless the service says more: an app's or a space's
        token is a token for this URL all the same, and a service that
        serves people must not take an app for one.
        """
        kinds = {CallerKind(kind) for kind in accept}
        claims = await self.claims(token, use={kind.token_use for kind in kinds})
        use = claims.get("token_use")
        caller: Caller
        if use == TokenUse.USER:
            caller = Person.from_claims(claims)
            if not caller.user_id or not caller.org_id:
                raise TokenRefused(_NO_ORGANISATION)
        elif use == TokenUse.APP:
            caller = App.from_claims(claims)
            if not caller.app_id or not caller.org_id:
                raise TokenRefused("the token names no app, or no organisation")
        else:
            caller = Space.from_claims(claims)
            if not caller.space_id or not caller.org_id or not caller.app_id:
                raise TokenRefused("the token names no space, app or organisation")
        return caller

    async def igni_call(self, token: str) -> IgniCall:
        """igni itself, for the organisation an `igni` token names."""
        claims = await self.claims(token, use=TokenUse.IGNI)
        org_id = str(claims.get("org") or "")
        if not org_id or claims.get("sub") != await self._igni.issuer():
            raise TokenRefused("the token is not igni's own call for an organisation")
        return IgniCall(org_id=org_id)
