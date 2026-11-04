"""People, asked of igni's directory.

A service stores no user rows. Who the *caller* is arrives in their token
(`Person`); everybody else — a colleague to mention, a document's owner,
the people a notification names — is asked of igni's directory, with a
service token for the organisation the question is about. The
organisation is never a parameter igni reads: a token for one
organisation answers for that one, so nothing here can ask about anybody
outside it.

`resolve` is the hot call, so its answers are kept thirty seconds per
organisation: long enough that a room of people joining is one round
trip, short enough that a changed name shows soon. It is not an
authorisation cache.
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from igni_service.clock import Clock
from igni_service.errors import IgniUnavailable
from igni_service.service_tokens import ServiceTokens
from igni_service.urls import IgniPaths


class Member(BaseModel):
    """One member of an organisation, present or former, as the directory describes them."""

    model_config = ConfigDict(frozen=True)

    user_id: str
    email: str = ""
    name: str = ""
    #: `admin` or `member`.
    role: str = "member"
    #: `active`, or `deactivated` for somebody who has left.
    status: str = "active"
    super_admin: bool = False

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def display_name(self) -> str:
        return self.name or self.email


class Members(BaseModel):
    people: list[Member] = Field(default_factory=list)


class Organisation(BaseModel):
    """The organisation a token names."""

    model_config = ConfigDict(frozen=True)

    org_id: str
    name: str = ""
    slug: str = ""
    #: Derive per-organisation databases and index names from this.
    tenant_number: int = 0
    domains: list[str] = Field(default_factory=list)


class ResolveRequest(BaseModel):
    identities: list[str]


class CachedMember(BaseModel):
    stamped: float
    member: Member


class DirectoryClient:
    """igni's directory for every organisation, with the `resolve` cache."""

    #: How long a resolved member is reused, in seconds.
    TTL = 30.0
    #: The most igni answers in one call.
    MAX_PEOPLE = 500

    def __init__(self, tokens: ServiceTokens, *, clock: Clock | None = None) -> None:
        self._tokens = tokens
        self._clock = clock or Clock()
        #: `(org_id, identity casefolded)` → cached member.
        self._cache: dict[tuple[str, str], CachedMember] = {}

    def forget(self) -> None:
        self._cache.clear()

    async def organisation(self, org_id: str) -> Organisation:
        response = await self._tokens.request(
            "GET", f"{IgniPaths.DIRECTORY}/organisation", org_id=org_id
        )
        return self._parsed(response, Organisation)

    async def members(
        self, org_id: str, *, query: str = "", limit: int = 100, include_former: bool = False
    ) -> list[Member]:
        response = await self._tokens.request(
            "GET",
            f"{IgniPaths.DIRECTORY}/members",
            org_id=org_id,
            params={
                "query": query,
                "limit": max(1, min(limit, self.MAX_PEOPLE)),
                "include_former": "true" if include_former else "false",
            },
        )
        return self._parsed(response, Members).people

    async def person(self, org_id: str, user_id: str) -> Member | None:
        response = await self._tokens.request(
            "GET", f"{IgniPaths.DIRECTORY}/people/{user_id}", org_id=org_id
        )
        if response.status_code == 404:
            return None
        member = self._parsed(response, Member)
        self._remember(org_id, member)
        return member

    async def resolve(self, org_id: str, identities: list[str]) -> dict[str, Member]:
        """User ids or addresses → present and former members.

        Keyed by every spelling a member answers to, casefolded: their
        user id and their address. An identity that is nobody in the
        organisation is simply absent.
        """
        wanted = list(
            dict.fromkeys(value.strip() for value in identities if value and value.strip())
        )
        found: dict[str, Member] = {}
        missing: list[str] = []
        for identity in wanted:
            cached = self._fresh(org_id, identity)
            if cached is not None:
                self._file(found, cached)
            else:
                missing.append(identity)
        for start in range(0, len(missing), self.MAX_PEOPLE):
            batch = missing[start : start + self.MAX_PEOPLE]
            response = await self._tokens.request(
                "POST",
                f"{IgniPaths.DIRECTORY}/people/resolve",
                org_id=org_id,
                json=ResolveRequest(identities=batch).model_dump(),
            )
            for member in self._parsed(response, Members).people:
                self._remember(org_id, member)
                self._file(found, member)
        return found

    @staticmethod
    def _file(found: dict[str, Member], member: Member) -> None:
        for key in (member.user_id, member.email):
            if key:
                found[key.casefold()] = member

    def _fresh(self, org_id: str, identity: str) -> Member | None:
        key = (org_id, identity.casefold())
        entry = self._cache.get(key)
        if entry is None:
            return None
        if self._clock.monotonic() - entry.stamped > self.TTL:
            self._cache.pop(key, None)
            return None
        return entry.member

    def _remember(self, org_id: str, member: Member) -> None:
        entry = CachedMember(stamped=self._clock.monotonic(), member=member)
        for key in (member.user_id, member.email):
            if key:
                self._cache[(org_id, key.casefold())] = entry

    @staticmethod
    def _parsed[T: BaseModel](response: httpx.Response, model: type[T]) -> T:
        if response.status_code != 200:
            raise IgniUnavailable(f"igni's directory answered {response.status_code}")
        try:
            return model.model_validate(response.json())
        except (ValidationError, ValueError) as exc:
            raise IgniUnavailable("igni's directory answered with something else") from exc


class Directory:
    """igni's directory, for one organisation. What `host.directory` is."""

    def __init__(self, client: DirectoryClient, org_id: str) -> None:
        self._client = client
        self._org_id = org_id

    @property
    def org_id(self) -> str:
        return self._org_id

    async def organisation(self) -> Organisation:
        """The organisation: name, slug, `tenant_number`, domains."""
        return await self._client.organisation(self._org_id)

    async def members(
        self, *, query: str = "", limit: int = 100, include_former: bool = False
    ) -> list[Member]:
        """Prefix search on address or name; at most 500."""
        return await self._client.members(
            self._org_id, query=query, limit=limit, include_former=include_former
        )

    async def person(self, user_id: str) -> Member | None:
        """One person, present or former; `None` if they were never a member."""
        return await self._client.person(self._org_id, user_id)

    async def resolve(self, identities: list[str]) -> dict[str, Member]:
        """User ids or addresses → members, keyed by casefolded id and address."""
        return await self._client.resolve(self._org_id, identities)
